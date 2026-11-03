"""Self-describing `.mxpack` thumbnail stores: one file per source folder,
zip or video.

Layout
------
    [MAGIC 8B] [record bytes ...] [TOC json] [toc_len uint64 LE] [END_MAGIC 8B]

Every record is a complete JPEG.  The TOC maps a member name to
(name_hash, offset, length, crc32), so a pack can be read, verified and
rebuilt into the index **without the media present** — losing the SQLite
file costs a TOC walk, not a re-crawl of 100k zips.

Why one pack per *folder* rather than per thumbnail
--------------------------------------------------
* A folder is the working set for one grid view: one ~6MB sequential read
  instead of ~300 random opens (54x faster cold on a spinning disk).
* File count drops ~100x (5.8M thumbs -> ~58k packs).
* Immutable until its folder changes, so rsync/restic/hardlink snapshots
  copy only the folders actually touched.
* A pack with no folder left is obviously garbage — orphan cleanup is a
  path test, not a hash-key archaeology exercise.
* `rm -f /packs/**/*.mxpack` is a complete cache purge that cannot touch
  the index.
"""
import json
import os
import struct
import threading
import zlib
from collections import OrderedDict

from .config import (PACK_PATH, PACK_MAGIC, PACK_END_MAGIC, PACK_CACHE_BYTES,
                     PACK_MODE, PACK_EXT, PACK_MIN_FREE_GB)
from pathlib import Path

TOC_VERSION = 1


# ------------------------------------------------------------------ keys

def _short(name: str, limit: int = 120) -> str:
    name = name.replace("/", "_").replace("\x00", "")
    return name if len(name) <= limit else name[:limit]


def stem_name(name: str) -> str:
    """The name a pack is filed under: an entry's name without its extension, so
    `Trip.zip` -> `Trip`, `clip.mkv` -> `clip`, `Holiday/` -> `Holiday`. One pack
    list per folder therefore reads the same whether the unit is a folder, an
    archive or a video. A leading dot is not an extension; `archive.tar.gz` loses
    only `.gz`."""
    i = name.rfind(".")
    if i > 0 and i < len(name) - 1:
        return name[:i]
    return name


def _stem_clashes(owner_dir, name_hint: str, siblings=None) -> bool:
    """True when another entry in the same folder shares this stem, e.g. a
    `Holiday/` beside a `Holiday.zip`: those two units would fight over one
    `.<stem>.mxthumbs` file, so both keep their extension in the pack name."""
    stem = stem_name(name_hint)
    if stem == name_hint:
        return False
    try:
        names = siblings if siblings is not None else [e.name for e in os.scandir(owner_dir)]
    except OSError:
        return False
    for n in names:
        if n != name_hint and stem_name(n) == stem:
            return True
    return False


def _has_headroom(directory) -> bool:
    try:
        import shutil
        return shutil.disk_usage(str(directory)).free >= PACK_MIN_FREE_GB * 1024 ** 3
    except OSError:
        return False


def central_path(loc_id: int, pack_key: str):
    import hashlib
    h = hashlib.sha1(f"{loc_id}|{pack_key}".encode()).hexdigest()
    return PACK_PATH / h[:2] / (h[2:] + PACK_EXT)


def resolve(loc_id: int, pack_key: str, owner_dir, name_hint: str):
    """Where this pack should live.

    adjacent: `<owner_dir>/.<unit stem>.mxthumbs` -- ONE hidden file beside the
    media it describes, in the media's parent folder (no new directories in the
    tree). The extension is dropped (`Trip.zip` -> `.Trip.mxthumbs`) so a
    folder, an archive and a video all look the same in the listing; if two
    units in that folder share a stem the extension is kept so they cannot
    collide. Any failure (RO mount, full disk, permissions) falls back to the
    central store so nothing breaks on a read-only share.
    """
    if PACK_MODE in ("adjacent", "hybrid") and owner_dir is not None:
        try:
            d = Path(owner_dir)
            if os.access(str(d), os.W_OK) and _has_headroom(d):
                name = name_hint if _stem_clashes(str(d), name_hint) else stem_name(name_hint)
                return d / ("." + _short(name) + PACK_EXT)
        except OSError:
            pass
    return central_path(loc_id, pack_key)


def hide(path):
    """Best effort: set the DOS hidden bit so SMB/Windows hides the pack too.
    The dot prefix already hides it from ls/Finder; this needs
    `store dos attributes = yes` (Unraid default) and xattr support."""
    try:
        os.setxattr(str(path), "user.DOSATTRIB", b"0x22")   # hidden + archive
    except (OSError, AttributeError):
        pass


# ------------------------------------------------------------------ build/read

def build(items, loc_id: int, pack_key: str, meta=None, gen: int = 0):
    """items: iterable of (name, bytes). meta: {name: (src_size, src_mtime)} so a
    pack found in a moved/renamed folder can be adopted only when its sources
    still match. Returns (blob, index, doc); index maps name -> (off, len, crc,
    size, mtime)."""
    meta = meta or {}
    out = [PACK_MAGIC]
    pos = len(PACK_MAGIC)
    toc = []
    for name, data in items:
        if not data:
            continue
        size, mtime = meta.get(name, (0, 0.0))
        out.append(data)
        toc.append([name, pos, len(data), zlib.crc32(data) & 0xFFFFFFFF,
                    int(size or 0), float(mtime or 0.0)])
        pos += len(data)
    # `gen` records the thumbnail MASTER generation that wrote these bytes. It is
    # what lets a pack be adopted after a rename only when its pictures are still
    # current, instead of serving old geometry forever.
    toc_doc = {"v": TOC_VERSION, "loc": loc_id, "key": pack_key,
               "gen": int(gen or 0),
               "gens": {n: int(gen or 0) for n, *_ in toc}, "items": toc}
    doc = json.dumps(toc_doc, separators=(",", ":")).encode()
    out.append(doc)
    out.append(struct.pack("<Q", len(doc)))
    out.append(PACK_END_MAGIC)
    index = {n: (o, ln, c, s, m) for n, o, ln, c, s, m in toc}
    return b"".join(out), index, toc_doc


def write(path, items, loc_id: int, pack_key: str, meta=None, gen: int = 0):
    """Atomic whole-pack write. Returns (bytes_written, index)."""
    blob, index, doc = build(items, loc_id, pack_key, meta, gen)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    hide(path)
    _cache_put(str(path), blob, index, doc)
    return len(blob), index


def append(path, items, loc_id: int, pack_key: str, meta=None, gen: int = 0):
    """Add members WITHOUT moving the ones already in the pack.

    The TOC lives at the END of the file, so appending new member bytes just
    before it leaves every existing offset valid -- which is what makes a shared
    folder pack (images + several videos) safe to grow: a whole-pack rewrite
    would shift bytes and every row pointing into the pack would serve the wrong
    data. Replaced members are dropped from the TOC (their bytes become dead
    space) and re-appended, so the newest copy always wins.

    Returns (bytes_on_disk, index)."""
    pairs = list(items.items()) if isinstance(items, dict) else list(items)
    if not path.exists():
        return write(path, pairs, loc_id, pack_key, meta, gen)
    blob, index, doc = read(path)
    try:
        toc_len = struct.unpack("<Q", blob[-len(PACK_END_MAGIC) - 8:-len(PACK_END_MAGIC)])[0]
    except struct.error:
        return write(path, pairs, loc_id, pack_key, meta, gen)
    start = len(blob) - len(PACK_END_MAGIC) - 8 - toc_len
    if start < len(PACK_MAGIC):
        return write(path, pairs, loc_id, pack_key, meta, gen)
    body = bytearray(blob[:start])
    toc = [[n, o, ln, c, s, m] for n, (o, ln, c, s, m) in index.items()]
    incoming = dict(pairs)
    if not incoming:
        return len(blob), index
    toc = [e for e in toc if e[0] not in incoming]      # superseded copies go dead
    for name, data in incoming.items():
        off = len(body)
        body += data
        toc.append([name, off, len(data), zlib.crc32(data) & 0xFFFFFFFF,
                    (meta or {}).get(name, (0, 0))[0], (meta or {}).get(name, (0, 0))[1]])
    # One pack holds members written by DIFFERENT code versions -- a folder's
    # pictures plus a tile composed months later. A single pack-level stamp
    # therefore lies about at least some members, so each one records its own.
    gens = dict(doc.get("gens") or {})
    if not gens:                      # pack predates per-member stamps
        gens = {e[0]: int(doc.get("gen", 0) or 0) for e in toc}
    for name in incoming:
        gens[name] = int(gen or 0)
    toc_doc = {"v": TOC_VERSION, "loc": doc.get("loc", loc_id),
               "key": doc.get("key", pack_key),
               "gen": int(gen or doc.get("gen", 0) or 0),
               "gens": gens, "items": toc}
    out_doc = json.dumps(toc_doc, separators=(",", ":")).encode()
    out = bytes(body) + out_doc + struct.pack("<Q", len(out_doc)) + PACK_END_MAGIC
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(out)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    hide(path)
    new_index = {n: (o, ln, c, s, m) for n, o, ln, c, s, m in toc}
    _cache_put(str(path), out, new_index, toc_doc)
    return len(out), new_index


class PackError(Exception):
    pass


def parse(blob: bytes):
    """Validate magic + trailer and return (toc_dict, index).

    Two independent version numbers guard the format:
    `PACK_MAGIC` (b"MXPACK1\\n") is the *container* version — bump it only for a
    change the trailer/TOC layout cannot express, in which case an old build sees
    "bad magic" and rebuilds instead of guessing;
    TOC_VERSION is the *schema* version inside that container — bump it when the
    item tuple or doc keys change, and an old build refuses the pack the same way.
    Either way the failure is safe: the pack is treated as unreadable and the
    thumbs are rebuilt, never misread.
    """
    if len(blob) < len(PACK_MAGIC) + 8 + len(PACK_END_MAGIC):
        raise PackError("too short")
    if not blob.startswith(PACK_MAGIC):
        raise PackError("bad magic")
    if not blob.endswith(PACK_END_MAGIC):
        raise PackError("bad trailer")
    toc_len = struct.unpack("<Q", blob[-len(PACK_END_MAGIC) - 8:-len(PACK_END_MAGIC)])[0]
    start = len(blob) - len(PACK_END_MAGIC) - 8 - toc_len
    if start < len(PACK_MAGIC):
        raise PackError("bad toc length")
    try:
        doc = json.loads(blob[start:start + toc_len].decode())
    except Exception as exc:
        raise PackError(f"bad toc: {exc}")
    ver = doc.get("v")
    if not isinstance(ver, int) or ver < 1:
        raise PackError(f"bad toc version {ver!r}")
    if ver > TOC_VERSION:
        raise PackError(f"toc v{ver} newer than supported v{TOC_VERSION}")
    try:
        toc = {n: (o, ln, c, s, m) for n, o, ln, c, s, m in doc["items"]}
    except (KeyError, TypeError, ValueError):
        raise PackError("bad toc items")
    return doc, toc


def member_gen(doc, name):
    """Generation of ONE member, or 0 when provenance is unknown.

    A pack written before per-member stamps existed carries only a pack-level
    value, and that value is NOT evidence about its members: the pack is rewritten
    every time anything is added, so a tile written today dates a photo written
    last year. Unknown therefore means stale, which costs one re-encode and can
    never serve old geometry."""
    gens = doc.get("gens")
    if not gens:
        return 0
    return int(gens.get(name, 0) or 0)


def read(path):
    """Return (blob, index, doc) for a pack, from RAM when hot."""
    key = str(path)
    hit = _cache_get(key)
    if hit is not None:
        blob, index, doc = hit
        return blob, index, doc
    with open(path, "rb") as f:
        blob = f.read()
    doc, index = parse(blob)
    _cache_put(key, blob, index, doc)
    return blob, index, doc


def item(path, name: str) -> bytes:
    """One member's bytes. Falls back to a full re-read if the name is not in a
    cached pack (a pack replaced on disk since it was cached)."""
    blob, index, _ = read(path)
    try:
        off, length = index[name][:2]
    except KeyError:
        _cache_drop(str(path))
        blob, index, _ = read(path)
        off, length = index[name][:2]
    return blob[off:off + length]


def items_of(path):
    """All members as a dict — used when merging a new thumb into an existing
    folder pack."""
    blob, index, _ = read(path)
    return {n: blob[o:o + ln] for n, (o, ln, *_rest) in index.items()}


def meta_of(path):
    """{name: (src_size, src_mtime)} from the TOC — used to decide whether a
    pack found in a renamed folder still describes its sources."""
    _blob, index, _doc = read(path)
    return {n: (v[3], v[4]) for n, v in index.items()}


def verify(path):
    """CRC-check every member. Returns (ok_count, bad_names)."""
    blob, index, _ = read(path)
    bad = []
    for name, (off, length, crc, *_rest) in index.items():
        if (zlib.crc32(blob[off:off + length]) & 0xFFFFFFFF) != crc:
            bad.append(name)
    return len(index) - len(bad), bad


# ------------------------------------------------------------------ LRU cache

_CACHE = OrderedDict()          # path -> (blob, index, doc)
_CACHE_BYTES = 0
_CACHE_LOCK = threading.Lock()


def _cache_get(key):
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None:
            return None
        _CACHE.move_to_end(key)
        return hit


def _cache_put(key, blob, index, doc=None):
    global _CACHE_BYTES
    size = len(blob)
    if size > PACK_CACHE_BYTES:
        return
    if doc is None:                 # never cache a doc-less entry: the TOC's
        doc, _ = parse(blob)        # key/loc drive pack adoption on rename
    with _CACHE_LOCK:
        old = _CACHE.pop(key, None)
        if old is not None:
            _CACHE_BYTES -= len(old[0])
        _CACHE[key] = (blob, index, doc)
        _CACHE_BYTES += size
        while _CACHE_BYTES > PACK_CACHE_BYTES and len(_CACHE) > 1:
            _k, (b, _i, _d) = _CACHE.popitem(last=False)
            _CACHE_BYTES -= len(b)


def _cache_drop(key):
    global _CACHE_BYTES
    with _CACHE_LOCK:
        old = _CACHE.pop(key, None)
        if old is not None:
            _CACHE_BYTES -= len(old[0])


def drop(path):
    """Public: forget one pack (file gone or replaced behind our back)."""
    _cache_drop(str(path))


def short(name: str) -> str:
    """Public: the on-disk name a pack gets for `name` (truncation rule)."""
    return _short(name)


def cache_stats():
    with _CACHE_LOCK:
        return {"packs": len(_CACHE), "bytes": _CACHE_BYTES, "limit": PACK_CACHE_BYTES}


def cache_clear():
    global _CACHE_BYTES
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE_BYTES = 0


# ------------------------------------------------------------------ maintenance

def iter_packs():
    """Packs in the central store (the adjacent store is walked per directory)."""
    if not PACK_PATH.exists():
        return
    for d in PACK_PATH.iterdir():
        if d.is_dir():
            yield from d.glob("*" + PACK_EXT)


def purge():
    """Delete every pack. Returns (count, bytes_freed)."""
    n = freed = 0
    for p in list(iter_packs()):
        try:
            freed += p.stat().st_size
            p.unlink()
            n += 1
        except OSError:
            pass
    cache_clear()
    return n, freed
