import os
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
VERSION = (APP_DIR / "VERSION").read_text().strip()

APP_NAME = os.environ.get("APP_NAME", "MediaExplorer")
DATA_PATH = Path(os.environ.get("DATA_PATH", "/data"))

# Unraid convention: TZ env var controls local time (file mtimes display)
_TZ = os.environ.get("TZ")
if _TZ:
    os.environ["TZ"] = _TZ
    time.tzset()
THUMB_PATH = Path(os.environ.get("THUMB_PATH", str(DATA_PATH / "thumbs")))

# Pack store: ONE hidden file per browsable unit, in that unit's PARENT folder,
# named after the unit (see app/packs.py):
#   /media/Photos/.Holiday.mxthumbs          <- thumbs for the images in Holiday/
#   /media/Photos/.Trip.zip.mxthumbs         <- entry thumbs for Trip.zip
#   /media/Photos/.img1234.mkv.mxthumbs      <- poster + filmstrip for a video
#   /media/Photos/._root.mxthumbs            <- the location root's own loose images
# Dot-prefixed so ls/Finder hide it (and the DOS hidden bit is set for Windows).
# No new directories are ever created inside a media tree.
#   adjacent - beside the media (default); a RO mount falls back to central
#   central  - under MX_PACK_PATH, keyed by a path hash
#   hybrid   - the pre-0.1.17 name for adjacent
PACK_MODE = os.environ.get("MX_PACK_MODE", "adjacent").strip().lower()
PACK_EXT = ".mxthumbs"
PACK_LEGACY_DIRNAME = ".mxthumbs"   # pre-0.1.18 per-folder dir; packtool cleans it
PACK_PATH = Path(os.environ.get("MX_PACK_PATH", str(DATA_PATH / "packs")))
PACK_MAGIC = b"MXPACK1\n"
PACK_END_MAGIC = b"MXPEND1\n"
PACK_CACHE_BYTES = int(float(os.environ.get("MX_PACK_CACHE_MB", "96")) * 1024 * 1024)
# never write a pack onto a disk with less than this much free space (GB);
# a thumbnail cache must not be the thing that fills a media disk
PACK_MIN_FREE_GB = float(os.environ.get("MX_PACK_MIN_FREE_GB", "5"))

DB_PATH = DATA_PATH / "mediaexplorer.db"
PORT = int(os.environ.get("PORT", "8000"))

def effective_cpus():
    """Public name: every pool sizes itself from the same number."""
    return _effective_cpus()


def _effective_cpus():
    """Cores this container may actually use.

    os.cpu_count() reports the host, so a container pinned with --cpus=4 would
    still fan out to host width. Honour the cgroup quota (v2 cpu.max, v1
    cfs_quota) so the thread count follows the container's own CPU limit."""
    try:                                    # respect a CPU affinity mask too
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 4
    try:                                    # cgroup v2
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
        if quota != "max":
            n = min(n, max(1, -(-int(quota) // int(period))))
    except (OSError, ValueError):
        try:                                # cgroup v1
            q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
            p = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
            if q > 0:
                n = min(n, max(1, -(-q // p)))
        except (OSError, ValueError):
            pass
    return n


# Encoding one folder is embarrassingly parallel: Pillow's decode/resize/encode
# release the GIL, so threads give near-linear speedup here. Browsing one folder
# used to occupy a single core because a pack is assembled by one job.
# Testing convenience: a new build starts from a clean thumb store. Wipes only
# when the *version* changes, so restarting mid-test keeps what you built.
WIPE_ON_BUILD = os.environ.get("MX_WIPE_ON_BUILD", "1").strip().lower() not in ("0", "false", "no", "off")

def cpu_budget():
    """Split the cores we are allowed between the three pools that do real work.

    They are independent, so their maxima used to add up: on a 25-core cpuset the
    process ran 24 encode threads + 8 pack threads + 3 ffmpeg + 2 interactive = 37
    runnable threads. Oversubscribed like that a decode-heavy build is slower than
    the same build on fewer threads, and the box still looks idle because the extra
    threads are blocked, not working. One budget, spent once:
      thumbs  = whatever is left after the pools that drive ffmpeg/tiles
      packs   = cpus/4, 2..8   (zip/folder packs + mosaics)
      ffmpeg  = cpus/8, 1..4   (posters, filmstrips: each is already multithreaded)
    """
    n = _effective_cpus()
    packs = max(2, min(8, n // 4))
    ffmpeg = max(1, min(4, n // 8))
    thumbs = max(1, n - packs - ffmpeg - 1)
    return n, thumbs, packs, ffmpeg


_BUDGET = cpu_budget()
PACK_WORKERS = _BUDGET[2]
FFMPEG_JOBS = _BUDGET[3]
THUMB_WORKERS = max(1, int(os.environ.get("MX_THUMB_WORKERS", "0") or 0)
                    or min(24, _BUDGET[1]))
# Generation of the thumbnail MASTER format. Every encoded thumbnail is stamped
# with the value current at the time, and a scan/rebuild only re-encodes rows whose
# stamp is older -- so changing geometry costs one folder at a time, never a sweep.
#   1 = cover-cropped 300x225 (pre 0.1.94)
#   2 = letterboxed, but zip/folder tiles still cropped their quadrants
#   3 = letterboxed everywhere (0.1.96+)
#   4 = 0.1.110: video posters. Generation 3 was stamped by packs whose members were
#       written by the OLD crop filter (a pack rewrite stamps the appending code's
#       generation on the pack), so a cropped poster reads 'current' and is adopted
#       forever. Bumping invalidates every member honestly; nothing rebuilds until a
#       folder is scanned or rebuilt.
THUMB_GEN = 4

THUMB_MAX = (300, 225)
THUMB_QUALITY = 82
EAGER_BACKLOG = 20          # backlog <= this generates inline on navigate
FRAME_COUNT = 8
ANIM_MAX = int(os.environ.get("MX_ANIM_MAX", "500"))  # Scan pre-builds hover
                            # filmstrips up to this many videos in the scanned
                            # subtree; beyond that they're made on hover

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".mpg", ".mpeg", ".ts"}


def media_locations():
    """Media locations from two sources (union):
    1. MEDIA_1 .. MEDIA_20 env vars: 'Display Name|/container/path' (or just a path).
    2. Auto-detected fixed container dirs /media1 .. /media8 (Unraid template Path rows).
       Display name: MEDIA_N_NAME env var if set, else the directory's base name."""
    locs = []
    for i in range(1, 21):
        v = os.environ.get(f"MEDIA_{i}")
        if not v:
            continue
        if "|" in v:
            name, path = v.split("|", 1)
        else:
            path = v
            name = Path(v).name
        name, path = name.strip(), path.strip()
        if path:
            locs.append((name, path))
    auto = os.environ.get("MEDIA_DIRS", "/media1,/media2,/media3,/media4,"
                                    "/media5,/media6,/media7,/media8")
    for i, d in enumerate(p.strip() for p in auto.split(",")):
        if d and os.path.isdir(d):
            name = os.environ.get(f"MEDIA_{i+1}_NAME") or Path(d.rstrip("/")).name
            locs.append((name, d))
    return locs
