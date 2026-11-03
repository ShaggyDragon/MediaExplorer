"""Pack store maintenance.

    python -m app.packtool stats          # what's on disk vs what the index says
    python -m app.packtool purge          # delete every pack, re-pend thumbs
    python -m app.packtool gc             # drop packs whose media is gone
    python -m app.packtool verify [N]     # CRC-check N packs (default: all)

`purge` is the sanitise story: it only ever deletes `.mxthumbs` pack files and
the pack columns. Media, the index and the locations table are untouched.
"""
import os
import sys

from .config import PACK_EXT, PACK_LEGACY_DIRNAME, DATA_PATH
from .db import db, q, one, exec_many, bump_store_epoch
from . import packs


def _human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


def cmd_stats(_args):
    db()
    print(f"format: {packs.PACK_MAGIC.strip().decode()} container, toc v{packs.TOC_VERSION}, "
          f"ext {PACK_EXT}")
    rows = q("SELECT kind, COUNT(*) c, SUM(count) items, SUM(bytes) b FROM thumb_pack GROUP BY kind")
    total_b = sum(r["b"] or 0 for r in rows)
    print(f"packs: {sum(r['c'] for r in rows):,}   on disk: {_human(total_b)}")
    for r in rows:
        print(f"  {r['kind']:6s} {r['c']:>7,} packs  {r['items'] or 0:>9,} thumbs  {_human(r['b'] or 0)}")
    ready = one("SELECT COUNT(*) c FROM nodes WHERE kind IN ('image','video') AND thumb_state='ready'")["c"]
    pending = one("SELECT COUNT(*) c FROM nodes WHERE kind IN ('image','video') AND thumb_state!='ready'")["c"]
    eready = one("SELECT COUNT(*) c FROM zip_entries WHERE kind='image' AND thumb_state='ready'")["c"]
    print(f"index: {ready:,} media thumbs ready, {pending:,} pending, {eready:,} zip-entry thumbs")
    st = packs.cache_stats()
    print(f"ram cache: {st['packs']} packs, {_human(st['bytes'])} of {_human(st['limit'])}")


def _stray_packs(known):
    """Packs on disk that the index does not know about, found without walking
    the media tree: a stray can only sit in a directory we still manage (a
    renamed zip/video/folder leaves its old pack beside the new one)."""
    for d in {os.path.dirname(p) for p in known if p}:
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if e.name.endswith(PACK_EXT) and e.path not in known:
                    yield e.path


def _legacy_dirs():
    """Pre-0.1.18 packs lived in `<folder>/.mxthumbs/`. Find those dirs from the
    index (one stat per indexed folder — never a walk of the media tree)."""
    seen = set()
    for r in q("SELECT n.rel AS rel, l.root AS root FROM nodes n"
               " JOIN locations l ON l.id=n.loc_id WHERE n.kind='dir'"):
        d = os.path.join(r["root"], r["rel"]) if r["rel"] else r["root"]
        p = os.path.join(d, PACK_LEGACY_DIRNAME)
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            yield p
    for r in q("SELECT root FROM locations"):
        p = os.path.join(r["root"], PACK_LEGACY_DIRNAME)
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            yield p


def _drop_legacy():
    """Remove the old per-folder `.mxthumbs/` dirs (files + dir)."""
    n = freed = 0
    for d in list(_legacy_dirs()):
        try:
            for e in os.scandir(d):
                try:
                    freed += e.stat().st_size
                    os.unlink(e.path)
                    n += 1
                except OSError:
                    pass
            os.rmdir(d)
        except OSError:
            pass
    return n, freed


def _walk_packs():
    """Every pack file under the media roots, found by walking the tree.

    The index cannot be trusted here: purge/gc exist precisely for the case
    where rows are missing or stale, and a pack in a directory that has no other
    known pack (the location root, say) is otherwise invisible -- it survived a
    purge. A maintenance walk is worth the seconds."""
    for loc in q("SELECT root FROM locations"):
        root = loc["root"]
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(".mxthumbs")]
            for f in filenames:
                if f.endswith(PACK_EXT):
                    yield os.path.join(dirpath, f)


def _all_pack_paths():
    known = {r["path"] for r in q("SELECT path FROM thumb_pack") if r["path"]}
    return list(known | set(_stray_packs(known))
                | {str(p) for p in packs.iter_packs()} | set(_walk_packs()))


def purge_all():
    """Delete every cached thumb and re-pend the index. Shared by the CLI and
    POST /api/admin/purge, so both do exactly the same thing.

    Removes: every pack file it can find (adjacent, central, legacy dirs), the
    pre-pack loose `thumbs/` tree on the data disk, and the pack columns. Never
    touches media. The index keeps its rows, so the next scan/open rebuilds."""
    db()
    bump_store_epoch()          # nothing rebuilt after this may reuse a cached URL
    paths = set(_all_pack_paths())
    n = freed = 0
    for p in paths:
        try:
            freed += os.path.getsize(p)
            os.unlink(p)
            n += 1
        except OSError:
            pass
    packs.cache_clear()
    # an index row whose file is gone is worse than no row: it makes a later
    # lookup believe the thumbs exist (and, for videos, adopt a neighbour's)
    stale = 0
    for r in q("SELECT id, path FROM thumb_pack"):
        if r["path"] and not os.path.exists(r["path"]):
            exec_many("DELETE FROM thumb_pack WHERE id=?", (r["id"],))
            stale += 1
    n_legacy, freed_legacy = _drop_legacy()
    n += n_legacy
    freed += freed_legacy
    # pre-pack loose thumbs (a "new data set" must not fall back to these)
    loose = 0
    for root, _d, files in os.walk(DATA_PATH / "thumbs"):
        for f in files:
            try:
                freed += os.path.getsize(os.path.join(root, f))
                os.unlink(os.path.join(root, f))
                loose += 1
            except OSError:
                pass
    exec_many("UPDATE nodes SET pack_id=NULL, pack_off=NULL, pack_len=NULL, thumb=NULL,"
              " thumb_state=CASE WHEN kind IN ('image','video') THEN 'pending' ELSE 'none' END")
    exec_many("UPDATE zip_entries SET pack_id=NULL, pack_off=NULL, pack_len=NULL,"
              " thumb_state=CASE WHEN kind='image' THEN 'pending' ELSE thumb_state END")
    exec_many("DELETE FROM thumb_pack")
    return {"packs": n, "loose": loose, "freed": freed}


def purge_db():
    """Drop the whole index so the library re-indexes from the filesystem."""
    from .config import DB_PATH
    from .db import close as db_close
    from pathlib import Path
    db_close()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        try:
            p.unlink()
        except OSError:
            pass


def cmd_purge(_args):
    res = purge_all()
    print(f"purged {res['packs']:,} packs + {res['loose']:,} loose thumbs, "
          f"{_human(res['freed'])}; thumbs re-pended")


def cmd_gc(_args):
    db()
    removed = freed = 0
    for r in q("SELECT p.*, n.rel AS owner_rel, n.loc_id AS owner_loc,"
               " n.kind AS owner_kind, l.root AS loc_root FROM thumb_pack p"
               " LEFT JOIN nodes n ON n.id = p.owner_id"
               " LEFT JOIN locations l ON l.id = p.loc_id"):
        orphan = False
        if r["kind"] == "dir" and r["owner_id"] is None:
            orphan = False                      # location root: always valid
        elif r["owner_rel"] is None:
            orphan = True                       # owner node deleted by a rescan
        else:
            owner_abs = os.path.join(r["loc_root"], r["owner_rel"]) if r["owner_rel"] else r["loc_root"]
            orphan = not os.path.exists(owner_abs)   # folder/zip/video gone from disk
        if not orphan:
            continue
        for tbl in ("nodes", "zip_entries"):        # never leave a dangling pointer
            exec_many(f"UPDATE {tbl} SET pack_id=NULL, pack_off=NULL, pack_len=NULL,"
                      f" thumb_state='pending' WHERE pack_id=?", (r["id"],))
        try:
            freed += os.path.getsize(r["path"])
            os.unlink(r["path"])
            removed += 1
        except OSError:
            pass
        exec_many("DELETE FROM thumb_pack WHERE id=?", (r["id"],))
    # rows whose file has vanished -- an interrupted write, or a pack re-registered
    # under a new name before rows were keyed by (loc, key). A row that cannot
    # serve a byte is worse than no row: re-pend whatever pointed at it.
    missing = repended = 0
    for r in q("SELECT id, path FROM thumb_pack"):
        if os.path.exists(r["path"]):
            continue
        for tbl in ("nodes", "zip_entries"):
            repended += one(f"SELECT COUNT(*) c FROM {tbl} WHERE pack_id=?",
                            (r["id"],))["c"]
            exec_many(f"UPDATE {tbl} SET pack_id=NULL, pack_off=NULL, pack_len=NULL,"
                      f" thumb_state='pending' WHERE pack_id=?", (r["id"],))
        exec_many("DELETE FROM thumb_pack WHERE id=?", (r["id"],))
        missing += 1
    if missing:
        print(f"gc: dropped {missing:,} pack row(s) whose file was missing, "
              f"{repended:,} thumb(s) re-pended")
    # packs on disk with no index row at all (interrupted writes, old names left
    # by a rename, and the legacy central-hash layout)
    known = {r["path"] for r in q("SELECT path FROM thumb_pack") if r["path"]}
    strays = set(_stray_packs(known)) | {str(p) for p in packs.iter_packs()}
    for p in strays - known:
        try:
            freed += os.path.getsize(p)
            os.unlink(p)
            removed += 1
        except OSError:
            pass
    n_legacy, freed_legacy = _drop_legacy()
    removed += n_legacy
    freed += freed_legacy
    print(f"gc: removed {removed:,} orphan packs, {_human(freed)}")


def cmd_verify(args):
    db()
    limit = int(args[0]) if args else 0
    rows = q("SELECT path FROM thumb_pack")
    bad_packs = 0
    checked = 0
    for r in rows[:limit] if limit else rows:
        if not os.path.exists(r["path"]):
            bad_packs += 1
            print(f"  MISSING {r['path']}: registered but not on disk (run gc)")
            continue
        try:
            ok, bad = packs.verify(r["path"])
            checked += ok + len(bad)
            if bad:
                bad_packs += 1
                print(f"  BAD {r['path']}: {len(bad)} corrupt member(s), e.g. {bad[:3]}")
        except (packs.PackError, OSError) as exc:
            bad_packs += 1
            print(f"  UNREADABLE {r['path']}: {exc}")
    print(f"verify: {checked:,} thumbs checked, {bad_packs} bad pack(s)")


def main(argv):
    cmds = {"stats": cmd_stats, "purge": cmd_purge, "gc": cmd_gc, "verify": cmd_verify}
    if len(argv) < 2 or argv[1] not in cmds:
        print(__doc__)
        return 2
    cmds[argv[1]](argv[2:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
