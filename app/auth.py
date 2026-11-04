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

from .db import meta_get, meta_set

COOKIE = "mx_auth"
# No lifetime on the cookie on purpose (owner, 2026-11): a *session* cookie, so it dies
# with the browser instead of sitting valid for a month. Closing a tab cannot end it --
# the server never sees that -- and the browser can restore session cookies on restart,
# which is why the password remains the thing that really ends a session (changing it
# invalidates every outstanding cookie at once).


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
    """True when this request carries a valid session cookie."""
    if not enabled():
        return True
    return cookie_ok(request.cookies.get(COOKIE))


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
