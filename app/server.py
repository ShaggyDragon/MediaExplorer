import io
import csv
import json
import logging
import traceback
import sys
import mimetypes
import os
import threading
import time
import zipfile
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, Response,
                               StreamingResponse, RedirectResponse, PlainTextResponse)
from natsort import natsorted

from .config import (APP_NAME, VERSION, PORT, DATA_PATH, media_locations, IMAGE_EXTS, EAGER_BACKLOG,
                     FRAME_COUNT,
                     THUMB_GEN,
                     THUMB_WORKERS, WIPE_ON_BUILD)
from .db import (db, q, one, exec_many, node_by_id, child_nodes, meta_get, meta_set,
                 pending_media_count, THUMB_WORK, sweep_orphans, set_slow_hook,
                 prefs_list, pref_get, pref_set, pref_drop, store_epoch)
log = logging.getLogger("mediaexplorer")
# Nothing configured logging before, so the module logger had no handler: log.info()
# went nowhere and warnings leaked out through lastResort without a timestamp. That
# made "the client says it clicked and got a 404" invisible in `docker logs`.
if not logging.getLogger().handlers:
    logging.basicConfig(level=os.environ.get("MX_LOG_LEVEL", "INFO").upper(),
                        stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log.setLevel(logging.INFO)

from .info import picture_info, video_info
from .media import (build_stats, HW_REASON, _HW_NOTE, node_abspath, video_member_bytes, POSTER_NAME,
                    queue_durations,
                    ZIP_TILE, queue_zip_tile, queue_video_poster, queue_frames,
                    queue_dir_tile, rebuild_scope, relink_orphans,
                    existing_frames, diff_dir, index_zip, zip_has_video,
                    gen_node_thumb, gen_entry_thumb, gen_folder_thumbs, gen_zip_pack,
                    gen_zip_tile, gen_dir_tile,
                    queue_zip_pack, queue_folder_pack, extract_frames, frames_dir,
                    zip_entry_bytes)
from . import packs, events

app = FastAPI(title=APP_NAME, version=VERSION)
WEB = Path(__file__).resolve().parent.parent / "web"
BOOT_TS = time.time()

# 404 census: "lots of 404s in the log" is only diagnosable if you know WHICH
# path repeats. A cache-busted thumbnail URL and a vanished tree id look
# identical in uvicorn's access log once the query string scrolls past.
_nf = {}
_nf_detail = deque(maxlen=300)
_nf_lock = threading.Lock()


_http_err = deque(maxlen=200)      # any 4xx/5xx that is not a plain 404

_dup_guard = {}                    # (status, path) -> ts, so one failure is one row
_LOG_FILE = None                   # DATA_PATH/client.log, resolved lazily
_LOG_FILE_MAX = 4 * 1024 * 1024


def ring(rec, to_file=True):
    """Record a telemetry row in the ring and, for anything that went wrong, on disk.

    The in-memory ring is lost on restart, and every deploy restarts the app -- so a
    report that arrived before the last deploy could never be looked at afterwards.
    Errors are appended to DATA_PATH/client.log (rotated once at 4MB) and served back
    by /api/log/file, which is how a 422 from a tab that has since been reloaded can
    still be identified."""
    global _LOG_FILE
    # the middleware and the exception handlers both see the same failure: keep the
    # richer (handler) record and drop the duplicate that lands a moment later
    key = (rec.get("status"), rec.get("path") or rec.get("req") or rec.get("src"))
    now = rec.get("ts") or time.time()
    if rec.get("ev") == "http_error" and key[0] and key[1]:
        prev = _dup_guard.get(key)
        if prev and abs(now - prev) < 2.0:
            return
        _dup_guard[key] = now
    _client_log.append(rec)
    if not to_file or rec.get("ev") not in ("http_error", "err"):
        return
    try:
        if _LOG_FILE is None:
            _LOG_FILE = DATA_PATH / "client.log"
        try:
            if _LOG_FILE.stat().st_size > _LOG_FILE_MAX:
                os.replace(_LOG_FILE, str(_LOG_FILE) + ".1")
        except OSError:
            pass
        with open(_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str)[:2000] + "\n")
    except Exception:
        pass                       # telemetry must never break a request


# Slow queries land in the same ring as errors, so "is the database the bottleneck?"
# is answered by looking at the log instead of guessing (MX_SLOW_MS, default 50ms).
set_slow_hook(lambda rec: ring(rec, to_file=False))


@app.exception_handler(Exception)
async def _log_unhandled(request: Request, exc: Exception):
    """A 500 must name itself.

    Unhandled exceptions are answered by Starlette's outermost error middleware, which
    sits ABOVE the logging middleware -- so the crash never reached the ring and the
    client only ever saw 'HTTP 500'. The traceback is now kept, which is how
    'UNIQUE constraint failed: nodes...' was found in the container log instead."""
    tb = traceback.format_exc()
    ring({"ev": "http_500", "path": request.url.path, "query": request.url.query[:200],
          "method": request.method, "detail": tb[-1500:], "ts": time.time()},
         to_file=True)
    log.error("500 %s %s?%s\n%s", request.method, request.url.path,
              request.url.query[:120], tb[-1500:])
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.exception_handler(StarletteHTTPException)
async def _log_http_exception(request: Request, exc: StarletteHTTPException):
    """Every deliberate 4xx/5xx names itself in the ring and on disk.

    BaseHTTPMiddleware hands back a streaming response, so the middleware's body capture
    sees nothing and 'detail' arrived empty -- which is why a 422 could be recorded and
    still tell nobody what it meant."""
    if exc.status_code != 404:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, default=str)
        ring({"ev": "http_error", "status": exc.status_code, "path": request.url.path,
              "query": request.url.query[:200], "method": request.method,
              "detail": str(detail)[:600], "ts": time.time()})
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def _log_validation_error(request: Request, exc: RequestValidationError):
    """A 422 names the field it rejected, instead of arriving as a bare status.

    FastAPI validates the path/query before the route body runs, so the middleware's
    body capture cannot see these; the error list is the whole story and it is what
    made one phantom 'Failed to load resource: 422' take three rounds to pin down."""
    detail = json.dumps(exc.errors(), default=str)[:800]
    rec = {"ev": "http_error", "status": 422, "path": request.url.path,
           "query": request.url.query[:200], "method": request.method,
           "detail": detail, "ts": time.time()}
    ring(rec)
    _http_err.append(rec)
    log.warning("422 %s %s?%s -> %s", request.method, request.url.path,
                request.url.query[:120], detail[:400])
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.middleware("http")
async def count_not_found(request: Request, call_next):
    resp = await call_next(request)
    # Anything the client reports as "Failed to load resource: 422" must be
    # answerable from here: the URL, the query and the response body are recorded
    # in the /api/log ring, so a rejected request names itself.
    if 400 <= resp.status_code < 600 and resp.status_code != 404:
        try:
            body = resp.body[:300].decode("utf-8", "replace") if getattr(resp, "body", None) else ""
        except Exception:
            body = ""
        rec = {"ev": "http_error", "status": resp.status_code, "path": request.url.path,
               "query": request.url.query[:200], "method": request.method,
               "detail": body, "ts": time.time()}
        _http_err.append(rec)
        ring(rec)
        log.warning("http %s %s?%s -> %s %s", request.method, request.url.path,
                    request.url.query[:120], resp.status_code, body[:200])
    if resp.status_code == 404:
        path = request.url.path
        # /api/thumb/node/123?v=... -> /api/thumb/node/:id, so counts aggregate
        parts = path.split("/")
        if len(parts) > 3 and parts[1] == "api" and parts[2] in ("thumb", "file"):
            path = "/".join(parts[:4]) + "/:id"
        with _nf_lock:
            _nf[path] = _nf.get(path, 0) + 1
            _nf_detail.append((round(time.time(), 2), request.url.path,
                               request.url.query[:80]))
    return resp

# ---------------------------------------------------------------- password gate
# Registered AFTER count_not_found on purpose: the last middleware added is the
# outermost, so a locked library answers 401/303 before the error recorder sees it.
# "Casual protection": one shared password from the Unraid settings
# (MX_AUTH / MX_AUTH_PASSWORD), remembered in a signed cookie for 30 days.

def _token_ok(request):
    """Admin token on this request? Used to keep deploys working while locked."""
    import hmac
    want = _admin_token()
    got = request.headers.get("x-mx-token", "")
    return bool(want) and hmac.compare_digest(got, want)


@app.middleware("http")
async def password_gate(request: Request, call_next):
    from . import auth
    if not auth.enabled():
        return await call_next(request)
    path = request.url.path
    if auth.public_path(path) or auth.check(request) or _token_ok(request):
        return await call_next(request)
    if path.startswith("/api/") or path in ("/events", "/log/stream"):
        return JSONResponse({"detail": "password required"}, status_code=401)
    from urllib.parse import quote
    return RedirectResponse("/login?next=" + quote(path + (("?" + request.url.query) if request.url.query else "")),
                            status_code=303)


def _login_page(request, next_url="/", error=""):
    from . import auth
    html = (WEB / "login.html").read_text()
    html = (html.replace("{{APP_NAME}}", APP_NAME).replace("{{VERSION}}", VERSION)
                .replace("{{NEXT}}", (next_url or "/").replace('"', "&quot;"))
                .replace("{{ERROR}}", error))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/login", include_in_schema=False)
def login_form(request: Request, next: str = "/"):
    from . import auth
    if not auth.enabled() or auth.check(request):
        return RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    return _login_page(request, next)


@app.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    from . import auth
    from urllib.parse import parse_qs
    raw = (await request.body()).decode("utf-8", "replace")
    data = {k: v[-1] for k, v in parse_qs(raw).items()}
    nxt = data.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if not auth.enabled():
        return RedirectResponse("/", status_code=303)
    if not auth.password_ok(data.get("password", "")):
        time.sleep(0.6)                       # cheap friction, not a lockout system
        log.warning("auth: failed password from %s", request.client.host if request.client else "?")
        return _login_page(request, nxt, "Incorrect password")
    resp = RedirectResponse(nxt, status_code=303)
    # no max_age/expires: a session cookie, kept in browser memory and dropped when the
    # browser closes (see the note in auth.py). Browsing never extends it because this is
    # the only place the cookie is ever set.
    resp.set_cookie(auth.COOKIE, auth.token(), path="/",
                    httponly=True, samesite="lax", secure=auth.secure_for(request))
    log.info("auth: unlocked (%s)", request.client.host if request.client else "?")
    return resp


@app.get("/logout", include_in_schema=False)
def logout():
    from . import auth
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.post("/api/admin/auth")
async def api_admin_auth(request: Request):
    """Set the gate without recreating the container (admin token only).

    Body: {"enabled": true|false, "password": "..."} — password optional. Only works
    while MX_AUTH / MX_AUTH_PASSWORD are absent from the environment: once the
    container settings define the gate they win on every request, so this answers 409
    instead of pretending to change something."""
    _admin_ok(request)
    from . import auth
    try:
        body = json.loads((await request.body()) or b"{}")
    except Exception:
        body = {}
    on = body.get("enabled")
    pw = body.get("password")
    if on is None and pw is None:
        raise HTTPException(422, "give enabled and/or password")
    try:
        res = auth.set_auth(bool(on) if on is not None else auth.enabled(), pw)
    except auth.FixedByEnvironment as e:
        raise HTTPException(409, str(e))
    log.info("auth: set by admin (enabled=%s, password=%s)", bool(on), "changed" if pw is not None else "unchanged")
    return {"enabled": res, "source": auth.source()}


_builds = {}          # node_id -> {"total": n, "done": n, "t0": ts}
_builds_lock = threading.Lock()

HOVER_BUILD = os.environ.get("MX_HOVER_BUILD", "0").strip() == "1"

scan_state = {"running": False, "phase": "idle", "folders": 0,
              "thumb_total": 0, "thumb_done": 0,
              "anim_total": 0, "anim_done": 0, "anim_skipped": 0,
              # the folder/zip being worked on and how many thumbnails it needs, so the
              # Scan badge can count down live instead of sitting on one number
              "loc": None, "parent": None, "zip": None, "work_total": 0,
              # a subtree walk publishes events from folder after folder, so the page
              # cannot match on one parent id; "deep" tells it every event is its own
              "deep": False}
_scan_lock = threading.Lock()


# ---------------------------------------------------------------- locations

def sync_locations():
    want = media_locations()
    have = {r["root"]: r for r in q("SELECT * FROM locations")}
    for name, path in want:
        root = os.path.realpath(path)
        if root not in have:
            exec_many("INSERT INTO locations(name,root) VALUES(?,?)", (name, root))
        elif have[root]["name"] != name:
            exec_many("UPDATE locations SET name=? WHERE root=?", (name, root))
    keep = {os.path.realpath(p) for _, p in want}
    for root, r in have.items():
        if root not in keep:
            exec_many("DELETE FROM zip_entries WHERE zip_id IN "
                      "(SELECT id FROM nodes WHERE loc_id=?)", (r["id"],))
            exec_many("DELETE FROM nodes WHERE loc_id=?", (r["id"],))
            exec_many("DELETE FROM locations WHERE id=?", (r["id"],))


@app.on_event("startup")
def _startup():
    db()
    Path(__file__).resolve()  # ensure import order
    # Unraid container settings are the truth after every start: MX_AUTH and
    # MX_AUTH_PASSWORD overwrite whatever the admin endpoint last set.
    from . import auth as _auth
    log.info("auth: gate is %s (Unraid MX_AUTH=%r, password %s)",
             "ON" if _auth.enabled() else "off",
             os.environ.get("MX_AUTH"), "set" if os.environ.get("MX_AUTH_PASSWORD") else "empty")
    sync_locations()
    _wipe_for_new_build()
    # MASTER SWITCH: nothing regenerates behind the user's back. A geometry change
    # is applied folder by folder, on request (right-click -> rebuild), because a
    # library-wide sweep costs hours of I/O and CPU for pictures he may not want
    # rebuilt at all. MX_AUTO_REBUILD=1 restores the old blanket invalidation.
    if AUTO_REBUILD:
        _invalidate_thumb_geometry()
        _invalidate_posters()
    if meta_get("dirtile_v1") != VERSION:
        # 0.1.96 gave up on category folders (no media of their own). They are
        # retried once now that a folder may borrow pictures from its children.
        n = (one("SELECT COUNT(*) c FROM nodes WHERE kind='dir' AND thumb_state='empty'") or {"c": 0})["c"]
        exec_many("UPDATE nodes SET thumb_state='none'"
                  " WHERE kind='dir' AND thumb_state='empty'")
        meta_set("dirtile_v1", VERSION)
        log.info("folder tiles: %s category folders back in the queue", n)
    if meta_get("relinked_v2") != VERSION:
        # Undo a blanket invalidation from an earlier build by re-pointing rows at
        # members that still exist in their packs: no decoding, no rebuild. Runs in
        # the background so a big library does not delay the first page.
        def _relink():
            try:
                n = relink_orphans()
                meta_set("relinked_v2", VERSION)
                log.info("relinked %s thumbnails to their existing pack members", n)
            except Exception as e:
                log.warning("relink failed: %s", e)
        threading.Thread(target=_relink, daemon=True).start()


def wipe_enabled() -> bool:
    """Effective wipe-on-build. A DB override wins over the container env.

    The env is baked into the container at create time, so an instance that no longer
    needs the dev-only wipe (every thumbnail now carries its own generation, so stale
    ones rebuild lazily) can be switched off without recreating it:
        POST /api/admin/settings {"wipe_on_build": false}   (x-mx-token)
    """
    v = meta_get("wipe_on_build")
    if v is None:
        return WIPE_ON_BUILD
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def _wipe_for_new_build():
    """MX_WIPE_ON_BUILD: thumbnail stores are derived data, so a new image should
    start empty rather than mix packs written by older code. Keyed on VERSION, so
    a plain restart keeps whatever was built."""
    if not wipe_enabled():
        return
    if meta_get("wiped_for") == VERSION:
        return
    try:
        from .packtool import purge_all
        res = purge_all()
        meta_set("wiped_for", VERSION)
        log.info("new build %s: wiped %s packs + %s loose thumbs (%s bytes)",
                 VERSION, res.get("packs"), res.get("loose"), res.get("freed"))
    except Exception as e:                      # never block startup on this
        log.warning("wipe-on-build failed: %s", e)


# ---------------------------------------------------------------- thumb geometry

# Bumping this re-pends every ready thumbnail once, so a change to how masters are
# encoded (0.1.94: cover-crop -> letterbox) reaches the whole library instead of
# only the folders that happen to be rescanned. Rows keep serving nothing until
# rebuilt, which is why the packs are not deleted: their bytes stay readable for
# anything already pointing at them, and dead members are compacted away later.
# THUMB_GEN comes from config: it is stamped on every thumbnail, so a scan only
# re-encodes what an older generation made.
AUTO_REBUILD = os.environ.get("MX_AUTO_REBUILD", "0").strip() == "1"


def _invalidate_posters():
    """Posters built by the poster+filmstrip path were still cover-cropped.

    Only videos are touched, and only once per generation: a full-library pass costs
    each folder one rebuild on its next visit.
    """
    if meta_get("poster_gen") == str(THUMB_GEN):
        return
    try:
        n = exec_many("UPDATE nodes SET pack_id=NULL, pack_off=NULL, pack_len=NULL,"
                      " thumb_state='pending' WHERE thumb_state='ready' AND kind='video'")
        meta_set("poster_gen", str(THUMB_GEN))
        log.info("poster geometry %s: re-pended %s video posters", THUMB_GEN, n)
    except Exception as e:
        log.warning("poster invalidation failed: %s", e)


def _invalidate_thumb_geometry():
    if meta_get("thumb_gen") == str(THUMB_GEN):
        return
    try:
        n1 = exec_many("UPDATE nodes SET pack_id=NULL, pack_off=NULL, pack_len=NULL,"
                       " thumb_state='pending' WHERE thumb_state='ready'")
        n2 = exec_many("UPDATE zip_entries SET pack_id=NULL, pack_off=NULL, pack_len=NULL,"
                       " thumb_state='pending' WHERE thumb_state='ready'")
        meta_set("thumb_gen", str(THUMB_GEN))
        log.info("thumb geometry %s: re-pended %s nodes + %s zip entries for rebuild",
                 THUMB_GEN, n1, n2)
    except Exception as e:                      # never block startup on this
        log.warning("thumb geometry invalidation failed: %s", e)


# ---------------------------------------------------------------- helpers

def loc_or_404(loc_id):
    r = one("SELECT * FROM locations WHERE id=?", (loc_id,))
    if not r:
        raise HTTPException(404, "unknown location")
    return r


def node_or_404(node_id):
    r = node_by_id(node_id)
    if not r:
        raise HTTPException(404, "unknown node")
    return r


def nsort(rows, sort_mode):
    def keyf(r):
        return r["name"]
    ordered = natsorted(rows, key=keyf)
    if sort_mode == "date":
        ordered = sorted(ordered, key=lambda r: -r["mtime"])
    # folders & zips first regardless
    order = {"dir": 0, "zip": 1, "image": 2, "video": 3}
    ordered.sort(key=lambda r: order[r["kind"]])
    return ordered


def _row_val(row, key, default=0):
    """A row value that tolerates a column the table does not have.

    zip_entries has no `mtime` (it keeps the zip's), so reading one raises
    IndexError on a sqlite3.Row -- and an uncaught one turns a listing into a 500.
    """
    try:
        v = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if v is None else v


def thumb_vstamp(row):
    """Cache-busting tag for a thumbnail URL: the identity of the BYTES.

    The browser keeps a thumbnail for a day, keyed by URL. `vstamp` folds in the
    source file's mtime, but a re-encode never changes the source file -- so a
    rebuilt thumbnail kept its old URL and the browser answered with the day-old
    picture. That is how letterboxed thumbs "reverted" to cover-cropped ones on
    the next reload: new bytes on the server, old bytes in the cache, same URL.
    Generation + pack offset change on every rebuild, so they go in the URL too.
    """
    try:
        gen = int(_row_val(row, "thumb_gen"))
    except (TypeError, ValueError):
        gen = 0
    try:
        off = int(_row_val(row, "pack_off"))
    except (TypeError, ValueError):
        off = 0
    return f"{vstamp(row)}-{gen}-{off}-{store_epoch()}"


def vstamp(row):
    """Cache-busting tag for a node's thumb URL.

    Images are cached for a day by the browser, keyed by URL. Node ids are not
    stable across a purge/re-index (they restart at 1), so a bare
    /api/thumb/node/37 could show a completely different file's picture after a
    rebuild. Folding the file's mtime into the URL makes a recycled id a
    different URL."""
    try:
        return int(float(_row_val(row, "mtime") or 0) * 1000)
    except (IndexError, KeyError, TypeError, ValueError):
        return 0


def item_payload(row):
    v = vstamp(row)
    d = {"id": row["id"], "kind": row["kind"], "name": row["name"],
         "size": row["size"], "mtime": row["mtime"],
         "thumb": (f"/api/thumb/node/{row['id']}?v={thumb_vstamp(row)}"
                   if row["thumb_state"] == "ready" else None),
         "pending": row["thumb_state"] == "pending"}
    if row["kind"] == "zip":
        # entries inside this archive that could not be decoded (see zip_defect): the
        # tile paints a badge from this, the repair list is /api/defects
        bad = _row_val(row, "bad_count", 0) or 0
        if bad:
            d["bad"] = bad
    if row["kind"] == "video":
        # running time for the tile label; absent until measured (-1 = unreadable)
        dur = _row_val(row, "dur", 0)
        if dur and dur > 0:
            d["dur"] = dur
        # The preview's frame list rides along with the listing: the page used to ask
        # /api/frames once per clip as each tile came into view -- 43 requests for one
        # 43-clip folder (~100ms each, measured) -- for data the server reads from the
        # same folder pack it is already listing. Same URLs /api/frames returns.
        if row["thumb_state"] == "ready":
            names = existing_frames(row["id"])
            st = thumb_vstamp(row)
            # an explicit [] too: a short clip with nothing to flip would otherwise
            # still ask /api/frames once just to hear "nothing"
            d["frames"] = [f"/api/framefile/{row['id']}/{n}?v={st}" for n in names]
    if row["kind"] == "video" and row["thumb_state"] != "ready":
        queue_video_poster(row["id"])       # one frame ~5s in, not a filmstrip
    # a folder row starts as thumb_state='none' (no picture at all); 'empty' is the
    # one gen_dir_tile writes after looking and finding nothing, so it is not retried
    if row["kind"] == "dir" and row["thumb_state"] in ("none", "pending"):
        # same 2x2 mosaic a zip gets, so a folder shows its content, not a glyph.
        # Throttled inside queue_dir_tile: a view with 60 subfolders does not turn
        # into 240 image decodes in one go.
        queue_dir_tile(row["id"])
    if row["kind"] == "zip":
        d["has_video"] = zip_has_video(row["id"])
        if row["thumb_state"] != "ready":
            queue_zip_tile(row["id"])          # 4-up tile, built on first sight
        samples = q("SELECT id,thumb,pack_off,thumb_gen FROM zip_entries WHERE zip_id=?"
                    " AND kind='image' AND thumb IS NOT NULL ORDER BY name LIMIT 4",
                    (row["id"],))
        d["samples"] = [f"/api/thumb/entry/{s['id']}?v={thumb_vstamp(s)}" for s in samples]
        cnt = one("SELECT COUNT(*) c FROM zip_entries WHERE zip_id=?", (row["id"],))
        d["entries"] = cnt["c"] if cnt else 0
    return d


def breadcrumb(loc, parent_id):
    path = [{"id": None, "name": loc["name"], "loc": loc["id"]}]
    chain = []
    cur = parent_id
    while cur is not None:
        r = node_by_id(cur)
        if not r:
            break
        chain.append({"id": r["id"], "name": r["name"],
                      "kind": "z" if r["kind"] == "zip" else "f"})
        cur = r["parent_id"]
    return path + list(reversed(chain))


# ---------------------------------------------------------------- api

@app.on_event("startup")
async def _startup_events():
    import asyncio
    events.set_loop(asyncio.get_running_loop())


@app.get("/api/events")
async def api_events():
    """Live progress: one event per freshly built thumbnail.

    Tiles paint as they are made, so the client does not need a "building…"
    notice to prove it is working."""
    import asyncio, json
    q = events.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"          # keeps proxies from closing
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/config")
def api_config():
    locs = q("SELECT id,name,root FROM locations ORDER BY id")
    return {"name": APP_NAME, "version": VERSION, "started": int(BOOT_TS),
            **_pool_report(),
            "locations": [{"id": l["id"], "name": l["name"]} for l in locs],
            "thumb_workers": THUMB_WORKERS,
            "hwaccel": HW_REASON or ("auto: " + ("; ".join(_HW_NOTE.values()) or "not probed yet")),
            "wipe_on_build": wipe_enabled(),          # effective (DB override wins)
            "wipe_env": WIPE_ON_BUILD,                # what the container asked for
            "wiped_for": (meta_get("wiped_for") if wipe_enabled() else None),
            "pending": pending_media_count(),
            # Visible proof of what the container settings actually gave this process:
            # `env_switch` None means MX_AUTH is not in the environment at all.
            "auth": {"enabled": _auth_enabled(), "source": _auth_source(),
                     "env_switch": os.environ.get("MX_AUTH"),
                     "env_password": bool(os.environ.get("MX_AUTH_PASSWORD"))}}


def _auth_enabled():
    from . import auth
    return auth.enabled()


def _auth_source():
    from . import auth
    return auth.source()


def _admin_token():
    """The admin token, from the env or from the bind-mounted /app/.deploy.env.

    A recreated container without `-e MX_RESTART_TOKEN` silently disables every
    admin endpoint -- including restart, which is the only way to deploy new code
    from here. The file lives in the source mount we already have, so treat it as
    the fallback instead of locking the deploy path out."""
    tok = os.environ.get("MX_RESTART_TOKEN", "")
    if tok:
        return tok
    try:
        for line in Path("/app/.deploy.env").read_text().splitlines():
            if line.startswith("MX_RESTART_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _admin_ok(request):
    """Token guard shared by the admin endpoints (MX_RESTART_TOKEN)."""
    import hmac
    want = _admin_token()
    if not want:
        raise HTTPException(403, "admin endpoints disabled (set MX_RESTART_TOKEN)")
    if not hmac.compare_digest(request.headers.get("x-mx-token", ""), want):
        raise HTTPException(403, "bad token")


@app.post("/api/admin/settings")
async def api_admin_settings(request: Request):
    """Persisted per-instance switches that beat the container env. Currently
    wipe_on_build only; keys are validated so a typo cannot write junk into meta."""
    _admin_ok(request)
    body = await request.json()
    allowed = {"wipe_on_build"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(400, f"unknown setting(s): {sorted(unknown)}")
    if "wipe_on_build" in body:
        meta_set("wipe_on_build", "1" if body["wipe_on_build"] else "0")
        log.info("admin: wipe_on_build := %s", body["wipe_on_build"])
    return {"wipe_on_build": wipe_enabled(), "wipe_env": WIPE_ON_BUILD,
            "wiped_for": meta_get("wiped_for")}


@app.get("/api/admin/work")
def api_admin_work(request: Request):
    """What is the app doing RIGHT NOW? Read-only, admin token.

    Answers "is it building the whole folder?" with facts: the queued job keys
    (one per unit -- a folder job builds every pending image in that folder, a
    video job builds one filmstrip), the worker counts, and the scan snapshot."""
    _admin_ok(request)
    from .media import _inflight, _inflight_lock, _executor
    with _inflight_lock:
        jobs = sorted(str(k) for k in _inflight)
    return {"inflight": jobs[:40], "inflight_count": len(jobs),
            "executor_threads": getattr(_executor, "_max_workers", None),
            "pending": pending_media_count(),
            "scan": dict(scan_state)}


_dirface = {"running": False, "total": 0, "queued": 0, "finished_at": 0.0}


@app.post("/api/admin/dirfaces")
def api_admin_dirfaces(request: Request):
    """Redraw the 2x2 face of every folder that holds zips, in the background.

    A folder's face is four of its children's pictures. For a folder of zips those
    children used to be their own 2x2 tiles, so the face was sixteen stamps at
    tile size -- unreadable. The mosaic now takes one photo per zip, which means
    every folder already carrying a tile has to be drawn again. Queued through the
    normal tile queue so the four tile workers pace it instead of spiking.
    """
    _admin_ok(request)
    from .media import queue_dir_tile, _dirq, _dirq_lock
    if _dirface["running"]:
        return dict(_dirface)
    ids = [r["id"] for r in q(
        "SELECT d.id FROM nodes d WHERE d.kind='dir' AND d.thumb_state='ready'"
        " AND EXISTS (SELECT 1 FROM nodes z WHERE z.parent_id=d.id AND z.kind='zip')")]
    _dirface.update({"running": True, "total": len(ids), "queued": 0, "finished_at": 0})
    if not ids:
        _dirface["running"] = False
        return dict(_dirface)

    def pump():
        try:
            for nid in ids:
                while True:                    # stay under the bounded backlog
                    with _dirq_lock:
                        depth = len(_dirq["q"])
                    if depth < 200:
                        break
                    time.sleep(0.2)
                queue_dir_tile(nid, force=True)
                _dirface["queued"] += 1
            while True:                        # report done only when drained
                with _dirq_lock:
                    depth, running = len(_dirq["q"]), _dirq["running"]
                if depth == 0 and not running:
                    break
                time.sleep(0.5)
            log.info("dirfaces: redrew %s folder faces", _dirface["queued"])
        finally:
            _dirface["running"] = False
            _dirface["finished_at"] = time.time()
    threading.Thread(target=pump, daemon=True, name="dirfaces").start()
    return dict(_dirface)


@app.get("/api/admin/dirfaces")
def api_admin_dirfaces_status(request: Request):
    """Progress of a running (or last) face redraw."""
    _admin_ok(request)
    from .media import _dirq, _dirq_lock
    with _dirq_lock:
        depth = len(_dirq["q"])
    return dict(_dirface, queue_depth=depth)


@app.get("/api/admin/packs")
def api_admin_packs(request: Request, match: str | None = None):
    """Where are the .mxthumbs packs, and does the index agree? Read-only.

    `?match=<substring>` narrows the listing to paths containing it, so a single
    folder can be located without dumping the whole index."""
    _admin_ok(request)
    from . import packtool
    from .config import DATA_PATH, PACK_MODE, PACK_EXT
    rows = q("SELECT kind, COUNT(*) c, SUM(bytes) b FROM thumb_pack GROUP BY kind")
    walked = list(packtool._walk_packs())
    known = [r["path"] for r in q("SELECT path FROM thumb_pack") if r["path"]]
    central = sorted(str(p) for p in (DATA_PATH / "packs").rglob("*" + PACK_EXT)) \
        if (DATA_PATH / "packs").exists() else []
    if match:
        known = [k for k in known if match in k]
        walked = [w for w in walked if match in w]
    return {"pack_mode": PACK_MODE, "match": match,
            "index_rows": [dict(r) for r in rows],
            "db_paths": len(known),
            "db_missing_on_disk": [k for k in known if not os.path.exists(k)][:5],
            "db_present_on_disk": sum(1 for k in known if os.path.exists(k)),
            "db_sample": [{"path": k, "exists": os.path.exists(k),
                           "bytes": os.path.getsize(k) if os.path.exists(k) else 0}
                          for k in known[:4]],
            "walked_media_packs": len(walked), "walked_sample": walked[:6],
            "central_store_packs": len(central), "central_sample": central[:3]}


@app.get("/api/admin/hwaccel")
def api_hwaccel(request: Request):
    """Why is (or isn't) the GPU decoding video? Reports the devices this
    container can actually see, ffmpeg's hwaccel list, and a timed 1s decode
    with and without NVDEC on a real file. Read-only."""
    _admin_ok(request)
    import glob
    import subprocess
    devs = sorted(glob.glob("/dev/nvidia*") + glob.glob("/dev/dri/*"))
    row = None
    for cand in q("SELECT * FROM nodes WHERE kind='video' LIMIT 20"):
        try:
            if os.path.exists(node_abspath(cand)):
                row = cand
                break
        except OSError:
            continue
    out = {"MX_HWACCEL": os.environ.get("MX_HWACCEL", "cuda (default)"),
           "devices": devs or "none (no --gpus all / /dev/dri passthrough)",
           "ffmpeg": subprocess.run(["ffmpeg", "-hide_banner", "-hwaccels"],
                                    capture_output=True, text=True,
                                    timeout=20).stdout.replace("\n", " ").strip()}
    if not row:
        return out
    src = node_abspath(row)
    out["sample"] = src
    for label, flags in (("cuda", ["-hwaccel", "cuda"]), ("cpu", [])):
        t0 = time.time()
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *flags,
                                "-ss", "1", "-i", src, "-t", "1",
                                "-vf", "scale=168:-1", "-f", "null", "-"],
                               capture_output=True, text=True, timeout=120)
            out[label] = {"seconds": round(time.time() - t0, 2),
                          "ok": r.returncode == 0,
                          "error": (r.stderr or "")[-300:] if r.returncode else ""}
        except subprocess.TimeoutExpired:
            out[label] = {"seconds": round(time.time() - t0, 2), "ok": False, "error": "timeout"}
    return out


@app.post("/api/admin/purge")
def api_purge(request: Request, full: int = 0):
    """Delete every cached thumb and re-pend the index.

    full=1 also drops the SQLite index and restarts, so the next Scan re-indexes
    the library from the filesystem -- the "new data set" test. Only cached
    artefacts and the index go; media is never touched."""
    _admin_ok(request)
    with _scan_lock:
        if scan_state.get("running"):
            raise HTTPException(409, "scan is running; let it finish or restart first")
    from . import packtool
    out = packtool.purge_all()
    out = {"packs": out["packs"], "loose": out["loose"], "freed": out["freed"]}
    if full:
        packtool.purge_db()
        out["index"] = "dropped"
        threading.Timer(0.4, lambda: os._exit(0)).start()
    else:
        out["index"] = "kept (thumbs re-pended)"
    out["version"] = VERSION
    return out


@app.post("/api/admin/restart")
def api_restart(request: Request):
    """Redeploy the code this process is running, without docker access.

    The process replaces ITSELF with a fresh uvicorn (execv), so this works even
    when the container has no restart policy and nothing outside has to be
    watching. Only if execv fails do we fall back to exiting and letting the
    daemon's `--restart unless-stopped` bring the container back.
    Guarded by MX_RESTART_TOKEN, compared in constant time; disabled when unset.
    Scans/thumb workers are in memory, so call it when idle."""
    _admin_ok(request)
    note = {"version": VERSION, "reloading": "execv in place (same PID, same port)"}
    threading.Timer(0.4, _reexec_self).start()            # answer first, then re-exec
    return note


def _reexec_self():
    """Replace this process with a fresh uvicorn on the same port."""
    argv = [sys.executable, "-m", "uvicorn", "app.server:app",
            "--host", "0.0.0.0", "--port", str(PORT)]
    try:
        os.execv(sys.executable, argv)
    except OSError:
        os._exit(0)                                       # last resort: let docker do it


@app.get("/api/tree")
def api_tree(loc: int, parent: int | None = None):
    l = loc_or_404(loc)
    diff_dir(loc, parent, l["root"], sweep=True, quick=True)
    rows = child_nodes(loc, parent, kinds=("dir", "zip"))
    return [{"id": r["id"], "kind": r["kind"], "name": r["name"]}
            for r in natsorted(rows, key=lambda r: r["name"])]


@app.get("/api/children")
def api_children(loc: int, parent: int | None = None, sort: str = "name",
                 nav: int = 0):
    l = loc_or_404(loc)
    parent = parent or None          # parent=0 is the REST idiom for "root"
    if parent is not None and not node_by_id(parent):
        # index rebuilt or the folder was renamed/deleted under the tree: the page
        # must hear about it rather than showing an empty pane
        log.warning("children: stale parent id %s (loc %s)", parent, loc)
        raise HTTPException(404, "unknown folder")   # index rebuilt: refresh the tree
    diff_dir(loc, parent, l["root"], sweep=True, quick=True)
    # eager inline generation for small backlogs (new additions appear at once)
    pend = q("SELECT id FROM nodes WHERE loc_id=? AND parent_id IS ? "
             f"AND kind='image' AND ({THUMB_WORK})", (loc, parent))
    if pend:
        # Build in the background and answer straight away: the client renders
        # placeholders and polls, so a cold folder never looks like a freeze.
        # (One pack write for the whole folder, deduped while in flight.)
        queue_folder_pack(loc, parent)
    rows = child_nodes(loc, parent)
    queue_durations(rows)               # clips listed before lengths were stored
    if nav:
        # only a real navigation logs; the 1.5s re-polls while thumbnails land pass nav=0
        crumbs = breadcrumb(l, parent)
        label = " / ".join([l["name"]] + [c["name"] for c in crumbs[1:]]) if crumbs else l["name"]
        _log_open(label, len(pend))
    row_here = node_by_id(parent) if parent is not None else None   # rel for the pin
    return {"path": breadcrumb(l, parent),
            "rel": (row_here["rel"] if row_here is not None else ""),
            "items": [item_payload(r) for r in nsort(rows, sort)]}


# --------------------------------------------------------- per-folder view pins
# A folder can remember its own tile size, sort order and animation state. The
# client pulls every pin once at boot (rows are tiny) so navigating into a pinned
# folder needs no extra round trip, and writes one row on pin/change.
@app.get("/api/prefs")
def api_prefs(loc: int | None = None):
    return {"items": [{"loc": r["loc_id"], "rel": r["rel"], "size": r["size"],
                       "sort": r["sort"],
                       "anim": None if r["anim"] is None else bool(r["anim"])}
                      for r in prefs_list(loc)]}


@app.put("/api/pref")
async def api_pref_put(request: Request):
    """Pin or update one folder. Every field is optional: a null keeps the stored
    value, so a client that only moved the slider cannot wipe the saved sort."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad json")
    loc = int(body.get("loc") or 0)
    loc_or_404(loc)                     # never file a pin against a stale location
    rel = str(body.get("rel") or "")
    size, sort, anim = body.get("size"), body.get("sort"), body.get("anim")
    if size is not None:
        size = max(80, min(600, int(size)))
    if sort is not None and sort not in ("name", "date"):
        raise HTTPException(400, "bad sort")
    if anim is not None:
        anim = 1 if anim else 0
    pref_set(loc, rel, size, sort, anim)
    return {"ok": True, "pref": {"loc": loc, "rel": rel, "size": size, "sort": sort,
                                 "anim": None if anim is None else bool(anim)}}


@app.delete("/api/pref")
def api_pref_del(loc: int, rel: str = ""):
    loc_or_404(loc)
    pref_drop(loc, rel)
    return {"ok": True}


# ------------------------------------------------------------- favourites menu
# The star in the top bar: a short list of folders (or zips, or a location root) to
# jump to. Stored by PATH, so a re-index that renumbers nodes keeps them; each GET
# resolves the path to today's node id, and a favourite whose folder has gone is
# returned flagged `missing` (shown greyed with its x still there) rather than dropped.
@app.get("/api/favs")
def api_favs():
    out = []
    for f in q("SELECT * FROM favourite ORDER BY added, id"):
        lrow = one("SELECT * FROM locations WHERE id=?", (f["loc_id"],))
        if not lrow:
            continue
        rel = f["rel"] or ""
        node = None
        if rel:
            node = one("SELECT id, kind FROM nodes WHERE loc_id=? AND rel=?", (f["loc_id"], rel))
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        out.append({"id": f["id"], "loc": f["loc_id"], "rel": rel, "kind": f["kind"],
                    "name": rel.rsplit("/", 1)[-1] if rel else lrow["name"],
                    "where": lrow["name"] + (" / " + parent.replace("/", " / ") if parent else "")
                             if rel else "",
                    "node": node["id"] if node else None,
                    "missing": bool(rel) and node is None})
    return {"items": out}


@app.post("/api/favs")
async def api_favs_add(request: Request):
    """Body: {"loc": <id>} for a location root, or {"node": <id>} for a folder/zip."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad json")
    nid = body.get("node")
    if nid:
        r = node_or_404(int(nid))
        if r["kind"] not in ("dir", "zip"):
            raise HTTPException(400, "only folders and zips can be favourites")
        loc, rel, kind = r["loc_id"], r["rel"] or "", r["kind"]
    else:
        loc = int(body.get("loc") or 0)
        loc_or_404(loc)
        rel, kind = "", "loc"
    exec_many("INSERT OR IGNORE INTO favourite(loc_id, rel, kind, added) VALUES(?,?,?,?)",
              (loc, rel, kind, time.time()))
    return api_favs()


@app.delete("/api/favs/{fav_id}")
def api_favs_del(fav_id: int):
    exec_many("DELETE FROM favourite WHERE id=?", (fav_id,))
    return api_favs()


@app.get("/api/zip/{zip_id}/defects")
def api_zip_defects(zip_id: int):
    """Entries inside one archive that could not be decoded, with the reason."""
    row = node_or_404(zip_id)
    rows = q("SELECT * FROM zip_defect WHERE zip_id=? ORDER BY entry", (zip_id,))
    return {"zip_id": zip_id, "name": row["name"], "rel": row["rel"],
            "count": len(rows),
            "items": [{"entry": r["entry"] or row["name"],
                       "scope": r["scope"], "reason": r["reason"],
                       "size": r["size"], "detected_at": r["detected_at"]} for r in rows]}


@app.get("/api/defects")
def api_defects(format: str = "json"):
    """Every flagged zip in the library: the list to hold against a backup copy.

    One row per zip with its path from the location root, so the same file can be
    found on another NAS by name; ?format=csv gives the sheet to work from.
    """
    rows = q("""SELECT n.id, n.name, n.rel, COALESCE(n.bad_count, 0) AS bad,
                       n.size, n.mtime, l.name AS loc_name, l.root AS loc_root
                FROM nodes n JOIN locations l ON l.id = n.loc_id
                WHERE n.kind='zip' AND COALESCE(n.bad_count, 0) > 0
                ORDER BY l.name, n.rel""")
    if format == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["location", "folder", "zip_file", "bad_entries", "size", "modified"])
        for r in rows:
            rel = r["rel"] or r["name"] or ""
            folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
            w.writerow([r["loc_name"], folder, r["name"], r["bad"], r["size"] or 0,
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"] or 0))])
        return PlainTextResponse(out.getvalue(), media_type="text/csv",
                                 headers={"content-disposition":
                                          'attachment; filename="mediaexplorer-defects.csv"'})
    return {"count": len(rows),
            "items": [{"id": r["id"], "location": r["loc_name"], "root": r["loc_root"],
                       "rel": r["rel"], "name": r["name"], "bad": r["bad"],
                       "size": r["size"], "mtime": r["mtime"]} for r in rows]}


@app.get("/api/zip/{zip_id}/entries")
def api_zip_entries(zip_id: int, sort: str = "name", nav: int = 0):
    row = node_or_404(zip_id)
    if index_zip(zip_id) is None:
        raise HTTPException(422, "cannot read zip")
    # entries encoded by an older generation are re-pended when the zip is opened
    exec_many("UPDATE zip_entries SET thumb_state='pending', pack_id=NULL,"
              " pack_off=NULL, pack_len=NULL WHERE zip_id=? AND thumb_state='ready'"
              " AND COALESCE(thumb_gen, 0) < ?", (zip_id, THUMB_GEN))
    pend = q("SELECT id FROM zip_entries WHERE zip_id=? "
             f"AND kind='image' AND ({THUMB_WORK})", (zip_id,))
    if pend:
        # background, deduped: 200 separate per-entry jobs would each rewrite the
        # same pack (O(n^2) writes), so one job builds the whole zip
        queue_zip_pack(zip_id)
    rows = q("SELECT * FROM zip_entries WHERE zip_id=? AND kind IN ('image','video')", (zip_id,))
    if nav:
        lrow = one("SELECT * FROM locations WHERE id=?", (row["loc_id"],))
        _log_open(f"{lrow['name'] if lrow else '?'} / {row['rel']}", len(pend), kind="zip")
    ordered = natsorted(rows, key=lambda r: r["name"])
    if sort == "date":
        ordered = sorted(ordered, key=lambda r: -r["size"])  # entries keep zip time
    zv = vstamp(row)                     # id reuse after a rebuild: bust the cache
    out = []
    defects = {d["entry"]: d["reason"] for d in
               q("SELECT entry, reason FROM zip_defect WHERE zip_id=?", (zip_id,))}
    for r in ordered:
        # zip_entry rides on every entry from the server. The page used to stamp it on
        # only when it first painted a zip; a re-list while thumbnails were building
        # replaced state.items WITHOUT it, and the viewer then fetched
        # /api/file/node/<entryId> -- a different picture whenever a real node has that
        # id (entry 54 = Image225.jpg in 12.zip, node 54 = Image159.jpg). Found 0.1.244.
        out.append({"id": r["id"], "kind": r["kind"], "name": r["name"], "size": r["size"],
                    "zip_entry": True,
                    "mtime": zv,          # the zip's stamp: used to version the file URL
                    "thumb": (f"/api/thumb/entry/{r['id']}?v={thumb_vstamp(r)}"
                              if r["thumb_state"] == "ready" else None),
                    "pending": r["thumb_state"] == "pending",
                    # unreadable bytes: the tile marks it so a hole in the grid is
                    # explained rather than looking like a thumbnail that never came
                    "bad": r["name"] in defects,
                    "why": defects.get(r["name"])})
    return {"path": breadcrumb(one("SELECT * FROM locations WHERE id=?", (row["loc_id"],)),
                               row["parent_id"]) + [{"id": zip_id, "name": row["name"], "kind": "z"}],
            "rel": row["rel"],
            "items": out, "has_video": zip_has_video(zip_id)}


# ---------------------------------------------------------------- thumbs & files

def _pack_bytes(pack_id, name):
    """Thumb bytes from a pack (hot packs come from RAM). None if unavailable."""
    if not pack_id:
        return None
    prow = one("SELECT path FROM thumb_pack WHERE id=?", (pack_id,))
    if not prow:
        return None
    try:
        return packs.item(prow["path"], name)
    except (packs.PackError, OSError, KeyError):
        return None


def _thumb_cache(versioned):
    """A versioned thumbnail URL names one exact set of bytes for ever: the stamp
    changes whenever the thumb is rebuilt (gen, pack offset, store epoch), so the
    browser may keep it permanently and never revalidate. It used to be a day, after
    which every folder re-downloaded every thumbnail it had already shown. A bare URL
    (no ?v=) keeps the old one-day rule."""
    return ("public, max-age=31536000, immutable" if versioned
            else "public, max-age=86400")


def _thumb_response(row, name, versioned=False):
    blob = None
    if row["pack_id"]:
        blob = _pack_bytes(row["pack_id"], name)
    if blob:
        return Response(blob, media_type="image/jpeg",
                        headers={"Cache-Control": _thumb_cache(versioned)})
    if row["thumb"] and os.path.exists(row["thumb"]):   # pre-pack library
        return FileResponse(row["thumb"], media_type="image/jpeg",
                            headers={"Cache-Control": _thumb_cache(versioned)})
    # The row thought it was done but cannot serve a byte (pack row or file gone).
    # Re-pend it so the next folder/zip open rebuilds instead of 404ing forever.
    tbl = "zip_entries" if "zip_id" in row.keys() else "nodes"
    exec_many(f"UPDATE {tbl} SET pack_id=NULL, pack_off=NULL, pack_len=NULL,"
              f" thumb_state='pending' WHERE id=?", (row["id"],))
    if tbl == "zip_entries":
        _repend_missing_entries(row["zip_id"], row["pack_id"])
        queue_zip_pack(row["zip_id"])
    else:
        # A node row had nothing to do with it: the folder pack was rewritten
        # underneath it. Re-pend the siblings and QUEUE the rebuild -- without
        # this the row stayed pending and the tile 404'd on every load.
        _repend_missing_nodes(row["loc_id"], row["parent_id"], row["pack_id"])
        # queue_folder_pack only encodes images, so a video poster or a zip 4-up
        # has to be asked for by name or the row waits for a click that never comes
        _queue_node_build(row)
        queue_folder_pack(row["loc_id"], row["parent_id"])
    return Response(status_code=404, headers={"Cache-Control": "no-store"})


@app.get("/api/diag/entry/{entry_id}")
def api_diag_entry(entry_id: int):
    """Why a zip entry thumbnail 404s: is its name actually a member of its pack?"""
    r = one("SELECT * FROM zip_entries WHERE id=?", (entry_id,))
    if not r:
        return {"entry": entry_id, "exists": False}
    out = {"entry": entry_id, "name": r["name"], "thumb_state": r["thumb_state"],
           "pack_id": r["pack_id"], "zip_id": r["zip_id"],
           "same_name_siblings": one("SELECT COUNT(*) c FROM zip_entries "
                                     "WHERE zip_id=? AND name=?", (r["zip_id"], r["name"]))["c"]}
    if r["pack_id"]:
        prow = one("SELECT path FROM thumb_pack WHERE id=?", (r["pack_id"],))
        out["pack_path"] = prow["path"] if prow else None
        if prow and os.path.exists(prow["path"]):
            try:
                _b, index, _d = packs.read(prow["path"])
                out["pack_members"] = len(index)
                out["name_in_index"] = r["name"] in index
                out["index_sample"] = list(index)[:6]
            except Exception as e:
                out["pack_error"] = repr(e)[:120]
        else:
            out["pack_missing"] = True
    return out


def _queue_node_build(row):
    """Ask for the build a re-pended node needs (folder packs skip these kinds)."""
    try:
        if row["kind"] == "video":
            queue_video_poster(row["id"])
        elif row["kind"] == "zip":
            queue_zip_tile(row["id"])
    except Exception:
        log.warning("thumb: could not queue rebuild for node %s", row["id"])


def _member_name(row):
    """The pack member a node's thumbnail lives under (see api_thumb_node)."""
    if row["kind"] == "video":
        return f"v{row['id']}/{POSTER_NAME}"
    if row["kind"] == "zip":
        return ZIP_TILE
    return row["name"]


def _pack_index(pack_id):
    if not pack_id:
        return set()
    prow = one("SELECT path FROM thumb_pack WHERE id=?", (pack_id,))
    if not prow or not os.path.exists(prow["path"]):
        return set()
    try:
        _b, idx, _d = packs.read(prow["path"])
        return set(idx)
    except Exception:
        return set()


def _repend_missing_nodes(loc_id, parent_id, pack_id=None):
    """Same story as zip entries, for the folder pack: one dead node means the
    pack was rewritten beneath its rows, so every 'ready' sibling whose member is
    gone is stale too -- re-pend them all and let one folder build heal them."""
    index = _pack_index(pack_id)
    rows = q("SELECT * FROM nodes WHERE loc_id=? AND parent_id IS ? AND thumb_state='ready'"
             " AND kind IN ('image','video','zip')", (loc_id, parent_id)) or []
    miss = [r["id"] for r in rows if _member_name(r) not in index]
    for rid in miss:
        exec_many("UPDATE nodes SET pack_id=NULL, pack_off=NULL, pack_len=NULL,"
                  " thumb_state='pending' WHERE id=?", (rid,))
    for r in rows:
        if r["id"] in miss:
            _queue_node_build(r)
    if miss:
        log.info("thumb: folder %s/%s has %d stale nodes (pack %s) -> repended",
                 loc_id, parent_id, len(miss), pack_id)
    return len(miss)


def _repend_missing_entries(zip_id, pack_id=None):
    """Rows that say 'ready' but whose member is not in the pack are stale.

    One dead entry means the pack was rewritten underneath its rows (rebuild or
    corruption recovery), so its siblings are stale too. Re-pending only the one
    the browser happened to ask for meant a zip of 200 entries healed 200 times,
    one 404 per click. Re-pend every 'ready' entry whose name the pack does not
    carry, so a single rebuild fixes the batch.
    """
    if not zip_id:
        return 0
    prow = one("SELECT path FROM thumb_pack WHERE id=?", (pack_id,)) if pack_id else None
    index = set()
    if prow and os.path.exists(prow["path"]):
        try:
            _b, idx, _d = packs.read(prow["path"])
            index = set(idx)
        except Exception:
            index = set()
    rows = q("SELECT id, name FROM zip_entries WHERE zip_id=? AND thumb_state='ready'",
             (zip_id,)) or []
    miss = [r["id"] for r in rows if r["name"] not in index]
    for rid in miss:
        exec_many("UPDATE zip_entries SET pack_id=NULL, pack_off=NULL, pack_len=NULL,"
                  " thumb_state='pending' WHERE id=?", (rid,))
    if miss:
        log.info("thumb: zip %s has %d stale entries (pack %s) -> repended",
                 zip_id, len(miss), pack_id)
    return len(miss)


@app.get("/api/diag/node/{node_id}")
def api_diag_node(node_id: int):
    r = node_by_id(node_id)
    if not r:
        return {"node": node_id, "exists": False}
    name = _member_name(r)
    out = {"node": node_id, "name": r["name"], "kind": r["kind"],
           "loc": r["loc_id"], "parent": r["parent_id"], "rel": r["rel"],
           "thumb_state": r["thumb_state"], "pack_id": r["pack_id"],
           "thumb_gen": r["thumb_gen"], "gen_needed": THUMB_GEN,
           "member_name": name,
           "source_exists": os.path.exists(node_abspath(r))}
    idx = _pack_index(r["pack_id"])
    out["pack_members"] = len(idx)
    out["name_in_index"] = name in idx
    if name in idx:                       # what generation the BYTES on disk are
        try:
            _b, _i, doc = packs.read(out["pack_path"])
            out["member_gen"] = packs.member_gen(doc, name)
        except Exception:
            out["member_gen"] = None
    prow = one("SELECT path FROM thumb_pack WHERE id=?", (r["pack_id"],)) if r["pack_id"] else None
    out["pack_path"] = prow["path"] if prow else None
    out["pack_file_exists"] = bool(prow and os.path.exists(prow["path"]))
    return out


def _entry_row(node_id: int):
    """A zip-entry id asked for through a node route.

    Entry ids and node ids live in different tables, but an old browser tab (cached JS)
    builds /api/file/node/<entryId> for images inside a zip, and every click 404'd in the
    access log while the picture never appeared. Serving the entry is harmless: real node
    ids are resolved first, so this only answers ids that are not nodes at all."""
    return one("SELECT * FROM zip_entries WHERE id=?", (node_id,))


@app.get("/api/thumb/node/{node_id}")
def api_thumb_node(node_id: int, v: str | None = None):
    r = node_by_id(node_id)
    if not r:
        er = _entry_row(node_id)
        if er:
            return _thumb_response(er, er["name"], bool(v))
        raise HTTPException(404)
    # a video's poster is a NAMESPACED member of its folder pack ("v<id>/poster.jpg"),
    # not a member named after the file
    if r["kind"] == "video":
        name = f"v{r['id']}/{POSTER_NAME}"
    elif r["kind"] == "zip":
        name = ZIP_TILE
    else:
        name = r["name"]
    return _thumb_response(r, name, bool(v))


@app.get("/api/thumb/entry/{entry_id}")
def api_thumb_entry(entry_id: int, v: str | None = None):
    r = one("SELECT * FROM zip_entries WHERE id=?", (entry_id,))
    if not r:
        raise HTTPException(404)
    return _thumb_response(r, r["name"], bool(v))


def _mt(path):
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


@app.get("/api/file/node/{node_id}")
def api_file_node(node_id: int, request: Request):
    r = node_by_id(node_id)
    if not r:
        er = _entry_row(node_id)
        if er:
            return _file_entry_response(er, request)
        raise HTTPException(404)
    p = node_abspath(r)
    if not os.path.isfile(p):
        raise HTTPException(404)
    return FileResponse(p, media_type=_mt(p), filename=os.path.basename(p),
                        headers={"Cache-Control": "no-cache"},  # revalidate: ids are reused
                        content_disposition_type="inline")   # <video> streams, no download prompt


# ------------------------------------------------------------ picture info (I key)
def _info_or_basic(source, name, size):
    """The viewer's info panel. A file Pillow cannot open still answers with what the
    index knows, so the panel says "no metadata" instead of an error chip."""
    try:
        return picture_info(source, name, size)
    except Exception as e:
        log.info("info: cannot read %s: %s", name, e)
        return {"name": name, "size": size, "error": "no readable metadata"}


@app.get("/api/info/node/{node_id}")
def api_info_node(node_id: int):
    r = node_by_id(node_id)
    if not r or r["kind"] not in ("image", "video"):
        raise HTTPException(404)
    p = node_abspath(r)
    if not os.path.isfile(p):
        raise HTTPException(404)
    if r["kind"] == "video":
        # one demux-only ffprobe, outside the encode gate (headers only, tens of ms)
        return video_info(p, r["name"], r["size"])
    return _info_or_basic(p, r["name"], r["size"])


@app.get("/api/info/entry/{entry_id}")
def api_info_entry(entry_id: int):
    er = one("SELECT * FROM zip_entries WHERE id=?", (entry_id,))
    if not er:
        raise HTTPException(404)
    zr = node_or_404(er["zip_id"])
    try:
        data = zip_entry_bytes(zr, er)
    except (OSError, ValueError, KeyError):
        raise HTTPException(422, "cannot read entry")
    return _info_or_basic(data, os.path.basename(er["name"]), er["size"])


@app.get("/api/file/entry/{entry_id}")
def api_file_entry(entry_id: int, request: Request):
    er = one("SELECT * FROM zip_entries WHERE id=?", (entry_id,))
    if not er:
        raise HTTPException(404)
    return _file_entry_response(er, request)


def _file_entry_response(er, request=None):
    """A picture inside a zip. It carries an ETag now: with `no-cache` and no validator
    the browser had to download the whole entry again every time it re-checked (the
    viewer's look-ahead was being thrown away for zips -- measured 0.1.243). The tag is
    the zip's identity (id, size, mtime) plus the entry's, so a rewritten zip is a new
    tag; a match is answered 304 before the zip is even opened."""
    zr = node_or_404(er["zip_id"])
    etag = (f'"z{zr["id"]}-{int(zr["size"] or 0)}-{int(float(zr["mtime"] or 0))}'
            f'-e{er["id"]}-{int(er["size"] or 0)}"')
    cache = {"ETag": etag, "Cache-Control": "no-cache"}
    if request is not None and request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cache)
    try:
        data = zip_entry_bytes(zr, er)
    except (OSError, ValueError, KeyError):
        raise HTTPException(422, "cannot read entry")
    name = os.path.basename(er["name"])
    return Response(data, media_type=_mt(name),
                    headers={"Content-Disposition": f'inline; filename="{name}"', **cache})


@app.get("/api/frames/{node_id}")
def api_frames(node_id: int, build: bool = False):
    """The filmstrip, READ-ONLY by default.

    Reading must never start work: a mouse sweeping a grid of clips would spawn
    9 ffmpeg calls per tile. Builds happen when asked (context menu ->
    POST /api/build/frames) or by opting in with MX_HOVER_BUILD=1."""
    r = node_or_404(node_id)
    if r["kind"] != "video":
        raise HTTPException(400, "not a video")
    names = existing_frames(node_id)
    if not names and (build or HOVER_BUILD):
        names = extract_frames(node_id)
    if not names:
        # Not an error. A clip that has never been built simply has no frames, and
        # that is the normal state of most of the library -- answering 422 made every
        # hover over an unbuilt tile log "Failed to load resource: 422" in the browser
        # and sent us hunting a phantom bug. An empty list says the same thing politely.
        return {"frames": []}
    return {"frames": [f"/api/framefile/{node_id}/{n}?v={thumb_vstamp(r)}" for n in names]}


@app.get("/api/framefile/{node_id}/{name}")
def api_framefile(node_id: int, name: str, v: str | None = None):
    r = node_or_404(node_id)
    if "/" in name or ".." in name:
        raise HTTPException(400)
    blob = video_member_bytes(r, name)             # member of the folder pack
    if blob:
        return Response(blob, media_type="image/jpeg",
                        headers={"Cache-Control": _thumb_cache(bool(v))})
    p = frames_dir(r) / name                       # pre-pack filmstrips
    if not p.exists():
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": _thumb_cache(bool(v))})


@app.get("/api/vlc/{node_id}")
def api_vlc(node_id: int, request: Request):
    r = node_or_404(node_id)
    if r["kind"] != "video":
        raise HTTPException(400, "not a video")
    base = str(request.base_url).rstrip("/")
    m3u = f"#EXTM3U\n#EXTINF:-1,{r['name']}\n{base}/api/file/node/{node_id}\n"
    safe = os.path.splitext(r["name"])[0].replace('"', "") + ".m3u"
    return Response(m3u, media_type="audio/x-mpegurl",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


# ---------------------------------------------------------------- client telemetry

# The UI is a single page and the interesting bugs are races between a click, a
# slow folder listing and the tree reveal. Browser console output is invisible
# after the fact, so the page posts nav events here: they land in `docker logs`
# and in a ring buffer that /api/log serves back for a post-mortem. Errors are
# always sent; the chatty per-request events need ?log=1 in the page URL (or
# MX_CLIENT_LOG=0 to silence stdout).
_client_log = deque(maxlen=1000)
CLIENT_LOG_ON = os.environ.get("MX_CLIENT_LOG", "1").strip().lower() not in ("0", "false", "no", "off")
_CLIENT_BORING = {"poll"}          # events that would flood the buffer


@app.post("/api/log")
async def api_client_log(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {"ev": "raw", "body": (await request.body())[:400].decode("utf-8", "replace")}
    items = body if isinstance(body, list) else [body]
    now = time.time()
    for it in items[:40]:
        if not isinstance(it, dict):
            it = {"ev": "raw", "body": str(it)[:300]}
        it["ts"] = now
        ring(it)
        if CLIENT_LOG_ON and it.get("ev") not in _CLIENT_BORING:
            try:
                log.info("client %s", json.dumps(it, default=str)[:700])
            except Exception:
                pass
    return {"ok": True, "n": len(items)}


@app.get("/api/log/file")
def api_client_log_file(limit: int = 200):
    """Errors recorded across restarts (DATA_PATH/client.log), newest last."""
    p = DATA_PATH / "client.log"
    if not p.exists():
        return {"items": [], "path": str(p), "exists": False}
    out = []
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in deque(fh, maxlen=max(1, min(limit, 2000))):
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except OSError as e:
        return {"items": [], "path": str(p), "error": str(e)}
    return {"items": out, "path": str(p), "exists": True}


@app.get("/api/log")
def api_client_log_read(limit: int = 200, since: float = 0.0, reset: bool = False):
    rows = [r for r in list(_client_log) if r.get("ts", 0) >= since]
    with _nf_lock:
        nf = sorted(_nf.items(), key=lambda kv: -kv[1])
        if reset:
            _nf.clear()
    with _nf_lock:
        recent = [{"ts": t, "path": p, "q": q} for t, p, q in list(_nf_detail)[-60:]]
    return {"now": time.time(), "items": rows[-limit:],
            "not_found": [{"path": k, "n": v} for k, v in nf[:40]],
            "not_found_recent": recent,
            "not_found_total": sum(v for _k, v in nf)}


# ---------------------------------------------------------------- scanning

_scan_thread = None
# Scan requests made while a scan is already running. Newest first: a user who
# navigates somewhere else and presses Scan wants that folder NEXT, not after the
# subtree scan that happens to own the status line.
_scan_queue = deque()
_scan_queue_lock = threading.Lock()
_pump_lock = threading.Lock()
_pump_on = [False]


def _scan_event(msg, **extra):
    """A scan line in BOTH places: the container log (timestamped, for `docker logs`
    / Unraid's log view) and the in-memory ring that GET /api/log serves, so the same
    history is readable from the app itself."""
    log.info("%s", msg)
    try:
        _client_log.append({"ev": "scan", "msg": msg, "ts": time.time(), **extra})
    except Exception:
        pass


def _safe_tile(kind, tid):
    """Recompose one 4-up mosaic; a broken source must not kill the scan."""
    try:
        if kind == "zip":
            gen_zip_tile(tid, force=True)
        else:
            gen_dir_tile(tid, force=True)
    except Exception as e:
        log.warning("tile %s %s failed: %s", kind, tid, e)


def _scan_label(lrow, parent):
    """Human label for a scan: location name plus the folder it is refreshing."""
    if parent:
        r = node_by_id(parent)
        return f"{lrow['name']} / {r['rel'] if r else parent}"
    return f"{lrow['name']} / (root)"


def _pool_report():
    """What this process really sees: a cpuset only lands when the CONTAINER is
    recreated, so /api/config is the quickest way to tell a pin from a wish."""
    try:
        from .config import effective_cpus
        from .media import _executor as _pack_pool, _fast as _fast_pool, VIDEO_JOBS
        return {"cpus": effective_cpus(), "cpus_allowed": sorted(os.sched_getaffinity(0)),
                "pools": {"thumbs": THUMB_WORKERS, "packs": _pack_pool._max_workers,
                          "interactive": _fast_pool._max_workers, "ffmpeg": VIDEO_JOBS}}
    except Exception:
        return {}


def _why_tag(why):
    """How a scan was asked for, spelled the same way everywhere in the log."""
    return f" [{why}]" if why else ""


def _log_open(label, pending, kind="folder"):
    """A folder/zip opened in the browser: what work that navigation just set going."""
    if not pending:
        return
    try:
        _client_log.append({"ev": "open", "ts": time.time(),
                            "msg": f"Opened {label} -- {pending} thumbnail"
                                   f"{'' if pending == 1 else 's'} to build",
                            "folder": label, "pending": pending, "kind": kind})
    except Exception:
        pass


def _run_scan(scans, include_zips=False, why="button"):
    """scans: list of (location_row, parent_id[, deep]) -- one folder each by default.

    The tree is browsed lazily: opening a folder lists that folder only. An ordinary
    scan answers the same question for the folder the user is looking at (pick up
    files added behind the app's back, build the thumbnails it can see). Walking every
    subfolder of a large library would be minutes of spindle I/O for folders nobody
    has opened, so the default is deliberately one level deep.

    deep=True is the deliberate version ("Scan all subfolders" in the context menu):
    the same work in every folder of the subtree, breadth first, so a file dropped
    anywhere below is picked up in one go."""
    global _scan_thread
    with _scan_lock:
        scan_state.update(running=True, phase="discover", folders=0,
                          thumb_total=0, thumb_done=0,
                          anim_total=0, anim_done=0, anim_skipped=0,
                          loc=None, parent=None, zip=None, work_total=0,
                          deep=any(len(s) > 2 and s[2] for s in scans))
    try:
        dropped = sweep_orphans()      # rows left behind by deleted/renamed folders
        if dropped:
            print(f"scan: dropped {dropped} orphan node rows", flush=True)
    except Exception:
        pass
    jobs = []          # (loc_id, parent_id) folder packs to build
    job_work = []      # thumbnails each folder pack will encode
    vids = []          # (video_id, 1 if it needs a poster, else 0)
    zip_ids = []       # zips found while walking, when the walk includes them
    zip_work = []      # entries + its own 4-up, per zip
    tiles_all = []     # (kind, id) mosaics to recompose
    grand_total = 0    # work found so far across the whole walk (monotone)
    scans = [(s[0], s[1], s[2] if len(s) > 2 else False) for s in scans]
    i = 0
    while i < len(scans):
        lrow, parent, deep = scans[i]
        i += 1
        label = _scan_label(lrow, parent)
        t0 = time.time()
        # a subtree walk can cover hundreds of folders: log a child only when it
        # actually found something, or the log becomes a wall of "nothing changed"
        quiet = deep and parent is not None
        if not quiet:
            _scan_event(f"Scanning started: {label}{_why_tag(why)}", folder=label, why=why)
        new_media = diff_dir(lrow["id"], parent, lrow["root"])   # entries only
        # Refresh means refresh: thumbnails this folder holds that were encoded by
        # an older generation are re-pended here. Nowhere else in the app does that
        # happen on its own, so the work stays exactly where the user pointed.
        stale = q("SELECT id, kind FROM nodes WHERE loc_id=? AND parent_id IS ?"
                  " AND thumb_state='ready' AND COALESCE(thumb_gen, 0) < ?",
                  (lrow["id"], parent, THUMB_GEN))
        for r in stale:
            exec_many("UPDATE nodes SET thumb_state='pending', pack_id=NULL,"
                      " pack_off=NULL, pack_len=NULL WHERE id=?", (r["id"],))
        pend_img, kinds, zips, tiles = 0, {}, 0, []   # tiles: (kind, id) 4-up mosaics
        vid_todo = 0
        vids_before = len(vids)
        rows = q("SELECT id,kind,thumb_state,thumb_gen FROM nodes "
                 "WHERE loc_id=? AND parent_id IS ?", (lrow["id"], parent))
        for r in rows:
            k = r["kind"]
            kinds[k] = kinds.get(k, 0) + 1
            if k == "zip":
                index_zip(r["id"])   # metadata only; entry thumbs made on open
                zips += 1
                if include_zips and r["id"] not in zip_ids:
                    # when the walk goes INTO the zips, _refresh_zip_entries rebuilds the
                    # zip's mosaic itself: adding a zip tile here too built every 4-up
                    # twice and counted it twice, so a folder of 4 zips read "x/9"
                    if len(zip_ids) < DEEP_SCAN_MAX:
                        zip_ids.append(r["id"])
                        # the zip counted as ONE item here is exactly the miscount the
                        # progress bar showed: a 300-entry zip read "1 thumbnail". Ask
                        # the database what the refresh will actually encode, the same
                        # predicate _refresh_zip_entries uses, plus 1 for its own 4-up.
                        zip_work.append(1 + _zip_pending_entries(r["id"]))
                elif not include_zips:
                    tiles.append(("zip", r["id"]))
            elif k == "image" and r["thumb_state"] == "pending":
                pend_img += 1        # 'error' rows are not work this scan can finish
            elif k == "video":
                # only a poster that is missing or from an older encode produces work;
                # a current one is adopted and publishes nothing, so counting it would
                # leave the Scan badge stuck above zero for the whole job
                needs = (r["thumb_state"] == "pending"
                         or (r["thumb_state"] == "ready" and (r["thumb_gen"] or 0) < THUMB_GEN))
                vids.append((r["id"], 1 if needs else 0))
                if needs:
                    vid_todo += 1
            elif k == "dir":
                tiles.append(("dir", r["id"]))
        # A folder's 4-up mosaic is built from four pictures inside it, so it goes
        # stale the moment one of them is re-encoded, renamed or replaced -- and the
        # folder's OWN face lives in its parent's pack, which no folder scan would
        # otherwise touch. Every mosaic in view is therefore rebuilt by a scan: four
        # decodes each, on demand, which is exactly what pressing Scan should mean.
        if parent is not None:
            tiles.append(("dir", parent))
        tiles_all.extend(tiles)
        grand_total += pend_img + vid_todo + len(tiles)
        with _scan_lock:
            scan_state.update(loc=lrow["id"], parent=parent, zip=None,
                              work_total=grand_total)
        if pend_img:
            jobs.append((lrow["id"], parent))   # one pack write for the folder
            job_work.append(pend_img)           # ...holding this many thumbnails
        if not quiet or new_media or pend_img or len(vids) > vids_before:
            _scan_event(
                f"Scanning completed: {label} in {time.time() - t0:.1f}s -- "
                f"{len(rows)} entries ({kinds.get('dir', 0)} folders, "
                f"{kinds.get('zip', 0)} zips, {kinds.get('image', 0)} images, "
                f"{kinds.get('video', 0)} videos), {new_media} new, "
                f"{pend_img + (len(vids) - vids_before)} thumbs queued, "
                f"{len(tiles)} tiles refreshed{_why_tag(why)}",
                folder=label, entries=len(rows), new=new_media,
                thumbs=pend_img + (len(vids) - vids_before), tiles=len(tiles), **kinds)
        if deep:
            # queue this folder's subfolders behind the ones already waiting, so a
            # subtree walk is breadth first and the counter climbs as it goes
            for r in q("SELECT id FROM nodes WHERE loc_id=? AND parent_id IS ?"
                       " AND kind='dir' ORDER BY name", (lrow["id"], parent)):
                if len(scans) < DEEP_SCAN_MAX:
                    scans.append((lrow, r["id"], True))
            with _scan_lock:
                scan_state["folders"] = len(scans)
    # The progress bar counts thumbnails, not tasks: a folder pack stands for every
    # image it will encode, a zip for every entry it will rebuild (plus its own 4-up).
    # Counting futures made "3 folders + 9 zips" read as "12 thumbnails" -- the
    # number tracked how many archives were opened, not how much work was left.
    total_thumbs = (sum(job_work) + sum(w for _, w in vids)
                    + len(tiles_all) + sum(zip_work))
    with _scan_lock:
        scan_state.update(phase="thumbs", folders=len(scans),
                          thumb_total=total_thumbs,
                          work_total=max(grand_total + sum(zip_work),
                                         scan_state.get("work_total") or 0))
    from .media import _executor, _safe_folder_pack, ensure_video_poster
    futs = [(_executor.submit(_safe_folder_pack, loc_id, pid), w)
            for (loc_id, pid), w in zip(jobs, job_work)]
    # posters, not filmstrips: one frame per video is the default tile state, and a
    # filmstrip is 9x the decode work for a tile nobody has hovered yet. Hover and
    # right-click "Build filmstrips" make the strips on demand.
    futs += [(_executor.submit(ensure_video_poster, vid), w) for vid, w in vids]
    futs += [(_executor.submit(_safe_tile, kind, tid), 1) for kind, tid in tiles_all]
    if zip_ids:
        # "Scan all subfolders" stopped at folders, so a zip's own entries were never
        # refreshed by a subtree walk; this is the version that goes into them too.
        # add_to_scan=False: those entries are already in the total above, so letting
        # the zip add them again would double the countdown.
        futs += [(_executor.submit(_refresh_zip_entries, zid, False), w)
                 for zid, w in zip(zip_ids, zip_work)]
        _scan_event(f"Scanning {len(zip_ids)} zip archive"
                    f"{'' if len(zip_ids) == 1 else 's'} inside the subtree",
                    zips=len(zip_ids))
    _scan_event(f"Scanning finished: {len(scans)} folder"
                f"{'' if len(scans) == 1 else 's'}, {total_thumbs} thumbnail"
                f"{'' if total_thumbs == 1 else 's'} queued{_why_tag(why)}",
                folders=len(scans), thumbs=total_thumbs, why=why)
    while futs:
        done_now = [f for f in futs if f[0].done()]
        futs = [f for f in futs if not f[0].done()]
        with _scan_lock:
            scan_state["thumb_done"] += sum(w for _, w in done_now)
        if futs:
            time.sleep(0.4)
    # No filmstrip pre-build here: posters are made by the thumbs phase above, and a
    # hover filmstrip is 9x a poster's cost for one tile nobody has hovered yet.
    # On-demand only (hover, or right-click "Build filmstrips").
    with _scan_lock:
        scan_state.update(phase="idle", running=False)
    _scan_thread = None


def _zip_pending_entries(zip_id):
    """How many thumbnails a zip refresh will encode, asked *before* it runs.

    _refresh_zip_entries can only say this after it has re-pended the stale rows, which
    is too late for a walk that has to publish a total up front. The predicate is the
    same one it uses -- work outstanding, or an encode from an older geometry -- so the
    walk counts a 300-entry archive as 300 thumbnails, not as one archive."""
    try:
        r = one(f"SELECT COUNT(*) c FROM zip_entries WHERE zip_id=? AND kind='image'"
                f" AND ({THUMB_WORK} OR (thumb_state='ready'"
                f" AND COALESCE(thumb_gen, 0) < ?))", (zip_id, THUMB_GEN))
        return r["c"] if r else 0
    except Exception as e:
        log.warning("zip %s pending count failed: %s", zip_id, e)
        return 0


def _refresh_zip_entries(zip_id, add_to_scan=False, report=None):
    """Re-read one zip and rebuild whatever it is missing or encoded by an older
    generation, in ONE pack write. Shared by the single-zip scan and the deep scan
    that also walks zips."""
    z = node_by_id(zip_id)
    if not z or z["kind"] != "zip":
        return 0
    exec_many("UPDATE nodes SET zindexed=NULL WHERE id=?", (zip_id,))
    index_zip(zip_id)
    exec_many("UPDATE zip_entries SET thumb_state='pending', pack_id=NULL,"
              " pack_off=NULL, pack_len=NULL WHERE zip_id=? AND thumb_state='ready'"
              " AND COALESCE(thumb_gen, 0) < ?", (zip_id, THUMB_GEN))
    todo = one("SELECT COUNT(*) c FROM zip_entries WHERE zip_id=? AND kind='image'"
               f" AND ({THUMB_WORK})", (zip_id,))["c"]
    if report:
        report(todo)          # publish the size of the job before the first encode
    if add_to_scan and todo:
        # the walk that queued this zip counted the zip as ONE item; its entries are
        # thumbnails too, and the page counts their events, so add them to the total
        # or the countdown reaches zero while hundreds are still being encoded
        with _scan_lock:
            # +1 for the zip's own 4-up, which _refresh_zip_entries rebuilds and publishes
            # as a tile event: without it the walk's countdown hit zero early
            scan_state["work_total"] = (scan_state.get("work_total") or 0) + todo + 1
    done = gen_zip_pack(zip_id, force=True) if todo else 0
    queue_zip_tile(zip_id, force=True)
    return todo, done


def _run_zip_scan(zip_id, status=True, why="button"):
    """Refresh ONE zip: re-read its central directory and rebuild the entry
    thumbnails it holds. The zip equivalent of scanning a folder.

    status=False runs it as a priority request beside a scan that already owns the
    progress line, so the counters are left alone."""
    global _scan_thread
    z0 = node_by_id(zip_id)
    if status:
        with _scan_lock:
            scan_state.update(running=True, phase="thumbs", folders=1, thumb_total=0,
                              thumb_done=0, anim_total=0, anim_done=0, anim_skipped=0,
                              loc=z0["loc_id"] if z0 else None, parent=None,
                              zip=zip_id, work_total=0, deep=False)
    try:
        z = node_by_id(zip_id)
        if not z:
            return
        locrow = one("SELECT * FROM locations WHERE id=?", (z["loc_id"],))
        label = f"{locrow['name'] if locrow else '?'} / {z['rel']}"
        t0 = time.time()
        _scan_event(f"Scanning started: {label}{_why_tag(why)}", folder=label, why=why)
        kinds = {r["kind"]: r["c"] for r in
                 q("SELECT kind, COUNT(*) c FROM zip_entries WHERE zip_id=? GROUP BY kind",
                   (zip_id,))}
        def _report(todo):
            # the total has to be known while the encodes run, so it is handed over
            # the moment _refresh_zip_entries has re-pended the stale rows
            if status:
                with _scan_lock:
                    scan_state.update(thumb_total=todo, thumb_done=0, loc=z["loc_id"],
                                      parent=None, zip=zip_id, work_total=todo)

        # force=True matters: without it a zip whose pack is present is ADOPTED, so
        # pressing Scan re-pended the stale entries and then relinked them without
        # encoding anything -- the zip equivalent of the folder bug fixed in 0.1.131
        todo, done = _refresh_zip_entries(zip_id, report=_report)
        if status:
            with _scan_lock:
                scan_state.update(phase="idle", running=False, thumb_done=done)
        _scan_event(
            f"Scanning completed: {label} in {time.time() - t0:.1f}s -- "
            f"{sum(kinds.values())} entries ({kinds.get('image', 0)} images, "
            f"{kinds.get('video', 0)} videos), {done} of {todo} thumbs rebuilt, "
            f"{todo - done} already current{_why_tag(why)}",
            folder=label, entries=sum(kinds.values()), thumbs=done, **kinds)
        _scan_event(f"Scanning finished: 1 zip, {done} thumbnail"
                    f"{'' if done == 1 else 's'} rebuilt{_why_tag(why)}",
                    folders=1, thumbs=done, why=why)
    except Exception:
        traceback.print_exc()
    finally:
        if status:
            with _scan_lock:
                scan_state.update(phase="idle", running=False)
            _scan_thread = None


DEEP_SCAN_MAX = int(os.environ.get("MX_DEEP_MAX", "4000"))
QUICK_SCAN_MAX = int(os.environ.get("MX_QUICK_MAX", "200"))


def _queue_priority(items):
    """Put scan requests at the FRONT of the queue and start the pump.

    Before this, /api/scan answered {"started": false} whenever a scan was running,
    so pressing Scan during a long job did nothing at all -- the folder on screen
    waited for the whole subtree."""
    with _scan_queue_lock:
        for it in items:
            try:
                _scan_queue.remove(it)      # same folder asked twice: one entry
            except ValueError:
                pass
            if it in _scan_queue:           # belt and braces: never two entries for one job
                continue
            _scan_queue.appendleft(it)      # newest first
    _start_scan_pump()
    return {"started": True, "queued": "front", "scope": "folder"}


def _start_scan_pump():
    with _pump_lock:
        if _pump_on[0]:
            return
        _pump_on[0] = True
    threading.Thread(target=_scan_pump, daemon=True, name="scan-priority").start()


def _scan_pump():
    """Drain priority requests newest-first, beside whatever scan is running."""
    while True:
        with _scan_queue_lock:
            if not _scan_queue:
                with _pump_lock:
                    _pump_on[0] = False
                    restart = bool(_scan_queue)      # arrived while we were leaving
                    if restart:
                        _pump_on[0] = True
                if not restart:
                    return
            item = _scan_queue.popleft()
            kind, a, b, c = item[:4]
            with_zips = bool(item[4]) if len(item) > 4 else False
            why = item[5] if len(item) > 5 else "queue"
        try:
            if kind == "zip":
                _run_zip_scan(a, status=False, why=why)   # the other scan owns the status
            else:
                _quick_scan(a, b, c, with_zips, why)
        except Exception:
            traceback.print_exc()


def _quick_scan(lrow, parent, deep=False, include_zips=False, why="button"):
    """Discovery plus thumbnails for ONE folder, while a full scan runs elsewhere.

    The same work as one pass of _run_scan's loop, except the pack build and the
    folder mosaics go to the FRONT of their queues, and the running scan's progress
    counters are left alone -- it keeps reporting its own job."""
    from .media import submit_folder_pack
    todo = deque([(parent, deep)])
    seen = 0
    while todo and seen < QUICK_SCAN_MAX:
        pid, dp = todo.popleft()
        seen += 1
        label = _scan_label(lrow, pid)
        t0 = time.time()
        _scan_event(f"Scanning started (jumped the queue): {label}"
                    f"{_why_tag(why)}", folder=label, why=why)
        try:
            new_media = diff_dir(lrow["id"], pid, lrow["root"])
        except Exception:
            traceback.print_exc()
            continue
        stale = q("SELECT id FROM nodes WHERE loc_id=? AND parent_id IS ?"
                  " AND thumb_state='ready' AND COALESCE(thumb_gen, 0) < ?",
                  (lrow["id"], pid, THUMB_GEN))
        for r in stale:
            exec_many("UPDATE nodes SET thumb_state='pending', pack_id=NULL,"
                      " pack_off=NULL, pack_len=NULL WHERE id=?", (r["id"],))
        rows = q("SELECT id,kind FROM nodes WHERE loc_id=? AND parent_id IS ?",
                 (lrow["id"], pid))
        dirs = []
        for r in rows:
            if r["kind"] == "dir":
                queue_dir_tile(r["id"], front=True)        # its face, from this view
                if dp:
                    dirs.append(r["id"])
            elif r["kind"] == "zip":
                queue_zip_tile(r["id"])
                if include_zips and dp:
                    _executor.submit(_refresh_zip_entries, r["id"], True)
        if pid is not None:
            queue_dir_tile(pid, front=True)                # the folder's own face
        pend = one("SELECT COUNT(*) c FROM nodes WHERE loc_id=? AND parent_id IS ?"
                   f" AND kind='image' AND ({THUMB_WORK})", (lrow["id"], pid))["c"]
        if pend or stale:
            submit_folder_pack(lrow["id"], pid, front=True)
        _scan_event(
            f"Scanning completed: {label} in {time.time() - t0:.1f}s -- "
            f"{len(rows)} entries, {new_media} new, {pend} thumbs queued "
            f"(front of queue)", folder=label, entries=len(rows), new=new_media,
            thumbs=pend)
        if dp:
            todo.extend((d, True) for d in dirs)


@app.post("/api/scan")
def api_scan(loc: int | None = None, parent: int | None = None,
             zip: int | None = None, deep: bool = False, zips: bool = False,
             why: str = "button"):
    """Refresh one folder -- the one the user is looking at.

    No loc: each location's root, still one level deep. Never recursive: a big
    library is browsed lazily, so scanning subfolders nobody opened is wasted I/O."""
    if zip is not None:                 # scanning from inside a zip
        node_or_404(zip)
        with _scan_lock:
            running = scan_state["running"]
            busy_here = running and scan_state.get("zip") == zip
        if busy_here:
            # A second press used to queue a SECOND _run_zip_scan for the same zip: two
            # threads re-pending and re-encoding the same entries, publishing every thumb
            # twice, while the page counted each event -- the number ran to zero at double
            # speed and the job restarted. One target, one job.
            return {"started": True, "queued": "already running", "scope": "zip"}
        if running:
            return _queue_priority([("zip", zip, None, False, False, why)])

        threading.Thread(target=_run_zip_scan, args=(zip, True, why), daemon=True).start()
        return {"started": True, "scope": "zip"}
    if loc is not None:
        lrows = [loc_or_404(loc)]
    else:
        lrows = q("SELECT * FROM locations")
    with _scan_lock:
        running = scan_state["running"]
        busy_here = (running and not scan_state.get("zip") and not scan_state.get("deep")
                     and scan_state.get("loc") in {l["id"] for l in lrows}
                     and scan_state.get("parent") == (parent or None))
    if busy_here:
        return {"started": True, "queued": "already running",
                "scope": "folder"}
    if running:
        return _queue_priority([("folder", l, parent or None, deep, zips and deep, why)
                                for l in lrows])
    threading.Thread(target=_run_scan,
                     args=([(l, parent or None, deep) for l in lrows], zips and deep, why),
                     daemon=True).start()
    return {"started": True, "scope": "subtree+zips" if (deep and zips)
            else "subtree" if deep else "folder"}


@app.post("/api/rescan/{node_id}")
def api_rescan(node_id: int, deep: bool = False, zips: bool = False,
               why: str = "menu"):
    r = node_or_404(node_id)
    l = loc_or_404(r["loc_id"])
    # anything that is not a directory (zip, image, video) is scanned via the folder
    # that holds it -- "scan this" on a file means "refresh the folder it lives in"
    parent = node_id if r["kind"] == "dir" else r["parent_id"]
    with _scan_lock:
        running = scan_state["running"]
    if running:
        return _queue_priority([("folder", l, parent, deep, zips and deep, why)])
    threading.Thread(target=_run_scan,
                     args=([(l, parent, deep)], zips and deep, why), daemon=True).start()
    return {"started": True, "scope": "subtree+zips" if (deep and zips)
            else "subtree" if deep else "folder"}


@app.post("/api/rebuild/thumbs/{node_id}")
def api_rebuild_thumbs(node_id: int):
    """Rebuild ONE folder's (or one zip's) thumbnails, by hand.

    Nothing in the app invalidates thumbnails on its own: a new geometry or a
    nicer tile is rolled out folder by folder, so the cost and the surprise both
    stay where the user asked for them."""
    return rebuild_scope(node_id)


# ----------------------------------------------------- deep (subfolder) re-encode
# "Re-encode thumbnails here" is deliberately one folder, because nothing in this
# app regenerates a thumbnail on its own. Rolling a tile change across a library
# therefore meant right-clicking every folder. This is the same work applied to a
# whole subtree, breadth first, in a background thread, ONE folder in flight at a
# time: a 300-folder subtree cannot queue 300 packs at once (that is what took the
# CPUs to full tilt), and the pass can be stopped between folders.
_deep = {"running": False, "folders": 0, "done": 0, "current": "", "current_id": 0,
         "items": 0, "started": 0.0, "stopped": False, "since": 0.0}
_deep_lock = threading.Lock()


def _subtree_dirs(loc_id, parent_id):
    """Every folder below one folder (and the folder itself), breadth first."""
    order = [] if parent_id is None else [parent_id]
    queue = deque([(loc_id, parent_id)])
    while queue:
        l, p = queue.popleft()
        for r in q("SELECT id FROM nodes WHERE loc_id=? AND parent_id IS ?"
                   " AND kind='dir' ORDER BY name", (l, p)):
            if r["id"] not in order:
                order.append(r["id"])
                queue.append((l, r["id"]))
    return order


def _folder_label(fid):
    r = node_by_id(fid)
    if not r:
        return str(fid)
    loc = one("SELECT name FROM locations WHERE id=?", (r["loc_id"],))
    return f"{loc['name']}/{r['rel']}" if loc and r["rel"] else (r["rel"] or str(fid))


def _pending_in(fid):
    """Thumbnails this folder still owes: what one unit of re-encode is waiting on."""
    return one("SELECT COUNT(*) c FROM nodes WHERE parent_id=? AND thumb_state='pending'",
               (fid,))["c"]


def _run_deep_rebuild(ids):
    try:
        for n, fid in enumerate(ids):
            with _deep_lock:
                if _deep["stopped"]:
                    break
                _deep.update(current=_folder_label(fid), current_id=fid, done=n,
                             since=time.time())
            try:
                rebuild_scope(fid)
            except Exception as e:
                log.warning("deep rebuild: %s failed: %s", fid, e)
                continue
            last, stuck = None, time.time()
            while True:
                with _deep_lock:
                    if _deep["stopped"]:
                        break
                left = _pending_in(fid)
                if left == 0:
                    break
                if left != last:
                    last, stuck = left, time.time()
                elif time.time() - stuck > 300:
                    # one wedged encode must not strand the whole walk
                    log.warning("deep rebuild: %s left %s pending after 5m, moving on", fid, left)
                    break
                time.sleep(0.5)
    finally:
        with _deep_lock:
            # the loop index is 0-based: the last folder never bumps it, so a finished
            # pass would read 33/34 and look like it gave up early
            if not _deep["stopped"]:
                _deep["done"] = _deep["folders"]
            _deep["running"] = False
        _scan_event(f"Re-encode finished: {_deep['done']} of {_deep['folders']} folders"
                    + (" (stopped)" if _deep["stopped"] else ""))


def _deep_view():
    """Live status, including WHAT one unit is waiting on.

    A folder with one 87MB clip in it sits still for minutes while ffmpeg seeks
    through it, which reads as "the job is stuck". Naming the item (and how long
    this folder has taken) is the difference between a progress line and a freeze."""
    with _deep_lock:
        v = dict(_deep)
    if v["running"] and v["current_id"]:
        v["left"] = _pending_in(v["current_id"])
        p = one("SELECT name, kind FROM nodes WHERE parent_id=? AND thumb_state='pending'"
                " LIMIT 1", (v["current_id"],)) if v["left"] else None
        v["left_name"] = p["name"] if p else ""
        v["left_kind"] = p["kind"] if p else ""
        v["since"] = int(time.time() - v["since"]) if v["since"] else 0
    else:
        v["left"] = 0
        v["left_name"] = v["left_kind"] = ""
        v["since"] = 0
    v.pop("started", None)
    return v


@app.get("/api/rebuild/preview/{node_id}")
def api_rebuild_preview(node_id: int):
    """How big the subtree re-encode below this folder would be, before it starts."""
    r = node_by_id(node_id) if node_id else None
    loc_id = r["loc_id"] if r else None
    parent_id = r["id"] if r else None
    if not loc_id:
        hit = q("SELECT id FROM locations ORDER BY id LIMIT 1")
        if not hit:
            return {"folders": 0, "items": 0}
        loc_id = hit[0]["id"]
    ids = _subtree_dirs(loc_id, parent_id)
    if parent_id:
        prel = one("SELECT rel FROM nodes WHERE id=?", (parent_id,))["rel"] or ""
        media = one("SELECT COUNT(*) c FROM nodes WHERE loc_id=? AND kind!='dir'"
                    " AND (rel=? OR rel LIKE ?)", (loc_id, prel, prel + "/%"))["c"]
    else:
        media = one("SELECT COUNT(*) c FROM nodes WHERE loc_id=? AND kind!='dir'",
                    (loc_id,))["c"]
    return {"folders": len(ids), "items": media + len(ids)}


@app.post("/api/rebuild/deep/{node_id}")
def api_rebuild_deep(node_id: int):
    with _scan_lock:
        scanning = bool(scan_state.get("running"))
    with _deep_lock:
        if _deep["running"]:
            return {"started": False, **_deep_view()}
    if scanning:
        raise HTTPException(409, "a scan is running -- wait for it, then re-encode")
    r = node_by_id(node_id) if node_id else None
    loc_id = r["loc_id"] if r else None
    parent_id = r["id"] if r else None
    if not loc_id:
        hit = q("SELECT id FROM locations ORDER BY id LIMIT 1")
        if not hit:
            return {"started": False, "folders": 0, "running": False}
        loc_id = hit[0]["id"]
    ids = _subtree_dirs(loc_id, parent_id)
    if not ids:
        return {"started": False, "folders": 0, "running": False}
    with _deep_lock:
        _deep.update(running=True, folders=len(ids), done=0, current="", current_id=0,
                     items=api_rebuild_preview(node_id).get("items", 0),
                     started=time.time(), stopped=False)
    threading.Thread(target=_run_deep_rebuild, args=(ids,), daemon=True,
                     name="deep-rebuild").start()
    _scan_event(f"Re-encode started: {len(ids)} folder{'' if len(ids) == 1 else 's'}"
                f" below {_folder_label(ids[0])}", folders=len(ids))
    return {"started": True, "running": True, "folders": len(ids)}


@app.get("/api/rebuild/status")
def api_rebuild_status():
    return _deep_view()


@app.post("/api/rebuild/cancel")
def api_rebuild_cancel():
    with _deep_lock:
        if not _deep["running"]:
            return {"running": False}
        _deep["stopped"] = True
    return {"running": True, "stopping": True}


@app.post("/api/build/frames/{node_id}")
def api_build_frames(node_id: int, deep: bool = True):
    """Explicit filmstrip build: one video, every video in a folder, or every
    video in the folder and everything below it (`deep=0` for "here").

    Posters are the default and cost ~0.6s each; a filmstrip is 9x that, so it
    is never pre-built for a whole library -- the user asks for it per file or
    per folder (or just hovers the tile)."""
    r = node_or_404(node_id)
    if r["kind"] == "video":
        # one clip, asked for by hand: always re-extract. The additive rule is for
        # folder builds, where skipping finished clips is the whole point; on a
        # single tile the only reason to click is that the strip needs replacing.
        # It is registered as a one-item job like a folder build, so
        # /api/build/status can say when it is FINISHED. Without that the page had
        # nothing to wait on: status said "idle" at once, and "has frames" was true
        # before the build began (every clip carries its two-image preview), so the
        # menu reported "filmstrip: built" two seconds in while ffmpeg still ran
        # (owner's report, 0.1.239). A second click while one is in flight reuses
        # that job: _once drops the duplicate request, so a fresh job would never
        # be bumped and the page would wait on it forever.
        with _builds_lock:
            cur = _builds.get(node_id)
            if cur and cur["done"] < cur["total"]:
                return {"queued": 1, "scope": "video", "force": True, "already": True}
            job = {"total": 1, "done": 0, "t0": time.time()}
            _builds[node_id] = job

        def bump_one(_vid, _job=job):
            with _builds_lock:
                _job["done"] += 1
                _job["last"] = time.time()

        queue_frames(node_id, on_done=bump_one, force=True)
        return {"queued": 1, "scope": "video", "force": True}
    if r["kind"] != "dir":
        raise HTTPException(400, "not a video or folder")
    rel = r["rel"] or ""
    if deep:
        rows = q("SELECT id FROM nodes WHERE loc_id=? AND kind='video' AND"
                 " (rel=? OR rel LIKE ?)", (r["loc_id"], rel, (rel + "/%") if rel else "%"))
    else:
        # "here" = videos sitting directly in this folder, no subfolders
        rows = q("SELECT id FROM nodes WHERE loc_id=? AND kind='video' AND rel LIKE ?"
                 " AND rel NOT LIKE ?", (r["loc_id"], (rel + "/%") if rel else "%",
                                        (rel + "/%/%") if rel else "%/%"))
    # "already has frames" is not "already has a filmstrip": every video now carries
    # poster + one extra still by default, so testing for ANY frames skipped the whole
    # library and the folder build queued nothing. Ask for the full strip instead.
    want = FRAME_COUNT + 1              # poster + FRAME_COUNT frames
    todo = [v["id"] for v in rows if len(existing_frames(v["id"])) < want]
    with _builds_lock:
        job = {"total": len(todo), "done": 0, "t0": time.time()}
        _builds[node_id] = job

    def bump(_vid, _job=job, _key=node_id):
        with _builds_lock:
            _job["done"] += 1
            _job["last"] = time.time()

    for vid in todo:
        queue_frames(vid, on_done=bump)
    return {"queued": len(todo), "videos": len(rows), "scope": "folder", "deep": deep}


@app.get("/api/build/status/{node_id}")
def api_build_status(node_id: int):
    with _builds_lock:
        j = _builds.get(node_id)
        if not j:
            return {"active": False}
        left = j["total"] - j["done"]
        rate = (j["done"] / max(0.1, time.time() - j["t0"])) or 0
        return {"active": left > 0, "total": j["total"], "done": j["done"],
                "eta": round(left / rate) if rate else None}


@app.get("/api/scan/status")
def api_scan_status():
    with _scan_lock:
        st = dict(scan_state)
    st["pending"] = pending_media_count()
    st["building"] = build_stats()      # work in flight, so a stall cannot be silent
    return st


# ---------------------------------------------------------------- static

@app.get("/", include_in_schema=False)
def index():
    html = (WEB / "index.html").read_text().replace("{{VERSION}}", VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/static/{name}", include_in_schema=False)
def static(name: str):
    if "/" in name or ".." in name:
        raise HTTPException(400)
    p = WEB / name
    if not p.exists():
        raise HTTPException(404)
    mt = _mt(name)
    if name.endswith(".html"):
        p = p  # index handled above
    return FileResponse(p, media_type=mt,
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})