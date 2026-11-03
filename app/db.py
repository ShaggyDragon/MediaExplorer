import os
import sqlite3
import threading
import time
from .config import DB_PATH, DATA_PATH

# A query that takes longer than this is recorded in the log ring with its SQL. The
# point is to answer "is SQLite the thing that is slow?" with evidence instead of a
# hunch: with one shared connection behind one lock, the wait includes lock time, so a
# listing that queues behind a slow statement shows up here as a *slow* query even
# though the statement it waited on is a different one.
SLOW_MS = float(os.environ.get("MX_SLOW_MS", "50"))
_slow_hook = None


def set_slow_hook(fn):
    """Server-side sink for slow-query records (keeps this module importable alone)."""
    global _slow_hook
    _slow_hook = fn


def _slow(sql, t0, rows=None):
    hook = _slow_hook
    if hook is None:
        return
    ms = (time.perf_counter() - t0) * 1000.0
    if ms < SLOW_MS:
        return
    try:
        hook({"ev": "slow_q", "sql": " ".join(str(sql).split())[:200],
              "ms": round(ms, 1), "rows": rows, "ts": time.time()})
    except Exception:
        pass          # logging must never be the reason a query fails

_lock = threading.RLock()
_conn = None

# A thumb is only genuinely ready when the pack it points at still exists (or it
# has a legacy loose file). gc/purge delete pack rows; a dangling pack_id would
# otherwise be trusted forever -- the thumb 404s and is never rebuilt. Use this
# predicate anywhere "is there work to do?" is asked.
THUMB_WORK = ("thumb_state NOT IN ('ready','error') OR (pack_id IS NOT NULL AND pack_id NOT IN "
              "(SELECT id FROM thumb_pack))")


def sweep_orphans():
    """Delete node rows whose parent row is gone.

    A deleted or renamed folder can leave its children behind (their parent_id
    then points at nothing). Those rows are invisible in the UI but stay in
    thumb_state='pending' forever, so /api/config's pending count never reaches
    zero and the scan thinks there is work that can never be done."""
    with _lock:
        cur = db().execute(
            "DELETE FROM nodes WHERE parent_id IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM nodes p WHERE p.id = nodes.parent_id)")
        return cur.rowcount


def close():
    """Close the connection so the file can be removed (purge_db)."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                _conn.close()
            except sqlite3.Error:
                pass
            _conn = None


def db():
    global _conn
    if _conn is None:
        DATA_PATH.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        # Without this a write that overlaps another connection raises after Python's
        # default 5s and surfaces as HTTP 500; busy_timeout makes it wait instead.
        _conn.execute("PRAGMA busy_timeout=15000")
        _init()
    return _conn


def q(sql, args=()):
    t0 = time.perf_counter()      # timed from before the lock: queueing is the cost
    with _lock:
        rows = _conn.execute(sql, args).fetchall()
    _slow(sql, t0, len(rows))
    return rows


def one(sql, args=()):
    t0 = time.perf_counter()
    with _lock:
        row = _conn.execute(sql, args).fetchone()
    _slow(sql, t0, 1 if row is not None else 0)
    return row


def exec_many(sql, args=()):
    t0 = time.perf_counter()
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
    _slow(sql, t0, cur.rowcount)
    return cur.lastrowid


def _init():
    _conn.executescript("""
    CREATE TABLE IF NOT EXISTS locations(
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        root TEXT NOT NULL UNIQUE
    );
    CREATE TABLE IF NOT EXISTS nodes(
        id INTEGER PRIMARY KEY,
        loc_id INTEGER NOT NULL REFERENCES locations(id),
        parent_id INTEGER,
        name TEXT NOT NULL,
        rel TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('dir','zip','image','video')),
        size INTEGER DEFAULT 0,
        mtime REAL DEFAULT 0,
        thumb TEXT,
        thumb_state TEXT NOT NULL DEFAULT 'none',
        UNIQUE(loc_id, parent_id, name)
    );
    CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(loc_id, parent_id);
    CREATE TABLE IF NOT EXISTS folder_pref(
        loc_id INTEGER NOT NULL,
        rel TEXT NOT NULL,             -- path inside the location; '' is its root
        size INTEGER,                  -- tile size in px
        sort TEXT,                     -- 'name' | 'date'
        anim INTEGER,                  -- video frame preview on/off
        updated REAL DEFAULT 0,
        PRIMARY KEY (loc_id, rel)
    );
    -- Favourites menu (star in the top bar). Keyed by PATH, like folder_pref: node
    -- ids are not stable across a re-index, a path is. 'kind' is dir | zip | loc.
    CREATE TABLE IF NOT EXISTS favourite(
        id INTEGER PRIMARY KEY,
        loc_id INTEGER NOT NULL,
        rel TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'dir',
        added REAL DEFAULT 0,
        UNIQUE (loc_id, rel)
    );
    CREATE TABLE IF NOT EXISTS zip_entries(
        id INTEGER PRIMARY KEY,
        zip_id INTEGER NOT NULL REFERENCES nodes(id),
        name TEXT NOT NULL,
        size INTEGER DEFAULT 0,
        kind TEXT NOT NULL CHECK(kind IN ('image','video','other')),
        thumb TEXT,
        thumb_state TEXT NOT NULL DEFAULT 'none',
        zoffset INTEGER DEFAULT -1,
        method INTEGER DEFAULT 0,
        csize INTEGER DEFAULT 0,
        UNIQUE(zip_id, name)
    );
    """)
    for col, typ in (("zoffset", "INTEGER DEFAULT -1"),
                     ("method", "INTEGER DEFAULT 0"),
                     ("csize", "INTEGER DEFAULT 0")):
        try:
            _conn.execute(f"ALTER TABLE zip_entries ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # column already present
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_zipentry_zip ON zip_entries(zip_id)")
    _conn.execute("""
    CREATE TABLE IF NOT EXISTS app_meta(
      key TEXT PRIMARY KEY,
      val TEXT
    )
    """)
    _conn.execute("""
    CREATE TABLE IF NOT EXISTS thumb_pack(
        id INTEGER PRIMARY KEY,
        loc_id INTEGER NOT NULL,
        kind TEXT NOT NULL,            -- 'dir' | 'zip' | 'video'
        owner_id INTEGER,              -- nodes.id of the folder/zip/video (NULL = location root)
        key TEXT NOT NULL,             -- pack key, e.g. 'd:USA/2015'
        path TEXT NOT NULL UNIQUE,     -- absolute .mxpack path
        count INTEGER DEFAULT 0,
        bytes INTEGER DEFAULT 0,
        dead INTEGER DEFAULT 0,        -- bytes of superseded records (gc hint)
        built REAL DEFAULT 0
    )""")
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pack_owner ON thumb_pack(loc_id, kind, owner_id)")
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pack_key ON thumb_pack(loc_id, key)")
    # pack pointers: thumb bytes live in a .mxpack, addressed by (pack, off, len)
    for tbl in ("nodes", "zip_entries"):
        for col, typ in (("pack_id", "INTEGER"), ("pack_off", "INTEGER"),
                         ("pack_len", "INTEGER"), ("thumb_gen", "INTEGER DEFAULT 0")):
            try:
                _conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # column already present
    try:
        _conn.execute("ALTER TABLE nodes ADD COLUMN zindexed INTEGER")  # type: ignore[union-attr]
    except sqlite3.OperationalError:
        pass  # column already present
    # A video normally carries poster + 8 filmstrip frames. "Re-encode thumbnails"
    # puts a clip back to the default two images (poster + one later still) without
    # touching the pack: the extra frames stay as dead weight until gc rewrites the
    # pack, and building a filmstrip again clears the flag.
    try:
        _conn.execute("ALTER TABLE nodes ADD COLUMN preview_only INTEGER DEFAULT 0")  # type: ignore[union-attr]
    except sqlite3.OperationalError:
        pass  # column already present
    # A video's running time in seconds, for the "0:39" label on its tile. Measured by
    # the same ffprobe the preview build already runs (it needs the length to place
    # the 3%/6% stills), so keeping it costs nothing; NULL = not measured yet.
    try:
        _conn.execute("ALTER TABLE nodes ADD COLUMN dur REAL")  # type: ignore[union-attr]
    except sqlite3.OperationalError:
        pass  # column already present
    # v2 thumbnail format = exact 300x225 center-cropped masters.
    # Bump user_version and invalidate old aspect-preserving thumbs once.
    ver = _conn.execute("PRAGMA user_version").fetchone()[0]
    if ver < 2:
        _conn.execute("UPDATE nodes SET thumb=NULL, thumb_state='none'")
        _conn.execute("UPDATE zip_entries SET thumb=NULL, thumb_state='none'")
        _conn.execute("PRAGMA user_version=2")
    if ver < 5:
        # v5: per-member generations. Thumbnails stamped by the pack-level value
        # cannot be trusted (a pack is rewritten by newer code while its older
        # members stay put), so every ready row is marked stale once. Nothing is
        # re-encoded here: pictures keep displaying and are upgraded by scanning
        # or rebuilding the folder that holds them.
        _conn.execute("UPDATE nodes SET thumb_gen=1 WHERE thumb_state='ready'")
        _conn.execute("UPDATE zip_entries SET thumb_gen=1 WHERE thumb_state='ready'")
        _conn.execute("PRAGMA user_version=5")
    if ver < 4:
        # v4: every thumbnail records the generation that encoded it, so a change
        # to how masters are made is rolled out per folder (Scan / rebuild) instead
        # of invalidating the library. Existing rows are stale-but-usable: they keep
        # displaying until the folder they belong to is scanned or rebuilt.
        _conn.execute("UPDATE nodes SET thumb_gen=1 WHERE thumb_state='ready'")
        _conn.execute("UPDATE zip_entries SET thumb_gen=1 WHERE thumb_state='ready'")
        _conn.execute("PRAGMA user_version=4")
    if ver < 3:
        # v3: video nodes now get a single poster-frame thumbnail -> re-pend videos
        _conn.execute("UPDATE nodes SET thumb=NULL, thumb_state='pending' WHERE kind='video'")
        _conn.execute("PRAGMA user_version=3")
    _conn.commit()


def node_by_id(node_id):
    return one("SELECT * FROM nodes WHERE id=?", (node_id,))


def child_nodes(loc_id, parent_id, kinds=None):
    if kinds:
        ph = ",".join("?" * len(kinds))
        return q(
            f"SELECT * FROM nodes WHERE loc_id=? AND parent_id IS ? AND kind IN ({ph})",
            (loc_id, parent_id, *kinds),
        )
    return q("SELECT * FROM nodes WHERE loc_id=? AND parent_id IS ?", (loc_id, parent_id))


def pending_media_count():
    # zip entries deliberately excluded: at 100k+ zips that COUNT is a full
    # table scan AND entry thumbs are generated on open, not by Scan.
    r = one(f"SELECT COUNT(*) c FROM nodes WHERE kind IN ('image','video') AND ({THUMB_WORK})")
    return r["c"] if r else 0


# ---------------------------------------------------------------- tiny kv store
# Survives restarts in the index DB; used for "which build did we last wipe for".

def meta_get(key, default=None):
    r = one("SELECT val FROM app_meta WHERE key=?", (key,))
    return r["val"] if r else default


def meta_set(key, val):
    exec_many("INSERT INTO app_meta(key, val) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET val=excluded.val", (key, str(val)))


# Store generation: part of every thumbnail URL. Thumbnails are cached by the browser
# keyed on URL, and the URL's other parts (mtime, gen, pack offset) can REPEAT after a
# purge -- pack ids are a plain INTEGER PRIMARY KEY and offsets restart at 0 -- so a
# rebuilt thumb could land on a URL the browser already holds different bytes for.
# Every purge bumps this, so nothing written before a purge can ever share a URL with
# anything written after it. Cached here: it is read once per thumbnail URL built.
_STORE_EPOCH = None


def store_epoch():
    global _STORE_EPOCH
    if _STORE_EPOCH is None:
        _STORE_EPOCH = meta_get("store_epoch") or "0"
    return _STORE_EPOCH


def bump_store_epoch():
    global _STORE_EPOCH
    _STORE_EPOCH = format(int(time.time() * 1000), "x")
    meta_set("store_epoch", _STORE_EPOCH)
    return _STORE_EPOCH


# ------------------------------------------------------- per-folder view pins
# A pinned folder keeps its own tile size, sort order and animation state, keyed by
# PATH inside the location rather than node id: a deep rescan can delete and re-create
# node rows (ids get reused), which would silently move or lose a pin. rel survives
# that, and a row is ~50 bytes, so the whole table is noise next to the thumb index.
def prefs_list(loc_id=None):
    if loc_id is None:
        return q("SELECT * FROM folder_pref ORDER BY loc_id, rel")
    return q("SELECT * FROM folder_pref WHERE loc_id=?", (loc_id,))


def pref_get(loc_id, rel):
    return one("SELECT * FROM folder_pref WHERE loc_id=? AND rel=?", (loc_id, rel or ""))


def pref_set(loc_id, rel, size=None, sort=None, anim=None):
    """Partial update: a column passed as None keeps its stored value, so a caller
    that only knows the size cannot wipe the sort."""
    exec_many("INSERT INTO folder_pref(loc_id, rel, size, sort, anim, updated)"
              " VALUES(?,?,?,?,?,?)"
              " ON CONFLICT(loc_id, rel) DO UPDATE SET"
              "  size=COALESCE(excluded.size, size),"
              "  sort=COALESCE(excluded.sort, sort),"
              "  anim=COALESCE(excluded.anim, anim),"
              "  updated=excluded.updated",
              (loc_id, rel or "", size, sort, anim, time.time()))


def pref_drop(loc_id, rel):
    exec_many("DELETE FROM folder_pref WHERE loc_id=? AND rel=?", (loc_id, rel or ""))
