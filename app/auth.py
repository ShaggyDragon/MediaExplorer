"""Optional password gate — casual, not a security boundary.

Andy's words: "it doesn't have to be that secure - just for the casual user not being
able to get to the software". One shared password, no accounts, no lockout beyond a
small delay: enough that a visitor cannot open the library, and honest about that.

Two settings, set in the Unraid container template (env):

    MX_AUTH=1|0             MX_AUTH_PASSWORD=<anything>

The environment is read **on every check**, never copied into the database at startup:
a container whose settings say "locked, with a password" is locked from the first
request, and no stored row can contradict the container settings. (0.1.211 seeded the
DB once at startup, and a stale row left the library open until the app was restarted
— that is exactly why this reads live.)

Without those env vars the stored state governs instead, which is what the admin
endpoint writes; that exists so the switch can be exercised without recreating the
container. While the env vars are present the admin endpoint refuses, because the
container settings would win anyway and a silent no-op is worse than an error.

Stored state (only consulted when the env is absent):
    auth_enabled  "1" / "0"
    auth_salt     random hex, generated once and kept — sessions survive restarts
    auth_hash     sha256(salt + password), never the password itself
    auth_secret   random hex, keys the cookie
"""
import hashlib
import hmac
import os
import secrets
import time

from .db import exec_many, meta_get, meta_set, one

COOKIE = "mx_auth"
# No lifetime on the cookie on purpose (owner, 2026-11): a *session* cookie, so it dies
# with the browser instead of sitting valid for a month. Closing a tab cannot end it --
# the server never sees that -- and browsers *restore* session cookies on restart
# ("continue where you left off"), so a cold boot used to come back logged in. The real
# limit is therefore server-side and time-based: a session must be USED within
# MX_SESSION_IDLE_MIN minutes (default 30) or the password is asked again (v1.4.2).
IDLE_DEFAULT_MIN = 30.0


def idle_seconds():
    """How long a session may sit unused before the password is asked again.

    MX_SESSION_IDLE_MIN overrides the default; 0 or a negative value switches the
    timeout off entirely (the cookie is then the only limit, as before v1.4.2).
    """
    raw = os.environ.get("MX_SESSION_IDLE_MIN")
    try:
        mins = float(raw) if raw not in (None, "") else IDLE_DEFAULT_MIN
    except (TypeError, ValueError):
        mins = IDLE_DEFAULT_MIN
    return max(0.0, mins) * 60.0


class FixedByEnvironment(Exception):
    """The container settings own this switch; the admin endpoint may not change it."""


def _hash(password, salt):
    return hashlib.sha256((salt + (password or "")).encode("utf-8")).hexdigest()


def _salt():
    s = meta_get("auth_salt")
    if not s:
        s = secrets.token_hex(16)
        meta_set("auth_salt", s)
    return s


def _secret():
    s = meta_get("auth_secret")
    if not s:
        s = secrets.token_hex(32)
        meta_set("auth_secret", s)
    return s


def env_settings():
    """(switch_on, password) from the container settings; None when neither is set."""
    en = os.environ.get("MX_AUTH")
    pw = os.environ.get("MX_AUTH_PASSWORD")
    if en is None and pw is None:
        return None
    on = str(en or "").strip().lower() in ("1", "true", "yes", "on")
    return on, (pw or "")


# Every request asks "is it locked?", and two containers share the database, so the
# stored-state path is cached for two seconds. The env path costs nothing.
_CACHE = {"t": 0.0, "hash": "", "on": False}


def _state(force=False):
    """The effective switch and password hash."""
    env = env_settings()
    if env is not None:
        on, pw = env
        return on, (_hash(pw, _salt()) if pw.strip() else "")
    now = time.time()
    if force or now - _CACHE["t"] > 2.0:
        _CACHE["hash"] = meta_get("auth_hash") or ""
        _CACHE["on"] = meta_get("auth_enabled") == "1"
        _CACHE["t"] = now
    return _CACHE["on"], _CACHE["hash"]


def source():
    """'container settings' when the env drives it, else 'admin override'."""
    return "container settings" if env_settings() is not None else "admin override"


def enabled():
    on, h = _state()
    return bool(on and h)


def set_auth(on, password=None):
    """Admin path (no container recreate). Refused while the env owns the switch."""
    if env_settings() is not None:
        raise FixedByEnvironment(
            "MX_AUTH / MX_AUTH_PASSWORD are set in the container settings — change them "
            "there (Docker tab → Edit → Apply) instead of through the API.")
    if password is not None:
        meta_set("auth_hash", _hash(password, _salt()) if str(password).strip() else "")
    meta_set("auth_enabled", "1" if on else "0")
    _state(force=True)
    end_all_sessions()          # the token changes with the password; drop the rows too
    return enabled()


def password_ok(password):
    _, want = _state()
    if not want:
        return False
    return hmac.compare_digest(_hash(password, _salt()), want)


def token():
    """The value the cookie carries: a keyed digest, not the password."""
    _, want = _state()
    if not want:
        return ""
    return hmac.new(_secret().encode("utf-8"), want.encode("utf-8"), hashlib.sha256).hexdigest()


def cookie_ok(value):
    if not value:
        return False
    return hmac.compare_digest(str(value), token())


def check(request):
    """True when this request carries a valid, recently-used session cookie."""
    if not enabled():
        return True
    value = request.cookies.get(COOKIE)
    if not cookie_ok(value):
        return False
    return session_alive(value)


# ---- sessions ------------------------------------------------------------
# The cookie says *who* (a keyed digest of the password); the table says *when it was
# last used*. Rows are keyed by a hash of the cookie value, so the database never holds
# anything that could be replayed as a login.

_touched = {}       # in-memory throttle: at most one last_seen write per session per minute


def _session_tok(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def start_session(value):
    """Record a successful login. Prunes rows that are already unusable."""
    if not value:
        return
    now = time.time()
    ttl = idle_seconds()
    exec_many("INSERT OR REPLACE INTO auth_session(tok, last_seen) VALUES(?,?)",
              (_session_tok(value), now))
    _touched[_session_tok(value)] = now
    if ttl > 0:
        exec_many("DELETE FROM auth_session WHERE last_seen < ?", (now - ttl,))


def end_session(value):
    if value:
        exec_many("DELETE FROM auth_session WHERE tok=?", (_session_tok(value),))
        _touched.pop(_session_tok(value), None)


def end_all_sessions():
    """A password change must not leave a live session behind."""
    exec_many("DELETE FROM auth_session")
    _touched.clear()


def session_alive(value):
    """Has this cookie's session been used inside the idle window?

    Every authenticated request counts as use, and the app's own `/api/config` poll
    (once a minute while a tab is open) is enough to keep an idle-looking tab alive --
    so the timeout measures "nothing was connected", not "nobody clicked"."""
    ttl = idle_seconds()
    if ttl <= 0:
        return True                     # timeout switched off
    now = time.time()
    tok = _session_tok(value)
    row = one("SELECT last_seen FROM auth_session WHERE tok=?", (tok,))
    if row is None:
        return False                    # logged out, expired, or from before v1.4.2
    try:
        seen = float(row["last_seen"] or 0)
    except (TypeError, ValueError):
        seen = 0.0
    if now - seen > ttl:
        exec_many("DELETE FROM auth_session WHERE tok=?", (tok,))
        _touched.pop(tok, None)
        return False
    if now - _touched.get(tok, 0.0) > 60.0:     # one write a minute, not one per request
        exec_many("UPDATE auth_session SET last_seen=? WHERE tok=?", (now, tok))
        _touched[tok] = now
    return True


def public_path(path):
    """What a locked library still serves: the login page and its own assets."""
    return (path in ("/login", "/logout", "/favicon.ico")
            or path.startswith("/static/"))


def secure_for(request):
    """Mark the cookie Secure when the page really arrived over https.

    Behind a reverse proxy that terminates TLS it does; on the plain-http LAN URL it does
    not, and a Secure cookie there would simply never be stored -- the login would look
    broken on the LAN while working through the proxy."""
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return proto == "https" or request.url.scheme == "https"


def seed_from_env():
    """Left for the old startup hook: nothing to do now, the env is read live."""
    return None
