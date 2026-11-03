import collections
import hashlib
import io
import logging
import os
import shutil
import subprocess
import threading
import traceback
import time
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from natsort import natsorted
from PIL import Image, ImageOps

from .config import (THUMB_PATH, THUMB_MAX, THUMB_QUALITY, IMAGE_EXTS, VIDEO_EXTS,
                     FRAME_COUNT, PACK_MODE, THUMB_WORKERS, THUMB_GEN, effective_cpus,
                     PACK_WORKERS, FFMPEG_JOBS)
from .db import db, q, one, exec_many, node_by_id, THUMB_WORK
from . import packs, events

log = logging.getLogger("mx.media")

PACK_ADJACENT_OK = PACK_MODE in ("adjacent", "hybrid")

_CPUS = effective_cpus()          # honours --cpuset-cpus and the cgroup quota
_executor = ThreadPoolExecutor(max_workers=PACK_WORKERS)

# Opening a zip or a folder is a person waiting, not a catalog running: those pack
# builds get their own two workers, because a deep scan keeps _executor busy with
# thousands of zip mosaics and a FIFO queue put the zip you just clicked behind all
# of them ("clicking a zip does not populate the thumbnails").
_fast = ThreadPoolExecutor(
    max_workers=max(1, min(_CPUS, int(os.environ.get("MX_FAST_WORKERS", "2") or 2))),
    thread_name_prefix="fast")

# A pack is built by one job, but the images in it are independent: fan the
# encode out over threads so a big folder does not pin a single core. This pool
# is separate from _executor on purpose -- jobs running in _executor submit here,
# and sharing one pool would risk starving/deadlocking it.
_encode_pool = ThreadPoolExecutor(max_workers=THUMB_WORKERS,
                                  thread_name_prefix="encode")


# Encode queue with a priority door. ThreadPoolExecutor only appends, so a folder
# pack that queued 300 encodes put a folder the user had just opened behind all of
# them; the workers here pop from the front, and a priority job is inserted ahead of
# everything already waiting.
_pmq = collections.deque()          # (fn, item, future)
_pmq_cv = threading.Condition()
_pmw = [0]


def _pm_worker():
    while True:
        with _pmq_cv:
            while not _pmq:
                _pmq_cv.wait()
            fn, item, fut = _pmq.popleft()
        if not fut.set_running_or_notify_cancel():
            continue
        try:
            fut.set_result(fn(item))
        except BaseException as e:              # noqa: BLE001 - reported to the caller
            fut.set_exception(e)


def _pmap(fn, items, front=False):
    """map() over the encode pool, preserving order and tolerating failures.

    front=True puts these jobs ahead of every encode already queued -- the difference
    between "the folder I am looking at fills in now" and "after the whole subtree"."""
    if len(items) <= 1 or THUMB_WORKERS == 1:
        return [fn(i) for i in items]
    futs = []
    with _pmq_cv:
        while _pmw[0] < THUMB_WORKERS:
            _pmw[0] += 1
            threading.Thread(target=_pm_worker, name=f"enc{_pmw[0]}", daemon=True).start()
        block = []
        for i in items:
            f = Future()
            futs.append(f)
            block.append((fn, i, f))
        if front:
            # extendleft() prepends in reverse: pass the block reversed so its FIRST
            # item lands at the head. Pushing with appendleft one by one (the old way)
            # put the whole build in backwards and a zip filled in from the bottom.
            _pmq.extendleft(reversed(block))
        else:
            _pmq.extend(block)
        _pmq_cv.notify_all()
    return [f.result() for f in futs]

# ffmpeg/ffprobe gate: ONE job at a time, lowest CPU priority, idle IO class.
# Keeps 4K poster/frame work from saturating the server (unlimited decoder
# threads + parallel jobs once pegged a 32-core box at ~50 load).
# ffmpeg concurrency. This used to be 1 (a 32-core box once hit load ~50), but one
# decoder leaves the other 31 cores idle during a video-heavy scan, so it is now
# env-tunable: MX_VIDEO_JOBS (default 3) caps simultaneous decodes.
VIDEO_JOBS = max(1, int(os.environ.get("MX_VIDEO_JOBS", "0") or 0) or FFMPEG_JOBS)
# ffmpeg jobs get their own pool: sharing the 4-thread thumbnail pool meant a
# right-clicked filmstrip waited behind a 300-image folder build
_vpool = ThreadPoolExecutor(max_workers=VIDEO_JOBS + 1)
_FGATE = threading.BoundedSemaphore(VIDEO_JOBS)
_INFLIGHT = set()          # node_ids currently being frame-extracted
_IFLOCK = threading.Lock()
_PRIO = (["nice", "-n", "19", "ionice", "-c3"]
         if shutil.which("nice") and shutil.which("ionice") else [])
THREADS = "2"              # per-ffmpeg decoder threads
# GPU decode: 'cuda' (NVDEC) by default, set MX_HWACCEL=none to force CPU.
# Ask for NVDEC only if this container can actually see a GPU: without a
# passthrough every ffmpeg call failed its hardware probe first and only then
# retried on CPU, which doubled the cost of every frame (8 frames took 8s).
# Decode path. MX_HWACCEL: auto (default) | cuda | none.
#   auto -- measure BOTH paths once per (codec, resolution class) on a real file and
#   use the winner for that family afterwards. The GPU is not always faster: NVDEC
#   wins big on 4K HEVC and can lose on small H.264 clips, and swapping in a 5060 Ti
#   should be picked up with no config change.
_HW_PREF = os.environ.get("MX_HWACCEL", "auto").strip().lower()
_HW_CHOICE = {}                 # (codec, height_class) -> "cuda" | "cpu"
_HW_NOTE = {}                   # same key -> human note for /api/config


def have_gpu():
    for i in range(4):
        if os.path.exists(f"/dev/nvidia{i}"):
            return True
    return False


HW_REASON = ("MX_HWACCEL=none" if _HW_PREF == "none"
             else ("" if have_gpu() else "no GPU device in this container: CPU decode"))


def _hw_flags():
    if _HW_PREF == "none" or not have_gpu():
        return []
    return ["-hwaccel", "cuda"]


def _family(src):
    """(codec, height class) -- what actually decides whether NVDEC helps."""
    codec, h = "?", 0
    try:
        out = ff_run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                      "-show_entries", "stream=codec_name,height",
                      "-of", "csv=p=0:s=,", src], timeout=30, hw=False)
        if out and out.returncode == 0:
            parts = (out.stdout or "").strip().split(",")
            codec = parts[0] or "?"
            h = int(float(parts[1])) if len(parts) > 1 and parts[1] else 0
    except (OSError, ValueError):
        pass
    return (codec, ">=1080" if h >= 1080 else "<1080")


def _time_decode(src, flags):
    """Wall time for a 1s decode with the given hwaccel flags (99s on failure)."""
    t0 = time.time()
    r = ff_run(["ffmpeg", "-v", "error", "-threads", THREADS, *flags,
                "-ss", "1", "-i", src, "-t", "1", "-vf", "scale=168:-1",
                "-f", "null", "-"], timeout=90, hw=False)
    if r is None or r.returncode != 0:
        return 99.0
    return time.time() - t0


def choose_hw(src):
    """Which decode path to use for this file. Probes once per family."""
    if _HW_PREF == "none" or not have_gpu():
        return "cpu"
    if _HW_PREF == "cuda":
        return "cuda"
    fam = _family(src)
    if fam in _HW_CHOICE:
        return _HW_CHOICE[fam]
    gpu_t = _time_decode(src, ["-hwaccel", "cuda"])
    cpu_t = _time_decode(src, [])
    # only switch to the GPU if it wins clearly (10%): a coin-flip is not worth
    # the extra failure mode
    pick = "cuda" if gpu_t < cpu_t * 0.9 else "cpu"
    _HW_CHOICE[fam] = pick
    _HW_NOTE[fam] = f"{fam[0]}/{fam[1]}: gpu {gpu_t:.2f}s vs cpu {cpu_t:.2f}s -> {pick}"
    return pick


def ff_run(args, timeout=120, hw=True):
    flags = _hw_flags() if hw else []
    cmd = _PRIO + (args[:1] + flags + args[1:] if flags else args)
    try:
        with _FGATE:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def ff_run_hw_fb(args, timeout=120):
    """Try GPU-decoded run; on failure retry once on CPU. Returns CompletedProcess|None."""
    r = ff_run(args, timeout=timeout, hw=True)
    if r is not None and r.returncode == 0:
        return r
    return ff_run(args, timeout=timeout, hw=False)


# ---------------------------------------------------------------- keys/paths

def node_abspath(row):
    loc = one("SELECT root FROM locations WHERE id=?", (row["loc_id"],))
    return os.path.join(loc["root"], row["rel"])


def _thumb_file(key: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()
    return THUMB_PATH / h[:2] / (h[2:] + ".jpg")


def node_thumb_key(row):
    return "n:" + node_abspath(row)


def entry_thumb_key(zip_row, entry_name):
    return "z:" + node_abspath(zip_row) + "#" + entry_name


# ---------------------------------------------------------------- thumbs

def encode_thumb(data: bytes) -> bytes:
    """Master thumbnail bytes: exact 300x225, EXIF-upright, letterboxed.

    This used to be ImageOps.fit (cover + centre crop), which threw away the top
    and bottom of every portrait shot -- the tile showed a headless torso. pad()
    scales the whole frame down to fit and fills the rest with black, so the 4:3
    geometry stays identical while the picture is complete.
    """
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    im = ImageOps.pad(im, THUMB_MAX, method=Image.Resampling.LANCZOS,
                      color=(0, 0, 0), centering=(0.5, 0.5))
    if im.mode in ("RGBA", "P", "LA"):
        bg = Image.new("RGB", im.size, (17, 17, 17))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=THUMB_QUALITY)
    return buf.getvalue()


def write_thumb_from_bytes(data: bytes, dest: Path):
    """Legacy single-file writer. Pre-pack thumbnails stay readable, so a
    library repaired by an older build is never stranded."""
    blob = encode_thumb(data)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, dest)


# ---- pack plumbing ------------------------------------------------------

def _owner_abs(loc_id, rel):
    loc = one("SELECT root FROM locations WHERE id=?",
              (loc_id,))
    if not loc:
        return None
    return os.path.join(loc["root"], rel) if rel else loc["root"]


def _pack_alive(pack_id):
    """True when that thumb_pack row still exists (a thumb pointing at a deleted
    row can never serve bytes, so it must count as work to do)."""
    return one("SELECT 1 AS ok FROM thumb_pack WHERE id=?", (pack_id,)) is not None


def folder_pack_spot(loc_id, parent_row):
    """(dir, name) for a folder's own pack: a hidden file beside the folder,
    named after it. The location root has no parent inside the mount, so its
    own loose images get `._root.mxthumbs` in the root itself."""
    if parent_row is None:
        return _owner_abs(loc_id, ""), "_root"
    abs_dir = _owner_abs(loc_id, parent_row["rel"]) or ""
    return os.path.dirname(abs_dir), parent_row["name"]


def _pack_upsert(loc_id, kind, owner_id, key, path):
    """One row per (loc, key). Matching by key first means a pack that was
    renamed on disk (extension dropped, folder renamed) updates its row instead
    of leaving a stale row pointing at a file that no longer exists."""
    r = one("SELECT id, path FROM thumb_pack WHERE loc_id=? AND key=?", (loc_id, key))
    if r:
        exec_many("UPDATE thumb_pack SET kind=?, owner_id=?, path=? WHERE id=?",
                  (kind, owner_id, str(path), r["id"]))
        return r["id"]
    r = one("SELECT id FROM thumb_pack WHERE path=?", (str(path),))
    if r:
        exec_many("UPDATE thumb_pack SET loc_id=?, kind=?, owner_id=?, key=? WHERE id=?",
                  (loc_id, kind, owner_id, key, r["id"]))
        return r["id"]
    return exec_many(
        "INSERT INTO thumb_pack(loc_id,kind,owner_id,key,path,built) VALUES(?,?,?,?,?,?)",
        (loc_id, kind, owner_id, key, str(path), time.time()))


_PACK_LOCKS = {}
_PACK_LOCKS_GUARD = threading.Lock()


def _pack_lock(path):
    """One writer per pack file. Video filmstrips now merge into the FOLDER pack,
    and up to MX_VIDEO_JOBS of them can be building at once: without this, two
    read-modify-write cycles overlap and the later writer silently drops the
    other's members."""
    k = str(path)
    with _PACK_LOCKS_GUARD:
        lock = _PACK_LOCKS.get(k)
        if lock is None:
            lock = _PACK_LOCKS[k] = threading.RLock()
        return lock


def _store_in_pack(loc_id, kind, owner_id, key, owner_dir, name_hint, members, targets,
                   meta=None):
    """Merge `members` into the owner's pack with ONE sequential write, then
    point rows at (pack, off, len). Merging keeps a regenerated thumb from
    losing its siblings; a corrupt pack is rebuilt rather than trusted."""
    path = packs.resolve(loc_id, key, owner_dir, name_hint)
    with _pack_lock(path):                  # read-merge-write must not interleave
        return _store_in_pack_locked(loc_id, kind, owner_id, key, path, members,
                                     targets, meta)


def _store_in_pack_locked(loc_id, kind, owner_id, key, path, members, targets, meta=None):
    existing, existing_meta = {}, {}
    if path.exists():
        try:
            existing = packs.items_of(path)
            existing_meta = packs.meta_of(path)
        except (packs.PackError, OSError):
            existing, existing_meta = {}, {}
    # APPEND, never rewrite: existing members keep their offsets, so rows
    # already pointing into this pack stay correct while it grows (images and
    # several videos share one folder pack now).
    total, index = packs.append(path, members, loc_id, key, dict(meta or {}),
                                gen=THUMB_GEN)
    merged = dict(existing)
    merged.update(members)
    pid = _pack_upsert(loc_id, kind, owner_id, key, path)
    exec_many("UPDATE thumb_pack SET count=?, bytes=?, dead=0, built=? WHERE id=?",
              (len(index), total, time.time(), pid))
    for name, (tbl, tid) in targets.items():
        hit = index.get(name)
        if not hit:
            continue
        off, ln = hit[:2]
        exec_many(f"UPDATE {tbl} SET pack_id=?, pack_off=?, pack_len=?,"
                  f" thumb_state='ready', thumb_gen=? WHERE id=?",
                  (pid, off, ln, THUMB_GEN, tid))
    return path, len(merged)


def _find_pack_by_meta(owner_dir, kind_letter, names, want_mtime, expect_count=None,
                       want_size=None):
    """A pack is named after its unit, so a rename orphans it. Find it by
    content identity: every expected member name present, matching source
    mtime, and the expected member count among the hidden packs in the SAME
    directory. Only a unique match is used — anything ambiguous is regenerated.

    Videos MUST also pass want_size: every filmstrip holds the same member names
    (poster + f00..f07), so mtime alone cannot tell two clips apart, and a camera
    writing several files in the same second made one video adopt its
    neighbour's filmstrip (wrong pictures, no rebuild). Source byte size is
    unique per file and survives a rename."""
    if not owner_dir:
        return None
    from .config import PACK_EXT
    d = Path(owner_dir)
    if not d.is_dir():
        return None
    want = set(names)
    hits = []
    for p in d.glob("." + "*" + PACK_EXT):
        try:
            _blob, index, doc = packs.read(p)
        except (packs.PackError, OSError):
            continue
        if not str(doc.get("key", "")).startswith(kind_letter + ":"):
            continue
        if expect_count is not None and len(index) != expect_count:
            continue
        if not want.issubset(index.keys()):
            continue
        probe = index.get(names[0]) if names else None
        if probe is None:
            probe = next(iter(index.values()), None)
        if probe is None:
            continue
        if probe[4] and want_mtime and abs(probe[4] - want_mtime) > 1:
            continue
        if want_size is not None and probe[3] != want_size:
            continue
        hits.append(p)
    return hits[0] if len(hits) == 1 else None


def adopt_pack(loc_id, kind, owner_id, key, owner_dir, name_hint, rows, tbl, meta_get):
    """Reuse a pack that already sits beside this media (a renamed/moved folder,
    or a rebuilt index) instead of re-encoding anything.

    Adopts only when the TOC's recorded (size, mtime) still matches the source,
    so an edited image is regenerated rather than served stale. Returns the
    number of rows adopted."""
    path = packs.resolve(loc_id, key, owner_dir, name_hint)
    index = None
    _doc = {}
    if path.exists():
        try:
            _blob, index, _doc = packs.read(path)
        except (packs.PackError, OSError):
            index = None
    if not index:
        # renamed zip: its pack still carries the old name
        want_mtime = meta_get(rows[0])[1]
        found = _find_pack_by_meta(owner_dir, key.split(":")[0],
                                   [r["name"] for r in rows], want_mtime,
                                   expect_count=len(rows))
        if found is None:
            return 0
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(found, path)                 # rename to the canonical name
            _blob, index, _doc = packs.read(path)
        except (OSError, packs.PackError):
            return 0
    if not index:
        return 0
    pid = None
    adopted = 0
    for r in rows:
        hit = index.get(r["name"])
        if not hit:
            continue
        off, ln, _crc, size, mtime = hit
        want_size, want_mtime = meta_get(r)
        if size != want_size:
            continue
        if mtime and want_mtime and abs(mtime - want_mtime) > 1:
            continue
        # judge THIS member, not the pack: one pack legitimately holds pictures
        # made by several versions, so a pack-level stamp proves nothing.
        if packs.member_gen(_doc, r["name"]) < THUMB_GEN:
            continue
        if pid is None:
            pid = _pack_upsert(loc_id, kind, owner_id, key, path)
        exec_many(f"UPDATE {tbl} SET pack_id=?, pack_off=?, pack_len=?,"
                  f" thumb_state='ready', thumb_gen=? WHERE id=?",
                  (pid, off, ln, packs.member_gen(_doc, r["name"]), r["id"]))
        adopted += 1
    if adopted:
        try:
            total = path.stat().st_size
        except OSError:
            total = 0
        exec_many("UPDATE thumb_pack SET count=?, bytes=?, dead=0, built=? WHERE id=?",
                  (len(index), total, time.time(), pid))
    return adopted


def gen_node_thumb(node_id):
    row = node_by_id(node_id)
    if not row:
        return
    try:
        if row["kind"] == "video":
            ensure_video_pack(node_id)
            return
        data = encode_thumb(open(node_abspath(row), "rb").read())
        parent = node_by_id(row["parent_id"]) if row["parent_id"] else None
        rel_dir = parent["rel"] if parent else ""
        owner, hint = folder_pack_spot(row["loc_id"], parent)
        _store_in_pack(row["loc_id"], "dir", row["parent_id"], f"d:{rel_dir}",
                       owner, hint,
                       {row["name"]: data},
                       {row["name"]: ("nodes", node_id)})
    except Exception:
        exec_many("UPDATE nodes SET thumb_state='error' WHERE id=?", (node_id,))


def gen_folder_thumbs(loc_id, parent_id, ids=None, force=False, front=False):
    """All pending images in one folder -> ONE pack write. This is the shape
    that keeps an HDD happy: a single sequential write per folder instead of
    one small write per thumbnail.

    The pack is a hidden file beside the folder (`.<folder>.mxthumbs`); a pack
    left under the old name by a rename is adopted and renamed first, so a
    reorganisation costs no re-encoding."""
    parent = node_by_id(parent_id) if parent_id else None
    rel_dir = parent["rel"] if parent else ""
    owner, hint = folder_pack_spot(loc_id, parent)
    key = f"d:{rel_dir}"
    sel = set(ids) if ids else None

    def pending():
        rows = q("SELECT * FROM nodes WHERE loc_id=? AND parent_id IS ? AND kind='image'"
                 f" AND ({THUMB_WORK})", (loc_id, parent_id))
        # the grid is natural-name sorted, so build in that order: the tiles fill in
        # from the top-left instead of wherever the rows happened to be inserted
        return natsorted([r for r in rows if sel is None or r["id"] in sel], key=lambda r: r["name"])

    rows = pending()
    if not rows:
        return 0
    # force=True is "Re-encode thumbnails here": re-draw the pictures even though a
    # perfectly valid pack already holds them. Without it, adopting the existing pack
    # re-links all sixty rows and nothing is encoded -- a button labelled re-encode
    # that quietly did nothing unless THUMB_GEN had been bumped.
    if not force and adopt_pack(loc_id, "dir", parent_id, key, owner, hint, rows, "nodes",
                                lambda r: (r["size"], r["mtime"])):
        rows = pending()
        if not rows:
            return 0
    members, targets, meta = {}, {}, {}

    def one(r):
        try:
            blob = encode_thumb(open(node_abspath(r), "rb").read())
        except Exception:
            exec_many("UPDATE nodes SET thumb_state='error' WHERE id=?", (r["id"],))
            return r, None
        # paint it in the browser now: the pack write happens once at the end,
        # but the user should not have to wait for it to see progress
        events.publish_thumb(r["id"], blob, int(float(r["mtime"] or 0) * 1000),
                             parent=parent_id)
        return r, blob

    for r, blob in _pmap(one, rows, front=front):   # parallel encode, in row order
        if blob is None:
            continue
        members[r["name"]] = blob
        targets[r["name"]] = ("nodes", r["id"])
        meta[r["name"]] = (r["size"], r["mtime"])
    if not members:
        return 0
    try:
        _store_in_pack(loc_id, "dir", parent_id, key, owner, hint,
                       members, targets, meta)
    except OSError:
        # pack not writable (RO share, full disk): leave them pending so a
        # later attempt can retry instead of marking a permanent error
        for _name, (_tbl, tid) in targets.items():
            exec_many("UPDATE nodes SET thumb_state='pending' WHERE id=?", (tid,))
        raise
    return len(members)


def gen_entry_thumb(zip_id, entry_id):
    gen_zip_pack(zip_id, ids=[entry_id])


def zip_tile_path(zrow):
    return packs.resolve(zrow["loc_id"], f"z:{zrow['rel']}",
                         os.path.dirname(node_abspath(zrow)), zrow["name"])


def _link_zip_tile(zrow, path, index):
    hit = index.get(ZIP_TILE)
    if not hit:
        return
    off, ln = hit[:2]
    prow = one("SELECT id FROM thumb_pack WHERE loc_id=? AND key=?",
               (zrow["loc_id"], f"z:{zrow['rel']}"))
    if not prow:
        return
    exec_many("UPDATE nodes SET pack_id=?, pack_off=?, pack_len=?, thumb_state='ready',"
              " thumb_gen=? WHERE id=?", (prow["id"], off, ln, THUMB_GEN, zrow["id"]))


def gen_zip_tile(zip_id, force=False):
    """A zip's tile: the first four images inside it, composed 2x2 into one
    300x225 JPEG, stored in the zip's own pack (beside the zip).

    Built on demand when the folder listing renders, NOT during scan: Andy's
    library is 110k zips, and pre-building a tile for each would decompress
    440k images for folders he may never open."""
    zrow = node_by_id(zip_id)
    if not zrow or zrow["kind"] != "zip":
        return 0
    path = zip_tile_path(zrow)
    if path.exists() and not force:
        try:
            _b, index, _d = packs.read(path)
            if (ZIP_TILE in index and index[ZIP_TILE][3] == (zrow["size"] or 0)
                    and packs.member_gen(_d, ZIP_TILE) >= THUMB_GEN):
                _link_zip_tile(zrow, path, index)
                return 1
        except (packs.PackError, OSError):
            pass
    ents = q("SELECT * FROM zip_entries WHERE zip_id=? AND kind='image'"
             " ORDER BY name LIMIT 4", (zip_id,))
    if not ents:
        # the central directory may not be indexed yet (a zip is normally
        # indexed when opened). Reading it is metadata-only -- no images decoded
        # -- so a tile can be made from a zip the user has never entered.
        try:
            index_zip(zip_id)
        except Exception:
            return 0
        ents = q("SELECT * FROM zip_entries WHERE zip_id=? AND kind='image'"
                 " ORDER BY name LIMIT 4", (zip_id,))
    if not ents:
        return 0
    cells = []
    for er in ents:
        try:
            cells.append(Image.open(io.BytesIO(zip_entry_bytes(zrow, er))).convert("RGB"))
        except Exception:
            continue
    if not cells:
        return 0
    blob = _jpeg_bytes(_quad(cells))
    try:
        _store_in_pack(zrow["loc_id"], "zip", zip_id, f"z:{zrow['rel']}",
                       os.path.dirname(node_abspath(zrow)), zrow["name"],
                       {ZIP_TILE: blob}, {ZIP_TILE: ("nodes", zip_id)},
                       {ZIP_TILE: (zrow["size"], zrow["mtime"])})
    except OSError:
        exec_many("UPDATE nodes SET thumb_state='pending' WHERE id=?", (zip_id,))
        return 0
    events.publish_thumb(zip_id, blob, int(float(zrow["mtime"] or 0) * 1000),
                         parent=zrow["parent_id"], zip_id=zip_id)
    return 1


def _jpeg_bytes(im):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=THUMB_QUALITY, optimize=True)
    return buf.getvalue()


def _quad(cells):
    """Compose up to four pictures into ONE 2x2 tile, cropping nothing.

    Each quadrant letterboxes its picture exactly as a tile-sized thumbnail does
    (whole frame, black bars), so a portrait photo inside a zip or a folder shows
    its full height instead of losing its top and bottom.

    One picture, one quadrant: the rest stay empty. The old code cycled the list
    (`cells[i % len(cells)]`) so a folder holding a single photo showed that photo
    four times, which read as four files rather than one. The 2x2 frame is kept
    either way, so a sparse folder still lines up with its neighbours in the grid.
    """
    tw, th = THUMB_MAX
    cw, ch = tw // 2, th // 2
    tile = Image.new("RGB", (tw, th), (18, 18, 20))
    for i, im in enumerate(cells[:4]):
        try:
            cell = ImageOps.pad(im, (cw, ch), method=Image.LANCZOS, color=(0, 0, 0))
        except Exception:
            continue
        tile.paste(cell, ((i % 2) * cw, (i // 2) * ch))
    return tile


def _child_thumb(row, kind):
    """That child's own picture, exactly as the grid shows it, straight from its pack."""
    if row["thumb_state"] != "ready" or not row["pack_id"]:
        return None
    name = (f"v{row['id']}/{POSTER_NAME}" if kind == "video"
            else ZIP_TILE if kind == "zip" else row["name"])
    prow = one("SELECT path FROM thumb_pack WHERE id=?", (row["pack_id"],))
    if not prow:
        return None
    try:
        return packs.item(prow["path"], name)
    except (packs.PackError, OSError, KeyError):
        return None


def _zip_face(zrow):
    """One photo from inside a zip — what a folder's mosaic uses for a zip child.

    A zip's own tile is already a 2x2 of four photos. Putting four of those in the
    folder's 2x2 made sixteen stamps that read as grey noise once the tile is drawn
    at 300x225, so the folder takes ONE picture per zip instead and each picture
    stays legible. Falls back to the zip's tile (caller) when the archive holds no
    readable image.
    """
    first = ("SELECT * FROM zip_entries WHERE zip_id=? AND kind='image'"
             " ORDER BY name LIMIT 1")
    ent = one(first, (zrow["id"],))
    if not ent:
        # not indexed yet: reading the central directory is metadata-only
        try:
            index_zip(zrow["id"])
        except Exception:
            return None
        ent = one(first, (zrow["id"],))
    if not ent:
        return None
    full = node_by_id(zrow["id"]) or zrow
    try:
        return zip_entry_bytes(full, ent)
    except Exception:
        return None


def child_cells(row, limit=4):
    """One picture per child: images, then zips, then videos, then subfolder tiles.

    Built from the thumbnails that already exist rather than from the files, so a
    folder's face costs a handful of pack reads instead of a full-size decode per
    picture and an ffmpeg run per clip. Before this a folder holding a zip and a
    video showed only the zip: dir_samples only looked at videos when the folder had
    nothing else, so the video had no cell at all.

    A zip child contributes one photo from inside the archive, not its own 2x2 tile:
    four tiles side by side made a sixteen-stamp folder face nobody could read."""
    kids = q("SELECT id, kind, thumb_state, pack_id, name FROM nodes"
             " WHERE loc_id=? AND parent_id IS ? ORDER BY name", (row["loc_id"], row["id"]))
    buckets = {"image": [], "zip": [], "video": [], "dir": []}
    for k in kids:
        if k["kind"] in buckets:
            buckets[k["kind"]].append(k)
    cells = []
    for kind in ("image", "zip", "video", "dir"):
        for k in buckets[kind]:
            if len(cells) >= limit:
                return cells
            blob = _zip_face(k) if kind == "zip" else None
            if not blob:
                blob = _child_thumb(k, kind)
            if not blob:
                continue
            try:
                im = Image.open(io.BytesIO(blob))
                if kind == "zip":
                    # a full-size archive photo, not a tile: draft() decodes JPEG at
                    # 1/4-1/8 scale, so a quadrant costs a fraction of a full decode
                    # and is indistinguishable once padded to 150x112
                    try:
                        im.draft("RGB", (600, 450))
                    except Exception:
                        pass
                cells.append(im.convert("RGB"))
            except Exception:
                continue
    return cells


def dir_samples(dir_abs, limit=4, depth=1):
    """Up to `limit` pictures that stand for a folder, cheapest source first:
    loose images, then the first picture inside each of its zips, then one frame
    from a video.

    A category folder holds no media of its own -- only other folders -- so when
    the directory itself yields nothing it may look ONE level into its children
    (depth=1). Bounded on purpose: a folder's tile must never turn into a tree
    walk."""
    imgs, zips, vids, subs = [], [], [], []
    try:
        with os.scandir(dir_abs) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    subs.append(e.path)
                    continue
                ext = os.path.splitext(e.name)[1].lower()
                if ext in IMAGE_EXTS:
                    imgs.append(e.path)
                elif ext == ".zip":
                    zips.append(e.path)
                elif ext in VIDEO_EXTS:
                    vids.append(e.path)
    except OSError:
        return []
    out = []
    for p in sorted(imgs):
        if len(out) >= limit:
            return out
        try:
            out.append(Image.open(p).convert("RGB"))
        except Exception:
            continue
    for zp in sorted(zips):
        if len(out) >= limit:
            return out
        try:
            with zipfile.ZipFile(zp) as z:
                names = [n for n in z.namelist() if not n.endswith("/")
                         and os.path.splitext(n)[1].lower() in IMAGE_EXTS]
                if not names:
                    continue
                out.append(Image.open(io.BytesIO(z.read(names[0]))).convert("RGB"))
        except Exception:
            continue
    for vp in sorted(vids):          # a clip contributes its poster like any other item
        if len(out) >= limit:
            return out
        try:
            out.append(Image.open(io.BytesIO(poster_bytes(vp, probe_duration(vp))))
                       .convert("RGB"))
        except Exception:
            continue
    if len(out) < limit and depth > 0:   # a category folder: borrow from its children
        for sd in sorted(subs)[:6]:
            if len(out) >= limit:
                break
            out.extend(dir_samples(sd, limit - len(out), depth - 1))
            if len(out) >= limit:
                break
    return out[:limit]


def gen_dir_tile(dir_id, force=False):
    """A folder's own tile: the same 2x2 mosaic a zip gets, so a folder shows its
    content instead of a glyph. Stored in the PARENT's pack under the folder's
    name, which is exactly where an image's own thumbnail already lives."""
    row = node_by_id(dir_id)
    if not row or row["kind"] != "dir":
        return 0
    if row["thumb_state"] == "ready" and not force:
        return 0
    cells = child_cells(row) or dir_samples(node_abspath(row))
    if not cells:
        # looked, found nothing worth showing: keep the glyph and stop asking
        exec_many("UPDATE nodes SET thumb_state='empty' WHERE id=?", (dir_id,))
        return 0
    pid = row["parent_id"]
    parent = node_by_id(pid) if pid else None
    owner, hint = folder_pack_spot(row["loc_id"], parent)
    key = f"d:{parent['rel'] if parent else ''}"
    blob = _jpeg_bytes(_quad(cells))
    try:
        _store_in_pack(row["loc_id"], "dir", pid, key, owner, hint,
                       {row["name"]: blob},
                       {row["name"]: ("nodes", dir_id)},
                       {row["name"]: (row["size"], row["mtime"])})
    except OSError:
        exec_many("UPDATE nodes SET thumb_state='pending' WHERE id=?", (dir_id,))
        return 0
    events.publish_thumb(dir_id, blob, int(float(row["mtime"] or 0) * 1000),
                         parent=pid)
    if pid:
        # the parent's mosaic is made of its children's pictures, so it is stale the
        # moment one of them is built. queue_dir_tile dedupes, so 161 children cost
        # one pending rebuild rather than 161.
        queue_dir_tile(pid, force=True)
    return 1


# A folder view can list dozens of subfolders at once, and every one of them wants a
# 2x2 mosaic. Building them all at once means hundreds of decodes; dropping the extras
# leaves the grid half grey and nothing ever retries them. So they are QUEUED (first
# one first) and drained by a few workers: the decode load per second is unchanged, the
# grid simply finishes filling in instead of stopping. The backlog is bounded only so a
# pathological view (hundreds of subfolders) cannot park work for minutes.
DIR_TILE_WORKERS = int(os.environ.get("MX_DIR_TILE_WORKERS", "4"))
DIR_TILE_BACKLOG = int(os.environ.get("MX_DIR_TILES", "400"))
_dirq = {"q": collections.deque(), "queued": set(), "running": 0}
_dirq_lock = threading.Lock()
_dirq_workers = {"started": False}


def _dir_tile_worker():
    while True:
        with _dirq_lock:
            item = _dirq["q"].popleft() if _dirq["q"] else None
        if item is None:
            time.sleep(0.2)
            continue
        dir_id, force = item
        with _dirq_lock:
            _dirq["running"] += 1
        try:
            gen_dir_tile(dir_id, force)
        except Exception:
            traceback.print_exc()
        finally:
            with _dirq_lock:
                _dirq["running"] -= 1
                _dirq["queued"].discard(dir_id)


# Folder pack builds used to go through a ThreadPoolExecutor, which only appends: a
# scan that queued three hundred folders left the folder the user had just opened at
# the back of the line. This queue takes work at either end instead.
PACK_WORKERS = int(os.environ.get("MX_PACK_WORKERS", "4"))
_pack_q = collections.deque()
_pack_cv = threading.Condition()
_pack_started = [0]


def _pack_worker():
    while True:
        with _pack_cv:
            while not _pack_q:
                _pack_cv.wait()
            loc_id, pid, front = _pack_q.popleft()
        try:
            _safe_folder_pack(loc_id, pid, front)
        except Exception:
            traceback.print_exc()


def submit_folder_pack(loc_id, pid, front=False):
    """Queue one folder's pack build; front=True puts it ahead of everything waiting.

    That is the whole point: a scan asked for *now* must not line up behind a subtree
    scan's backlog of folders."""
    with _pack_cv:
        if _pack_started[0] < PACK_WORKERS:
            _pack_started[0] += 1
            threading.Thread(target=_pack_worker, name=f"pack{_pack_started[0]}",
                             daemon=True).start()
        item = (loc_id, pid, front)
        for old in [x for x in _pack_q if x[:2] == (loc_id, pid)]:
            _pack_q.remove(old)             # already waiting: move it, do not repeat
        if front:
            _pack_q.appendleft(item)
        else:
            _pack_q.append(item)
        _pack_cv.notify()


def queue_dir_tile(dir_id, force=False, front=False):
    """Queue one folder's 2x2 mosaic.

    This used to DROP the request once eight were in flight, so a folder holding 20+
    subfolders built the first handful and then silently stopped; the rest appeared
    only after leaving and coming back, because nothing retried them. Every tile is
    queued now and four workers drain the queue in order."""
    with _dirq_lock:
        if dir_id in _dirq["queued"]:
            return False                       # already waiting or building
        if not force and len(_dirq["q"]) >= DIR_TILE_BACKLOG:
            return False                       # very large view: next visit picks it up
        _dirq["queued"].add(dir_id)
        if front:
            _dirq["q"].appendleft((dir_id, force))
        else:
            _dirq["q"].append((dir_id, force))
        if not _dirq_workers["started"]:
            _dirq_workers["started"] = True
            for i in range(DIR_TILE_WORKERS):
                threading.Thread(target=_dir_tile_worker, name=f"dirtile{i}",
                                 daemon=True).start()
    return True


def build_stats():
    """Background thumb work right now: {active, queued, by_kind}.

    Andy's folder tiles "stopped after five or eight" because a throttle dropped
    requests and nothing counted the leftovers. A number that only ever goes down is
    the cheapest way to make that visible: active+queued must reach 0, and if it sits
    still for half a minute something is wedged."""
    with _inflight_lock:
        keys = list(_inflight)
    by = {}
    for k in keys:
        kind = k[0] if isinstance(k, tuple) else str(k)
        by[kind] = by.get(kind, 0) + 1
    with _dirq_lock:
        dir_running = _dirq["running"]
        dir_queued = len(_dirq["q"])
    by["dirtile"] = by.get("dirtile", 0) + dir_running
    return {"active": len(keys) + dir_running, "queued": dir_queued, "by_kind": by}


def relink_orphans():
    """Re-point rows at a pack member that still exists.

    A geometry change only orphans a thumbnail when the row loses its (pack, off,
    len). Relinking restores the picture the library already had without decoding
    anything, so a re-encode happens only where someone asks for one by hand.
    """
    fixed = 0
    for loc in q("SELECT id FROM locations"):
        pending = q("SELECT * FROM nodes WHERE loc_id=? AND thumb_state='pending'"
                    " AND pack_id IS NULL", (loc["id"],))
        if not pending:
            continue
        want = {}
        for r in pending:
            pid = r["parent_id"]
            parent = node_by_id(pid) if pid else None
            if r["kind"] == "zip":
                key, member = f"z:{r['rel']}", ZIP_TILE
            else:
                key = f"d:{parent['rel'] if parent else ''}"
                member = (f"v{r['id']}/{POSTER_NAME}" if r["kind"] == "video"
                          else r["name"])
            want.setdefault(key, []).append((r["id"], member))
        for key, items in want.items():
            prow = one("SELECT * FROM thumb_pack WHERE loc_id=? AND key=?",
                       (loc["id"], key))
            if not prow:
                continue
            try:
                _b, index, _m = packs.read(Path(prow["path"]))
            except Exception:
                continue
            for node_id, member in items:
                e = index.get(member)
                if not e:
                    continue
                exec_many("UPDATE nodes SET pack_id=?, pack_off=?, pack_len=?,"
                          " thumb_state='ready', thumb_gen=? WHERE id=?",
                          (prow["id"], e[0], e[1], packs.member_gen(_m, member), node_id))
                fixed += 1
    return fixed


def rebuild_scope(node_id):
    """Force ONE unit's thumbnails to be rebuilt: a folder's children (images,
    video posters, zip tiles, subfolder tiles) or a zip's entries.

    Deliberately manual -- nothing here is ever called on startup, so a geometry
    change cannot sweep the library. The caller picks folders one at a time.
    """
    row = node_by_id(node_id) if node_id else None
    if row and row["kind"] == "video":
        # A single clip: back to the two-image preview, exactly what a folder re-encode
        # does for every clip in it. Without this branch the call fell through to the
        # folder path with the VIDEO as its parent -- no children to re-pend, so the
        # strip stayed on offer (owner: "does the right-pane one replace with 2 images?"
        # -- it did not) and a folder tile was queued for a node that has no children.
        exec_many("UPDATE nodes SET thumb_state='pending', pack_id=NULL, pack_off=NULL,"
                  " pack_len=NULL, preview_only=1 WHERE id=?", (row["id"],))
        queue_video_poster(row["id"], force=True)
        return {"scope": "video", "queued": 1, "filmstrips": 0, "trimmed": 1}
    if row and row["kind"] == "image":
        # One picture: no children, no strip -- re-encode just this tile.
        exec_many("UPDATE nodes SET thumb_state='pending', pack_id=NULL, pack_off=NULL,"
                  " pack_len=NULL WHERE id=?", (row["id"],))
        queue_node(row["id"])
        return {"scope": "image", "queued": 1, "filmstrips": 0, "trimmed": 0}
    if row and row["kind"] == "zip":
        zid = row["id"]
        exec_many("UPDATE zip_entries SET thumb_state='pending', pack_id=NULL,"
                  " pack_off=NULL, pack_len=NULL WHERE zip_id=?", (zid,))
        exec_many("UPDATE nodes SET thumb_state='pending', pack_id=NULL, pack_off=NULL,"
                  " pack_len=NULL WHERE id=?", (zid,))
        _once(("ziprebuild", zid), lambda: gen_zip_pack(zid, force=True))
        queue_zip_tile(zid, force=True)
        return {"scope": "zip", "queued": one(
            "SELECT COUNT(*) c FROM zip_entries WHERE zip_id=?", (zid,))["c"] + 1}
    loc_id = row["loc_id"] if row else None
    parent_id = row["id"] if row else None
    if not loc_id:                      # root of a location
        hit = q("SELECT id FROM locations ORDER BY id LIMIT 1")
        if not hit:
            return {"scope": "none", "queued": 0}
        loc_id = hit[0]["id"]
    kids = q("SELECT id, kind, preview_only FROM nodes WHERE loc_id=? AND parent_id IS ?",
             (loc_id, parent_id))
    for r in kids:
        if r["kind"] in ("image", "video", "zip", "dir"):
            # Re-encode also puts every clip back to the two-image preview: it is the
            # "I don't want that many thumbs for a video" button (owner's request), so
            # the strip is hidden rather than refreshed. Asking for filmstrips again
            # ("Build filmstrips …") clears the flag.
            exec_many("UPDATE nodes SET thumb_state='pending', pack_id=NULL,"
                      " pack_off=NULL, pack_len=NULL"
                      + (", preview_only=1" if r["kind"] == "video" else "") +
                      " WHERE id=?", (r["id"],))
    if parent_id is not None:
        exec_many("UPDATE nodes SET thumb_state='pending', pack_id=NULL, pack_off=NULL,"
                  " pack_len=NULL WHERE id=?", (parent_id,))
    _once(("folder", loc_id, parent_id, "force"),
          lambda: gen_folder_thumbs(loc_id, parent_id, force=True))
    trimmed = 0
    for r in kids:
        if r["kind"] == "video":
            # poster + ONE later still, re-extracted: the strip is not rebuilt here
            # (9x the ffmpeg work, and a video-heavy folder must not turn a re-encode
            # into an hour of encoding).
            queue_video_poster(r["id"], force=True)
            trimmed += 1
        elif r["kind"] == "zip":
            queue_zip_tile(r["id"], force=True)
        elif r["kind"] == "dir":
            queue_dir_tile(r["id"], force=True)
    if parent_id is not None:
        queue_dir_tile(parent_id, force=True)     # its own tile, in the parent pack
    return {"scope": "folder", "queued": len(kids) + (1 if parent_id else 0),
            "filmstrips": 0, "trimmed": trimmed}


def queue_zip_tile(zip_id, force=False):
    _once(("ztile", zip_id, force), lambda: gen_zip_tile(zip_id, force))


def gen_zip_pack(zip_id, ids=None, force=False, front=False):
    """All pending image entries of one zip -> ONE pack write, sitting beside
    the zip so it travels with the zip through renames and moves."""
    zrow = node_by_id(zip_id)
    if not zrow:
        return 0
    sel = set(ids) if ids else None
    zabs = node_abspath(zrow)
    owner = os.path.dirname(zabs)
    key = f"z:{zrow['rel']}"

    def pending():
        rows = q("SELECT * FROM zip_entries WHERE zip_id=? AND kind='image'"
                 f" AND ({THUMB_WORK})", (zip_id,))
        # same order the entries listing uses (/api/zip/{id}/entries is natsorted):
        # without this the fill order followed the archive, which reads as random
        return natsorted([r for r in rows if sel is None or r["id"] in sel], key=lambda r: r["name"])

    rows = pending()
    if not rows:
        return 0
    # entries carry the zip's mtime: a moved zip keeps its content and its pack
    if not force and adopt_pack(zrow["loc_id"], "zip", zip_id, key, owner, zrow["name"],
                                rows, "zip_entries", lambda r: (r["size"], zrow["mtime"])):
        rows = pending()
        if not rows:
            return 0
    members, targets, meta = {}, {}, {}

    def one_entry(er):
        try:
            blob = encode_thumb(zip_entry_bytes(zrow, er))
        except Exception:
            exec_many("UPDATE zip_entries SET thumb_state='error' WHERE id=?", (er["id"],))
            return er, None
        events.publish_thumb(er["id"], blob, int(float(zrow["mtime"] or 0) * 1000),
                             kind="entry", parent=zrow["parent_id"], zip_id=zrow["id"])
        return er, blob

    for er, blob in _pmap(one_entry, rows, front=front):
        if blob is None:
            continue
        members[er["name"]] = blob
        targets[er["name"]] = ("zip_entries", er["id"])
        meta[er["name"]] = (er["size"], zrow["mtime"])
    if not members:
        return 0
    try:
        _store_in_pack(zrow["loc_id"], "zip", zip_id, key, owner, zrow["name"],
                       members, targets, meta)
    except OSError:
        for _name, (_tbl, tid) in targets.items():
            exec_many("UPDATE zip_entries SET thumb_state='pending' WHERE id=?", (tid,))
        raise
    return len(members)


def queue_node(node_id):
    _executor.submit(gen_node_thumb, node_id)


def queue_entry(zip_id, entry_id):
    _executor.submit(gen_entry_thumb, zip_id, entry_id)


_inflight = set()
_inflight_lock = threading.Lock()


def _once(key, fn, pool=None):
    """Run fn in the pool unless an identical job is already in flight.

    Nav-triggered builds are now background (the listing returns at once), so a
    user clicking back and forth would otherwise stack duplicate jobs that all
    rewrite the same pack."""
    with _inflight_lock:
        if key in _inflight:
            return False
        _inflight.add(key)
    def run():
        try:
            fn()
        finally:
            with _inflight_lock:
                _inflight.discard(key)
    (pool or _executor).submit(run)
    return True


def queue_zip_pack(zip_id):
    _once(("zip", zip_id), lambda: _safe_gen_zip_pack(zip_id, front=True), pool=_fast)


def queue_folder_pack(loc_id, parent_id):
    _once(("dir", loc_id, parent_id), lambda: _safe_folder_pack(loc_id, parent_id, front=True),
          pool=_fast)


def _safe_gen_zip_pack(zip_id, front=False):
    try:
        gen_zip_pack(zip_id, front=front)
    except Exception:
        traceback.print_exc()      # a silent failure here means thumbs that never appear
    finally:
        events.publish_done(zip_id, "zip")


def _safe_folder_pack(loc_id, parent_id, front=False):
    try:
        gen_folder_thumbs(loc_id, parent_id, front=front)
    except Exception:
        traceback.print_exc()
    finally:
        # end of a build: the page re-lists this folder once, which picks up
        # thumbs it never saw an event for and files added since it rendered
        events.publish_done(parent_id, "dir")
        if parent_id is not None:
            queue_dir_tile(parent_id, force=True)   # its face is made of these


# ---------------------------------------------------------------- scanning

def _classify(name, is_dir):
    if is_dir:
        return "dir"
    ext = os.path.splitext(name)[1].lower()
    if ext == ".zip":
        return "zip"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def sweep_strays(dpath, present, entries=None):
    """Opportunistic gc, run on every folder entry (and scan): a hidden pack in
    this directory whose unit is no longer here was deleted or moved elsewhere —
    drop it. If it was a rename, the thumbs are simply rebuilt on next view.
    Packs are only removed when their name matches no entry in the listing, so
    a live unit's cache can never be swept."""
    from .config import PACK_EXT
    if entries is None:
        try:
            entries = list(os.scandir(dpath))
        except OSError:
            return 0
    removed = 0
    for e in entries:
        if not (e.name.startswith(".") and e.name.endswith(PACK_EXT)):
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                continue                # 0.1.17-era .mxthumbs/ directory
        except OSError:
            continue
        unit = e.name[1:-len(PACK_EXT)]
        if unit == "_root" or unit in present:
            continue                    # the location root's own pack, or a live unit
        # a pack is named after its unit's stem (`Trip.zip` -> `.Trip.mxthumbs`);
        # the full name is also live (legacy or a stem clash)
        if any(packs.short(packs.stem_name(n)) == unit for n in present):
            continue                    # long names are stored truncated
        prow = one("SELECT id FROM thumb_pack WHERE path=?", (e.path,))
        if prow is None:
            continue                    # not ours: never delete a stranger's file
        try:
            os.unlink(e.path)
        except OSError:
            continue
        exec_many("DELETE FROM thumb_pack WHERE id=?", (prow["id"],))
        packs.drop(e.path)
        removed += 1
    return removed


_dir_locks = {}
_dir_locks_guard = threading.Lock()


def _dir_lock(key):
    with _dir_locks_guard:
        lk = _dir_locks.get(key)
        if lk is None:
            lk = _dir_locks[key] = threading.Lock()
        return lk


def diff_dir(loc_id, parent_id, root_abs, sweep=False, quick=False):
    """One-level metadata diff between filesystem and DB. Returns #new media items.

    Serialised per folder. Two callers reach the same directory all the time -- opening
    a folder indexes it while a Scan (or the eager backlog) indexes it too -- and both
    then saw no row for a just-discovered file and both INSERTed it, which raised
    'UNIQUE constraint failed: nodes.loc_id, nodes.parent_id, nodes.name' and reached
    the browser as HTTP 500 on /api/children and /api/tree. The lock closes the race
    inside one process; the INSERT is OR IGNORE as well, because a lock cannot survive
    a restart or a second worker.

    `quick` (browsing only, never a scan): skip the per-file re-check when the folder
    itself is unchanged since the last full one. See _QUICK_* below."""
    with _dir_lock((loc_id, parent_id)):
        return _diff_dir(loc_id, parent_id, root_abs, sweep, quick)


# Opening a folder re-checked every file in it: scandir + one stat per entry through
# Unraid's user-share layer, 20-580ms per listing (measured on dev) -- and the page
# re-lists while thumbnails build, so a folder could pay that several times a second.
# A directory's own mtime changes whenever an entry is added, removed or renamed, so
# when it has not moved since the last full check there is nothing new to find.
# What it cannot see is a file rewritten IN PLACE under the same name; for that a full
# check still runs at least every _QUICK_FULL_EVERY seconds, and Scan always does one.
_QUICK_FULL_EVERY = 300.0
_dir_seen = {}          # (loc_id, parent_id) -> (dir mtime_ns, time of last full check)


def _diff_dir(loc_id, parent_id, root_abs, sweep=False, quick=False):
    if parent_id is None:
        rel_prefix = ""
    else:
        prow = node_by_id(parent_id)
        if not prow:
            return 0          # stale id (index rebuilt): nothing to diff here
        rel_prefix = prow["rel"] + "/"
    dpath = os.path.join(root_abs, rel_prefix) if rel_prefix else root_abs
    key = (loc_id, parent_id)
    try:
        dir_mtime = os.stat(dpath).st_mtime_ns
    except OSError:
        dir_mtime = None
    if quick and dir_mtime is not None:
        last = _dir_seen.get(key)
        if last and last[0] == dir_mtime and time.time() - last[1] < _QUICK_FULL_EVERY:
            return 0
    try:
        entries = list(os.scandir(dpath))
    except OSError:
        return 0
    if dir_mtime is not None:
        _dir_seen[key] = (dir_mtime, time.time())
    existing = {r["name"]: r for r in _db_children(loc_id, parent_id)}
    new_media = 0
    seen = set()
    for e in entries:
        kind = _classify(e.name, e.is_dir(follow_symlinks=False))
        if kind is None:
            continue
        seen.add(e.name)
        st = e.stat(follow_symlinks=False)
        rel = rel_prefix + e.name
        ex = existing.get(e.name)
        if ex is None:
            thumb_state = "pending" if kind in ("image", "video") else "none"
            exec_many(
                "INSERT OR IGNORE INTO nodes(loc_id,parent_id,name,rel,kind,size,mtime,thumb_state)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (loc_id, parent_id, e.name, rel, kind, st.st_size, st.st_mtime, thumb_state))
            if kind in ("image", "video"):
                new_media += 1
        elif ex["kind"] != kind or ex["size"] != st.st_size or abs(ex["mtime"] - st.st_mtime) > 1:
            # changed in place: drop old thumbnail reference (and the stored running
            # time -- a replaced clip is a different length), re-pend
            exec_many(
                "UPDATE nodes SET kind=?, size=?, mtime=?, thumb=NULL, dur=NULL,"
                " thumb_state=? WHERE id=?",
                (kind, st.st_size, st.st_mtime,
                 "pending" if kind in ("image", "video") else "none", ex["id"]))
            if kind == "zip" or ex["kind"] == "zip":
                exec_many("DELETE FROM zip_entries WHERE zip_id=?", (ex["id"],))
    for name, ex in existing.items():
        if name not in seen:
            if ex["kind"] == "zip":
                exec_many("DELETE FROM zip_entries WHERE zip_id=?", (ex["id"],))
            exec_many("DELETE FROM nodes WHERE id=?", (ex["id"],))
    if sweep:                    # browse only: a scan still needs strays to adopt
        sweep_strays(dpath, seen, entries)
    return new_media


def _db_children(loc_id, parent_id):
    return q("SELECT * FROM nodes WHERE loc_id=? AND parent_id IS ?", (loc_id, parent_id))


def diff_subtree(loc_id, parent_id, root_abs):
    """Recursive diff (scan-subfolders). Returns (folders_scanned, new_media)."""
    total_new = 0
    scanned = 0
    stack = [parent_id]
    while stack:
        pid = stack.pop()
        scanned += 1
        total_new += diff_dir(loc_id, pid, root_abs)
        for r in q("SELECT id FROM nodes WHERE loc_id=? AND parent_id IS ? AND kind IN ('dir','zip')",
                   (loc_id, pid)):
            stack.append(r["id"])
    return scanned, total_new


def queue_pending_for_subtree(loc_id, parent_id):
    """Queue thumbnail generation for pending media in subtree. Returns count queued."""
    ids = []
    stack = [parent_id]
    while stack:
        pid = stack.pop()
        for r in q("SELECT id,kind,thumb_state,pack_id FROM nodes WHERE loc_id=? AND parent_id IS ?",
                   (loc_id, pid)):
            if r["kind"] in ("image", "video") and (r["thumb_state"] != "ready"
                                                    or r["pack_id"] is not None and not _pack_alive(r["pack_id"])):
                ids.append(r["id"])
            if r["kind"] in ("dir", "zip"):
                stack.append(r["id"])
        if pid is None:
            pass
    for nid in ids:
        queue_node(nid)
    return len(ids)


# ---------------------------------------------------------------- zips

def index_zip(zip_id):
    row = node_by_id(zip_id)
    # unchanged since last index -> skip (110k-zip libraries must not re-read
    # every central directory on every scan)
    if row["zindexed"] == row["mtime"] and \
            one("SELECT 1 FROM zip_entries WHERE zip_id=? LIMIT 1", (zip_id,)):
        return []
    try:
        with zipfile.ZipFile(node_abspath(row)) as zf:
            infos = zf.infolist()
    except (OSError, zipfile.BadZipFile):
        return None
    # contents changed: drop stale rows, re-insert fresh
    exec_many("DELETE FROM zip_entries WHERE zip_id=?", (zip_id,))
    for info in infos:
        if info.is_dir():
            continue
        name = info.filename
        if name.startswith("__MACOSX") or os.path.basename(name).startswith("."):
            continue
        ext = os.path.splitext(name)[1].lower()
        kind = "image" if ext in IMAGE_EXTS else ("video" if ext in VIDEO_EXTS else "other")
        if kind == "other":
            continue
        ts = "pending" if kind == "image" else "none"
        exec_many(
            "INSERT OR IGNORE INTO zip_entries(zip_id,name,size,kind,thumb_state,"
            "zoffset,method,csize) VALUES(?,?,?,?,?,?,?,?)",
            (zip_id, name, info.file_size, kind, ts,
             info.header_offset, info.compress_type, info.compress_size))
    exec_many("UPDATE nodes SET zindexed=? WHERE id=?", (row["mtime"], zip_id))
    return infos


def zip_entry_bytes(zrow, erow):
    """Fast path: read an entry as one exact byte range from the zip.
    STORED entries (the spec's 'no compression' zips) are a raw pread —
    no zipfile object, no decompression, no whole-file buffering."""
    path = node_abspath(zrow)
    if erow["zoffset"] is None or erow["zoffset"] < 0:
        # row indexed by an older build: locate once, repair the row
        with zipfile.ZipFile(path) as zf:
            info = zf.getinfo(erow["name"])
        exec_many("UPDATE zip_entries SET zoffset=?, method=?, csize=? WHERE id=?",
                  (info.header_offset, info.compress_type, info.compress_size, erow["id"]))
        erow = dict(erow)
        erow["zoffset"] = info.header_offset
        erow["method"] = info.compress_type
        erow["csize"] = info.compress_size
    with open(path, "rb") as f:
        f.seek(erow["zoffset"])
        head = f.read(30)
        if len(head) < 30 or head[:4] != b"PK\x03\x04":
            raise ValueError("bad local header")
        name_len = int.from_bytes(head[26:28], "little")
        extra_len = int.from_bytes(head[28:30], "little")
        f.seek(erow["zoffset"] + 30 + name_len + extra_len)
        raw = f.read(erow["csize"])
        if erow["method"] == 0:          # STORED — zero-copy for the browser
            return raw
        import zlib
        return zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw)


def zip_has_video(zip_id):
    r = one("SELECT COUNT(*) c FROM zip_entries WHERE zip_id=? AND kind='video'", (zip_id,))
    return (r["c"] or 0) > 0


# ---------------------------------------------------------------- video frames

def frames_dir(row):
    h = hashlib.sha1(("f:" + node_abspath(row)).encode()).hexdigest()
    return THUMB_PATH / "frames" / h[:2] / h[2:]


def probe_duration(path, gate=True):
    """Clip length in seconds (0.0 if unreadable).

    `gate=False` skips _FGATE: the duration backfill is a demux-only ffprobe (tens of
    ms), and behind the gate it queued after full video encodes -- 43 lengths took
    ~20s on a busy dev box. It still runs at the build-pool priority (_PRIO)."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    try:
        # ffprobe only demuxes for duration; no decoder, so never hwaccel.
        if gate:
            out = ff_run(cmd, timeout=30, hw=False)
        else:
            out = subprocess.run(_PRIO + cmd, capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip()) if out else 0.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------- video durations
# The tile shows each clip's running time. The length is measured anyway by every
# preview/filmstrip build (it places the 3%/6% stills), so remember_duration() just
# stops throwing it away. Clips built before the column existed are filled in by
# queue_durations(): one ffprobe each (demux only, ~tens of ms), on a small pool of
# its own so it never queues behind ffmpeg encodes, deduped while in flight, and
# each answer pushed to the page as a "dur" event so the label appears in place.
_dur_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dur")
_dur_inflight = set()
_dur_lock = threading.Lock()


def remember_duration(node_id, dur):
    if dur and dur > 0:
        exec_many("UPDATE nodes SET dur=? WHERE id=?", (round(float(dur), 2), node_id))
        events.publish({"t": "dur", "id": node_id, "dur": round(float(dur), 2)})


def _probe_and_remember(node_id):
    try:
        row = node_by_id(node_id)
        if not row or row["kind"] != "video" or row["dur"]:
            return
        dur = probe_duration(node_abspath(row), gate=False)
        if dur > 0:
            remember_duration(node_id, dur)
        else:
            # unreadable/zero-length: store -1 so a broken clip is probed once, not on
            # every listing; the tile simply shows no label
            exec_many("UPDATE nodes SET dur=-1 WHERE id=?", (node_id,))
    except Exception as e:
        log.warning("duration probe for node %s failed: %s", node_id, e)
    finally:
        with _dur_lock:
            _dur_inflight.discard(node_id)


def queue_durations(rows):
    """Measure every video in this listing that has no stored length yet."""
    for r in rows:
        if r["kind"] != "video" or r["dur"] is not None:
            continue
        with _dur_lock:
            if r["id"] in _dur_inflight:
                continue
            _dur_inflight.add(r["id"])
        _dur_pool.submit(_probe_and_remember, r["id"])


#[video: poster + filmstrip live in ONE pack per video]

POSTER_NAME = "poster.jpg"
# The second image of the two-image preview lives under its own name. It used to be
# written as f00 -- the same name a filmstrip uses for its FIRST frame (6% in) -- so
# after a strip existed, "the second image" silently meant the opening shot, and a
# preview-only clip inherited that frame. Separate names, separate meanings.
PREVIEW2 = "p1.jpg"

# The preview flip alternates between two moments of a clip, so the default build
# makes TWO images, not one. A second still is one more GOP decode inside the same
# ffmpeg process (the inputs decode in parallel), so wall time barely moves.
# 3% and 6% (owner's pick, Nov 2026): far enough in to clear a graphical intro card on a
# long clip, close enough that the two images still read as the same clip. The 2s floor
# only bites below ~67 s, where 3% would land in the first second -- exactly the region
# the owner wants clear of intros and camera-startup black.
POSTER_FRAC = 0.03          # first image: 3% into the clip ...
POSTER_MIN = 2.0            # ... but never inside the first two seconds
SECOND_FRAC = 0.06          # second image: 6% into the clip (the owner's number)
SECOND_MIN_SEC = 4.0        # ... with a floor so short clips still differ visibly
SECOND_SEEK = 40.0          # fallback when the duration could not be probed
SECOND_MIN = 12.0           # shorter clips get a single frame


def _frame_name(i):
    return f"f{i:02d}.jpg"


def _ff_jpg(tail, timeout=120):
    """Run ffmpeg into a scratch jpg and return its bytes (None on failure)."""
    tmpdir = THUMB_PATH / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    dest = tmpdir / f"ff-{os.getpid()}-{threading.get_ident()}.jpg"
    try:
        r = ff_run_hw_fb(tail + ["-y", str(dest)], timeout=timeout)
        if r and r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return dest.read_bytes()
        return None
    finally:
        dest.unlink(missing_ok=True)


# Whole frame inside 300x225 with black bars. force_divisible_by=2 keeps the
# scaled size even (mjpeg), and the named pad args avoid "Padded dimensions
# cannot be smaller than input dimensions" that the shorthand chain throws on
# portrait sources.
PAD225 = ("scale=w=300:h=225:force_original_aspect_ratio=decrease:force_divisible_by=2,"
          "pad=w=300:h=225:x=(ow-iw)/2:y=(oh-ih)/2:color=black")


def poster_bytes(src: str, dur: float):
    """Poster frame, exact 300x225 letterboxed like image masters. Seeks ~5% in
    (skips black/logo frames) with fallbacks for short videos."""
    for seek in (min(5.0, dur * 0.05) if dur > 0 else 5.0, 0.5, 0.0):
        blob = _ff_jpg(["ffmpeg", "-v", "error", "-threads", THREADS,
                        "-ss", f"{seek:.2f}", "-i", src, "-frames:v", "1",
                        # decrease+pad: whole frame, black bars, same as image
                        # masters. Named args + force_divisible_by: the shorthand
                        # chain "fails to configure pad" on portrait sources.
                        "-vf", PAD225,
                        "-q:v", "4"])
        if blob:
            return blob
    return None


def _stills(src: str, jobs):
    """Render [(member name, seek seconds, filter)] from ONE ffmpeg process.

    Each output carries its own -ss, so the file is still seeked per still, but the
    decoder, the hardware device and the process are set up once instead of N
    times. Returns {member_name: jpeg_bytes} (missing keys were not produced)."""
    tmpdir = THUMB_PATH / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    out = tmpdir / f"v-{os.getpid()}-{threading.get_ident()}"
    out.mkdir(exist_ok=True)
    names = []
    for idx, (member, seek, vf) in enumerate(jobs):
        names.append((member, seek, vf, out / f"{idx:02d}.jpg"))
    args = ["ffmpeg", "-v", "error", "-threads", THREADS]
    for _m, seek, _vf, _dest in names:
        args += ["-ss", f"{seek:.2f}", "-i", src]
    for idx, (_m, _seek, vf, dest) in enumerate(names):
        args += ["-map", f"{idx}:v:0", "-frames:v", "1", "-vf", vf,
                 "-q:v", "4", "-y", str(dest)]
    try:
        # CPU on purpose: with seeks this small the decode is a couple of GOPs per
        # frame, and NVDEC's per-input device setup costs more than it saves.
        r = ff_run(args, timeout=300, hw=False)
        if r is None or r.returncode != 0:
            # A failed multi-output run returns NOTHING for every still, and the
            # caller then falls back to one poster -- the strip looks "not rebuilt"
            # with no reason anywhere. Log the ffmpeg verdict, not just a bool.
            log.warning("stills for %s failed: rc=%s err=%s seeks=%s", src,
                        None if r is None else r.returncode,
                        (getattr(r, "stderr", b"") or b"")[-300:],
                        [round(s, 2) for _m, s, _v in jobs])
            return {}
        got = {}
        for member, _seek, _vf, dest in names:
            try:
                if dest.exists() and dest.stat().st_size > 0:
                    got[member] = dest.read_bytes()
            except OSError:
                pass
        return got
    finally:
        for _m, _seek, _vf, dest in names:
            try:
                dest.unlink()
            except OSError:
                pass
        try:
            out.rmdir()
        except OSError:
            pass


def poster_seek(dur: float) -> float:
    """Where the FIRST image comes from: 2% in, never inside the first two seconds."""
    if dur <= 0:
        return 5.0
    return min(max(dur * POSTER_FRAC, POSTER_MIN), max(dur * 0.5, POSTER_MIN))


def second_seek(dur: float) -> float:
    """Where the SECOND image comes from: 6% in (owner's number), floored at 4s.

    It has been 40 s flat, then "past halfway": a flat offset is the opening scene on
    a long clip, and a half-way offset is the end credits on a short one. A fraction of
    the clip keeps the two images apart at every length, and the floor keeps them from
    collapsing onto the same frame when the clip is brief.
    """
    if dur <= 0:
        return SECOND_SEEK
    lo = min(SECOND_MIN_SEC, dur * 0.5)
    hi = max(lo, dur - 2.0)
    return min(max(dur * SECOND_FRAC, lo), hi)


def preview_members(src: str, dur: float):
    """Poster + ONE more still: the default two-image preview, one process."""
    jobs = [(POSTER_NAME, poster_seek(dur), PAD225)]
    if dur >= SECOND_MIN:                      # a very short clip has no second moment
        jobs.append((PREVIEW2, second_seek(dur), PAD225))
    else:
        # A clip with no duration looks like "no second moment" and silently leaves
        # the clip with ONE image -- which reads exactly like "the flip never got
        # built". Say so, once per clip, instead of failing quiet.
        log.warning("preview for %s: duration %ss < %ss -- single still only",
                    src, round(dur or 0, 2), SECOND_MIN)
    got = _stills(src, jobs)
    missing = [n for n, _s, _v in jobs if n not in got]
    if missing:
        log.warning("preview for %s: ffmpeg produced no %s (wanted seeks %s)",
                    src, missing, [round(s, 2) for n, s, _v in jobs if n in missing])
    return got


def video_members(src: str, dur: float):
    """Poster + filmstrip frames from ONE ffmpeg process."""
    jobs = []                                  # (member name, seek seconds, filter)
    # same geometry as poster_bytes: whole frame, black bars. This path (one
    # ffmpeg for poster + filmstrip) was still cropped, so a poster looked
    # different depending on which code built it.
    jobs.append((POSTER_NAME, poster_seek(dur), PAD225))
    if dur > 0:
        for i in range(FRAME_COUNT):
            jobs.append((_frame_name(i), dur * (i + 0.5) / FRAME_COUNT,
                         "scale=300:225:force_original_aspect_ratio=decrease"))
    return _stills(src, jobs)


def frame_bytes(src: str, t: float):
    """One filmstrip frame: fits inside 300x225, true aspect kept."""
    return _ff_jpg(["ffmpeg", "-v", "error", "-threads", THREADS,
                    "-ss", f"{t:.2f}", "-i", src, "-frames:v", "1",
                    "-vf", "scale=300:225:force_original_aspect_ratio=decrease",
                    "-q:v", "4"])


def video_folder_spot(row):
    """(owner_dir, name_hint, key) of the FOLDER pack that carries this video.

    A video's poster and filmstrip are members of the pack of the folder that
    contains it -- `.Holiday.mxthumbs` holds Holiday's images AND its videos --
    so a folder never grows one extra dot-file per clip."""
    pid = row["parent_id"]                      # sqlite3.Row has no .get()
    parent = node_by_id(pid) if pid else None
    owner, hint = folder_pack_spot(row["loc_id"], parent)
    key = f"d:{parent['rel'] if parent else ''}"
    return owner, hint, key


ZIP_TILE = "\x01tile.jpg"      # reserved member: a zip's 4-up tile. Real entry
                              # names are relative paths, never a control char.


def video_ns(node_id):
    """Member namespace for one video inside its folder pack. Image members are
    bare file names, which can never contain '/', so these cannot collide."""
    return f"v{node_id}/"


def video_member_bytes(row, name):
    """Bytes of one filmstrip member (poster/f00..f07), or None."""
    owner, hint, key = video_folder_spot(row)
    path = packs.resolve(row["loc_id"], key, owner, hint)
    try:
        return packs.item(path, video_ns(row["id"]) + name)
    except (packs.PackError, OSError, KeyError):
        return None


def _link_poster(row, path, index, ns=None, gen=None):
    """Point a video row at its existing poster member.

    `gen` is the generation of the BYTES found in the pack, and it is recorded on
    the row: stamping THUMB_GEN on adopted bytes would declare a cover-cropped
    poster current, and stamping nothing leaves the row looking stale forever, so
    every scan re-pends it and adoption hands back the same old picture.
    """
    name = (ns or "") + POSTER_NAME
    if name not in index:
        return
    off, ln = index[name][:2]
    _owner, _hint, key = video_folder_spot(row)
    prow = one("SELECT id FROM thumb_pack WHERE loc_id=? AND key=?", (row["loc_id"], key))
    if not prow:
        return
    exec_many("UPDATE nodes SET pack_id=?, pack_off=?, pack_len=?, thumb_state='ready',"
              " thumb_gen=? WHERE id=?",
              (prow["id"], off, ln, int(gen or 0), row["id"]))


def existing_frames(node_id):
    """Frame names IF the filmstrip is already built. Never builds anything.

    Hover is a read: a pointer sweeping a grid used to trigger 9-ffmpeg builds
    per tile, which is what pegged the CPU while browsing. The strip is built
    only when asked for (context menu) or when MX_HOVER_BUILD=1."""
    row = node_by_id(node_id)
    if not row or row["kind"] != "video":
        return []
    ns = video_ns(node_id)
    owner, hint, key = video_folder_spot(row)
    path = packs.resolve(row["loc_id"], key, owner, hint)
    if not path.exists():
        return []
    try:
        _b, index, _d = packs.read(path)
    except (packs.PackError, OSError):
        return []
    hit = index.get(ns + POSTER_NAME)
    if hit and hit[3] != (row["size"] or 0):
        return []                       # source changed: the strip is stale
    # What the flip can show: the two-image preview's second still (p1) and/or strip frames.
    strip = [_frame_name(i) for i in range(FRAME_COUNT)]
    strip = [n for n in strip if ns + n in index]
    has_poster = ns + POSTER_NAME in index
    has_p1 = ns + PREVIEW2 in index

    if row["preview_only"]:
        # "Re-encode thumbnails" trims a clip back to the two-image preview (poster at
        # POSTER_FRAC, second still at SECOND_FRAC). The old strip stays in the pack --
        # append-only, nothing rewrites offsets for a cosmetic change -- but it is not
        # offered here, which is what "back to 2 images" means to the owner.
        second = PREVIEW2 if has_p1 else (strip[0] if strip else None)
        return ([POSTER_NAME] if has_poster else []) + ([second] if second else [])

    names = list(strip)
    if not names and has_p1:
        names = [PREVIEW2]             # a two-image clip that never got a strip
    if not names:
        return []                      # nothing to flip to
    if has_poster:
        names.insert(0, POSTER_NAME)   # the flip starts on the full-frame poster
    return names


def ensure_video_poster(node_id, force=False):
    """The poster plus one more still -- ONE ffmpeg call, POSTER_FRAC and SECOND_FRAC in.

    Two images are the DEFAULT state for every video, so the tile's flip has
    something to alternate with. More than that (the eight-frame filmstrip, 9x the
    work) waits until it is actually wanted -- "Build filmstrips in this folder"
    or a hover on a clip that already has one.

    force=True re-encodes the frame even when a current poster is already in the
    pack. Without it the adopt branch below is what "Re-encode thumbnails here"
    hit: the row was re-pended, the poster path found its own current bytes and
    re-linked them, so the verb re-encoded every image in the folder and no
    video at all."""
    row = node_by_id(node_id)
    if not row or row["kind"] != "video":
        return None
    ns = video_ns(node_id)
    owner, hint, key = video_folder_spot(row)
    path = packs.resolve(row["loc_id"], key, owner, hint)
    if path.exists() and not force:
        try:
            _b, index, _d = packs.read(path)
            hit = index.get(ns + POSTER_NAME)
            if (hit and hit[3] == (row["size"] or 0)
                    and packs.member_gen(_d, ns + POSTER_NAME) >= THUMB_GEN):
                _link_poster(row, path, index, ns,
                             packs.member_gen(_d, ns + POSTER_NAME))
                return path
        except (packs.PackError, OSError):
            pass
    src = node_abspath(row)
    dur = probe_duration(src)
    remember_duration(node_id, dur)
    # TWO stills by default, one ffmpeg: the tile's flip needs a second moment to
    # alternate with, and it costs one more GOP decode in the same process rather
    # than a second process. Short clips get the poster only.
    got = preview_members(src, dur) or {}
    blob = got.get(POSTER_NAME) or poster_bytes(src, dur)
    if not blob:
        # ffmpeg produced nothing (corrupt container, unreadable stream, or it timed out
        # under load). Leaving the row 'pending' made it permanent work: the Scan badge
        # and the re-encode queue counted a clip nothing could ever finish. 'error' is the
        # state the image path already uses -- it does not count as pending, and an
        # explicit "Re-encode thumbnails here" is what retries it.
        exec_many("UPDATE nodes SET thumb_state='error' WHERE id=?", (node_id,))
        log.warning("poster failed for node %s (%s, %s bytes, %ss): ffmpeg returned no image",
                    node_id, row["name"], row["size"], round(dur or 0))
        return None
    members = {ns + name: data for name, data in got.items()}
    members[ns + POSTER_NAME] = blob
    _store_in_pack(row["loc_id"], "dir", row["parent_id"], key, owner, hint,
                   members,
                   {ns + POSTER_NAME: ("nodes", node_id)},
                   {name: (row["size"], row["mtime"]) for name in members})
    # Image packs streamed their bytes to the page, but a video poster did not:
    # the tile sat on its 🎬 placeholder until the folder was listed again.
    events.publish_thumb(node_id, blob, int(float(row["mtime"] or 0) * 1000),
                         parent=row["parent_id"])
    return path


def queue_frames(node_id, on_done=None, force=False):
    """Build this video's filmstrip. Explicit only (context menu) -- see
    existing_frames() for why hover never triggers it.

    `force` belongs in the _once key, or a plain build already in flight would
    swallow the rebuild request and the menu item would appear to do nothing."""
    def run():
        try:
            extract_frames(node_id, force=force)
        finally:
            if on_done:
                on_done(node_id)
    _once(("frames", node_id, bool(force)), run, pool=_vpool)


def _poster_job(node_id, force=False):
    """Never let one bad clip leave a row pending forever, and never lose the reason."""
    try:
        return ensure_video_poster(node_id, force=force)
    except Exception as e:
        log.warning("poster job for node %s raised: %s", node_id, e)
        exec_many("UPDATE nodes SET thumb_state='error' WHERE id=?", (node_id,))
        return None


def queue_video_poster(node_id, force=False):
    # the key carries `force`, or a plain poster job already in flight would
    # swallow the re-encode request ("Re-encode" then does nothing, quietly)
    _once(("poster", node_id, bool(force)),
          lambda: _poster_job(node_id, force), pool=_vpool)


def ensure_video_pack(node_id, force=False):
    """Poster + FRAME_COUNT frames, stored as members of the containing folder's
    pack. Idempotent: returns the frame names as soon as that pack holds them.

    force=True skips that short-circuit and re-extracts (the poster is re-stored
    too, which bumps its generation, so the browser cache key changes)."""
    row = node_by_id(node_id)
    if not row or row["kind"] != "video":
        return []
    ns = video_ns(node_id)
    want = [_frame_name(i) for i in range(FRAME_COUNT)]
    members_want = [ns + POSTER_NAME] + [ns + n for n in want]
    owner, hint, key = video_folder_spot(row)
    path = packs.resolve(row["loc_id"], key, owner, hint)

    def ready():
        """Frame names if the folder pack already carries this video's members."""
        try:
            _b, index, _d = packs.read(path)
        except (packs.PackError, OSError):
            return None
        if not all(m in index for m in members_want):
            return None
        _off, _ln, _crc, size, mtime = index[ns + POSTER_NAME]
        if size != (row["size"] or 0):
            return None
        if mtime and row["mtime"] and abs(mtime - row["mtime"]) > 1:
            return None
        gen = packs.member_gen(_d, ns + POSTER_NAME)
        if gen < THUMB_GEN:          # poster made by older code: re-encode it
            return None
        _link_poster(row, path, index, ns, gen)
        return want

    got = None if force else ready()
    if got:
        return got
    slot = node_id if not force else ("force", node_id)   # two jobs must not share a slot
    with _IFLOCK:
        if slot in _INFLIGHT:
            deadline = time.time() + 30          # another request owns it: wait
            while time.time() < deadline:
                time.sleep(0.5)
                if not force:
                    got = ready()
                    if got:
                        return got
            return []
        _INFLIGHT.add(slot)
    try:
        src = node_abspath(row)
        dur = probe_duration(src)
        remember_duration(node_id, dur)
        raw = video_members(src, dur)
        if not raw:                            # single pass failed: per-frame path
            pb = poster_bytes(src, dur)
            if pb:
                raw[POSTER_NAME] = pb
            if dur > 0:
                for i in range(FRAME_COUNT):
                    blob = frame_bytes(src, dur * (i + 0.5) / FRAME_COUNT)
                    if blob:
                        raw[want[i]] = blob
        if not raw:
            return []
        members = {ns + k: v for k, v in raw.items()}
        _store_in_pack(row["loc_id"], "dir", row["parent_id"], key, owner, hint,
                       members, {ns + POSTER_NAME: ("nodes", node_id)},
                       {m: (row["size"], row["mtime"]) for m in members})
        # Building a filmstrip is the owner asking for the strip: clear the "two images
        # only" flag a re-encode may have set, or the strip would stay hidden.
        exec_many("UPDATE nodes SET preview_only=0 WHERE id=?", (node_id,))
        return [n for n in want if ns + n in members]
    finally:
        with _IFLOCK:
            _INFLIGHT.discard(slot)


def extract_frames(node_id, force=False):
    """Kept for callers: returns the video's frame member names.

    `force` re-extracts even when a full strip is already in the pack -- the
    per-video menu item needs it, or a bad strip can never be replaced."""
    return ensure_video_pack(node_id, force=force)

