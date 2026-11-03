"""Live progress events (server-sent events).

Thumb building is the one place where waiting is visible, so each encoded
thumbnail is streamed to any connected browser the moment it exists: the tile
fills in, and no "Building N thumbnails…" notice is needed for the common case.

Only thumbnails produced while a subscriber is connected are published, and the
base64 payload is only built when somebody is listening, so an idle server pays
nothing for this.
"""
import asyncio
import base64
import threading

_subs = set()
_lock = threading.Lock()
_loop = None


def set_loop(loop):
    """Called once at startup: publishing happens on worker threads."""
    global _loop
    _loop = loop


def subscribe():
    q = asyncio.Queue(maxsize=512)
    with _lock:
        _subs.add(q)
    return q


def unsubscribe(q):
    with _lock:
        _subs.discard(q)


def _offer(q, ev):
    try:
        q.put_nowait(ev)
    except asyncio.QueueFull:
        pass


def publish(ev):
    with _lock:
        subs = list(_subs)
        loop = _loop
    if not subs or loop is None:
        return False
    for q in subs:
        try:
            loop.call_soon_threadsafe(_offer, q, ev)
        except RuntimeError:       # loop closed mid-shutdown
            pass
    return True


def listening():
    with _lock:
        return bool(_subs)


def publish_thumb(node_id, blob, stamp, kind="node", parent=None, zip_id=None):
    """One thumbnail is ready: send its bytes so the tile can paint now.

    `parent`/`zip_id` say where it belongs, so the page can count a running scan
    down without guessing which folder an event came from."""
    if not listening():
        return False
    ev = {"t": "thumb", "k": kind, "id": node_id, "v": stamp,
          "d": base64.b64encode(blob).decode("ascii")}
    if parent is not None:
        ev["p"] = parent
    if zip_id is not None:
        ev["z"] = zip_id
    return publish(ev)


def publish_done(owner_id, kind="dir"):
    return publish({"t": "done", "k": kind, "id": owner_id})
