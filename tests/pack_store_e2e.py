"""Acceptance test for the v0.1.17 pack store: build, serve, rename-adopt, gc.

Run against the dev server on :8765 with MX_PACK_MODE=adjacent.
"""
import json, os, shutil, subprocess, sys, time, urllib.request, urllib.error

B = "http://127.0.0.1:8765"
TL = "/opt/data/cache/scratch/testlib"
PY = "/opt/data/cache/scratch/mxenv/bin/python"
ENV = dict(os.environ, DATA_PATH="/opt/data/cache/scratch/mxdata", MX_PACK_MODE="adjacent")

fails = []

def get(p):
    return json.load(urllib.request.urlopen(B + p, timeout=180))

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (": " + detail if detail else ""))
    if not cond:
        fails.append(name)

def scan():
    get_post("/api/scan")
    for _ in range(60):
        s = get("/api/scan/status")
        if not s["running"]:
            return s
        time.sleep(3)
    return s

def get_post(p):
    req = urllib.request.Request(B + p, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=60))


def settle(url, tries=25):
    """Listings now answer immediately and build thumbs in worker threads, so
    wait for pending to clear before asserting on thumbs."""
    d = get(url)
    for _ in range(tries):
        if not any(i.get("pending") for i in d["items"]):
            break
        time.sleep(1)
        d = get(url)
    return d

def packs():
    out = {}
    for root, _d, files in os.walk(TL):
        for f in files:
            if f.endswith(".mxthumbs"):
                full = os.path.join(root, f)
                st = os.stat(full)
                out[full] = (st.st_ino, int(st.st_mtime))
    return out

def thumb_ok(url):
    r = urllib.request.urlopen(B + url, timeout=60)
    body = r.read()
    return r.status == 200 and r.headers["content-type"] == "image/jpeg" and len(body) > 500

def tree(loc=1, parent=None):
    p = f"/api/tree?loc={loc}" + (f"&parent={parent}" if parent else "")
    return get(p)

def children(loc=1, parent=None):
    p = f"/api/children?loc={loc}" + (f"&parent={parent}" if parent else "")
    return get(p)["items"]

def find(name, loc=1, parent=None):
    for i in tree(loc, parent):
        if i["name"] == name:
            return i
    return None

# ---------------------------------------------------------------- 1. build
print("== scan ==")
print(scan())
pk = packs()
check("packs created beside media", len(pk) >= 5, f"{len(pk)} packs")
check("no central packs", not os.path.isdir("/opt/data/cache/scratch/mxdata/packs"),
      "no packs/ dir under /data")

# open zips + videos so their packs exist
arch = find("Archive2020")
zips = [i for i in children(parent=arch["id"]) if i["kind"] == "zip"]
for z in zips:
    e = settle(f"/api/zip/{z['id']}/entries")
    ready = sum(1 for i in e["items"] if i.get("thumb"))
    check(f"zip {z['name']} thumbs ready", ready == 6 or ready >= len(e["items"]) - 1,
          f"{ready}/{len(e['items'])}")
# a folder and a zip with the same stem keep separate packs (extension kept in
# the pack name only where it is needed)
arch_dir = os.path.join(TL, "photos", "Archive2020")
settle(f"/api/children?loc=1&parent={arch['id']}")
for _ in range(20):
    if os.path.exists(os.path.join(arch_dir, ".Small.zip.mxthumbs")):
        break
    time.sleep(1)
check("stem clash keeps both packs apart",
      os.path.exists(os.path.join(arch_dir, ".Small.mxthumbs"))
      and os.path.exists(os.path.join(arch_dir, ".Small.zip.mxthumbs")),
      "Small/ and Small.zip both have their own pack")

# a video
vid = find("Videos")
vids = [i for i in children(parent=vid["id"]) if i["kind"] == "video"]
if vids:
    fr = get(f"/api/frames/{vids[0]['id']}")
    check(f"filmstrip {vids[0]['name']}", len(fr["frames"]) == 8, f"{len(fr['frames'])} frames")
    check("filmstrip frame serves", thumb_ok(fr["frames"][0]))

before = packs()
print(f"   baseline: {len(before)} packs")

# ---------------------------------------------------------------- 2. renames
print("== renames ==")
moves = [("photos/Portraits", "photos/DuoPortraits"),
         ("photos/Archive2020/Trip.zip", "photos/Archive2020/Trip2015.zip")]
# pick a video to rename too
if vids:
    nm = vids[0]["name"]
    moves.append((f"photos/Videos/{nm}", f"photos/Videos/renamed-{nm}"))
for src, dst in moves:
    os.rename(os.path.join(TL, src), os.path.join(TL, dst))
print("   moved:", [os.path.basename(d) for _s, d in moves])

st = scan()
print("  ", st)

# open the renamed zip + video again: adoption should reuse the old packs
arch = find("Archive2020")
z = [i for i in children(parent=arch["id"]) if i["kind"] == "zip" and "Trip2015" in i["name"]]
if z:
    e = settle(f"/api/zip/{z[0]['id']}/entries")
    ready = sum(1 for i in e["items"] if i.get("thumb"))
    check("renamed zip entries adopted", ready >= len(e["items"]) - 1, f"{ready}/{len(e['items'])}")
vid = find("Videos")
if vids:
    nv = [i for i in children(parent=vid["id"]) if i["kind"] == "video" and "renamed-" in i["name"]]
    if nv:
        fr = get(f"/api/frames/{nv[0]['id']}")
        check("renamed video filmstrip adopted", len(fr["frames"]) == 8, f"{len(fr['frames'])}")

after = packs()
# the folder pack is adopted by content and renamed to the new folder's name —
# same inode AND same mtime, i.e. it was moved, not rewritten
old_port = [(k, v) for k, v in before.items() if k.endswith("/photos/.Portraits.mxthumbs")]
new_port = [(k, v) for k, v in after.items() if k.endswith("/photos/.DuoPortraits.mxthumbs")]
check("folder rename: pack adopted and renamed, NOT rewritten",
      bool(old_port and new_port) and old_port[0][1] == new_port[0][1]
      and not [k for k in after if k.endswith("/photos/.Portraits.mxthumbs")],
      f"{old_port[0][1] if old_port else None} -> {new_port[0][1] if new_port else None}")

trip_old = [k for k in after if k.endswith("Archive2020/.Trip.mxthumbs")]
trip_new = [k for k in after if k.endswith("Archive2020/.Trip2015.mxthumbs")]
check("zip rename: pack renamed to new name", bool(trip_new) and not trip_old,
      f"old={trip_old} new={trip_new}")

# thumb still serves from the adopted pack
kids = children(parent=find("DuoPortraits")["id"])
img = next((i for i in kids if i.get("thumb")), None)
check("adopted pack serves a thumb", bool(img) and thumb_ok(img["thumb"]))

# ------------------------------------------------- 2b. opportunistic sweep
# entering a folder drops packs whose unit is no longer beside it; live packs stay
shutil.rmtree(os.path.join(TL, "photos", "DuoPortraits"))
children()                       # parent=None -> the location root
check("folder entry swept the removed folder's pack",
      not os.path.exists(os.path.join(TL, "photos", ".DuoPortraits.mxthumbs")),
      "DuoPortraits pack gone")
check("live packs survive the sweep",
      os.path.exists(os.path.join(TL, "photos", ".Vacation2019.mxthumbs")))

# ---------------------------------------------------------------- 3. gc
print("== gc ==")
os.rmdir  # noqa: B018  (keep linters quiet about unused import style)
gone = os.path.join(TL, "work", "Scans", "Invoices")
if os.path.isdir(gone):
    subprocess.run(["rm", "-rf", gone], check=True)
scan()
out = subprocess.run([PY, "-m", "app.packtool", "gc"], cwd="/opt/data/mediaexplorer",
                     env=ENV, capture_output=True, text=True)
print("   ", out.stdout.strip() or out.stderr.strip())
check("gc removed the deleted folder's pack",
      not os.path.exists(os.path.join(TL, "work", "Scans", ".Invoices.mxthumbs")),
      "no Invoices pack")

# a pack left behind by a rename is invisible to the index: plant one and prove gc
# finds it through its neighbours (no media-tree walk)
planted = os.path.join(TL, "photos", "Archive2020", ".Ghost.mxthumbs")
shutil.copy(os.path.join(TL, "photos", "Archive2020", ".Trip2015.mxthumbs"),
            planted)
out = subprocess.run([PY, "-m", "app.packtool", "gc"], cwd="/opt/data/mediaexplorer",
                     env=ENV, capture_output=True, text=True)
print("   ", out.stdout.strip() or out.stderr.strip())
check("gc removed a stranded pack", not os.path.exists(planted), "Ghost pack gone")
check("gc left the live packs alone",
      os.path.exists(os.path.join(TL, "photos", "Archive2020", ".Trip2015.mxthumbs")))

# purge is the sanitise path: every pack gone, index re-pended, media untouched
media_before = sum(len([f for f in fs if not f.endswith(".mxthumbs")]) for _r, _d, fs in os.walk(TL))
out = subprocess.run([PY, "-m", "app.packtool", "purge"], cwd="/opt/data/mediaexplorer",
                     env=ENV, capture_output=True, text=True)
print("   ", out.stdout.strip() or out.stderr.strip())
left = [os.path.join(r, f) for r, _d, fs in os.walk(TL) for f in fs if f.endswith(".mxthumbs")]
check("purge removed every pack", not left, f"{len(left)} left")
media_after = sum(len([f for f in fs if not f.endswith(".mxthumbs")]) for _r, _d, fs in os.walk(TL))
check("purge left the media alone", media_after == media_before,
      f"{media_before} -> {media_after} files")

# ---------------------------------------------- 6. pack format versioning
print("== format version ==")
import json as _json
import struct as _struct
os.environ.setdefault("DATA_PATH", "/opt/data/cache/scratch/mxdata")
os.environ.setdefault("MX_PACK_MODE", "adjacent")
sys.path.insert(0, "/opt/data/mediaexplorer")
from app import packs as _packs

def _mk_pack(toc_ver, items=None):
    toc = _json.dumps({"v": toc_ver, "loc": 1, "key": "d:t", "items": items or []},
                      separators=(",", ":")).encode()
    return _packs.PACK_MAGIC + toc + _struct.pack("<Q", len(toc)) + _packs.PACK_END_MAGIC

doc, _idx = _packs.parse(_mk_pack(_packs.TOC_VERSION))
check("own toc version parses", doc["v"] == _packs.TOC_VERSION, f"v{doc['v']}")
try:
    _packs.parse(_mk_pack(_packs.TOC_VERSION + 1))
    check("newer toc version is refused", False)
except _packs.PackError as exc:
    check("newer toc version is refused", "newer" in str(exc), str(exc))
try:
    _packs.parse(_mk_pack(0))
    check("toc version 0 is refused", False)
except _packs.PackError as exc:
    check("toc version 0 is refused", "bad toc version" in str(exc), str(exc))
try:
    _packs.parse(_packs.PACK_MAGIC.replace(b"1", b"2") + b"\x00" * 32)
    check("foreign container magic is refused", False)
except _packs.PackError as exc:
    check("foreign container magic is refused", "bad magic" in str(exc), str(exc))
# the version is really written by a live write
blob, _i2, _d2 = _packs.build([("a.jpg", b"x" * 40)], 1, "d:t")
check("built packs carry the version", _packs.parse(blob)[0]["v"] == _packs.TOC_VERSION)

# --------------------------------- 5b. a renamed pack must not leave a dead row
print("== dead rows ==")
# simulate the pre-0.1.22 bug: a row whose file was renamed on disk
import sqlite3 as _sq
from app.config import DB_PATH as _DB
children(parent=find("Vacation2019")["id"])  # purge emptied the store: rebuild one pack
_con = _sq.connect(str(_DB))
_row = None
for _ in range(20):        # thumbs are built by worker threads now: wait for a row
    _row = _con.execute("SELECT id,path,loc_id,key,kind,owner_id FROM thumb_pack ORDER BY id LIMIT 1").fetchone()
    if _row:
        break
    _con.close(); time.sleep(1); _con = _sq.connect(str(_DB))
if not _row:
    check("a pack exists to test the dead-row path", False, "no thumb_pack rows")
    print("FAILURES:", fails); sys.exit(1)
moved = _row[1] + ".moved"
os.rename(_row[1], moved)
_con.execute("UPDATE thumb_pack SET path=? WHERE id=?", (moved + ".gone", _row[0]))
_con.commit(); _con.close()
out = subprocess.run([PY, "-m", "app.packtool", "gc"], cwd="/opt/data/mediaexplorer",
                     env=ENV, capture_output=True, text=True)
print("   ", out.stdout.strip() or out.stderr.strip())
_con = _sq.connect(str(_DB))
left = _con.execute("SELECT COUNT(*) FROM thumb_pack WHERE id=?", (_row[0],)).fetchone()[0]
pends = _con.execute("SELECT COUNT(*) FROM nodes WHERE thumb_state!='ready'").fetchone()[0]
_con.close()
check("gc dropped the row whose file was gone", left == 0)
check("gc re-pended the thumbs it pointed at", pends > 0, f"{pends} pending")
check("gc left the renamed file alone", os.path.exists(moved))
out = subprocess.run([PY, "-m", "app.packtool", "verify"], cwd="/opt/data/mediaexplorer",
                     env=ENV, capture_output=True, text=True)
check("verify no longer reports that path", moved + ".gone" not in out.stdout,
      out.stdout.strip().splitlines()[-1])
os.rename(moved, _row[1])

# ------------------------------- 5c. a dangling pack pointer must self-heal
print("== dangling pointers ==")
import sqlite3 as _sq2
_con = _sq2.connect("/opt/data/cache/scratch/mxdata/mediaexplorer.db")
z = [i for i in children(parent=find("Archive2020")["id"]) if i["kind"] == "zip"
     and "Trip2015" in i["name"]][0]
e = settle(f"/api/zip/{z['id']}/entries")
before_ready = sum(1 for i in e["items"] if i.get("thumb"))
_con.execute("DELETE FROM thumb_pack WHERE kind='zip'")   # rows gone, entries still 'ready'
_con.commit()
stuck = _con.execute("SELECT COUNT(*) FROM zip_entries WHERE thumb_state='ready' "
                     "AND pack_id IS NOT NULL").fetchone()[0]
_con.close()
check("entries claim ready with no pack row", stuck > 0, f"{stuck} dangling")
e2 = get(f"/api/zip/{z['id']}/entries")          # opening the zip must rebuild
after_ready = sum(1 for i in e2["items"] if i.get("thumb"))
check("zip rebuilds after the pack row vanished", after_ready == before_ready,
      f"{before_ready} -> {after_ready} thumbs")

print()
# --------------------------------------------- 7. purge endpoint (admin)
print("== purge endpoint ==")
def media_files():
    return [os.path.join(r, f) for r, _d, fs in os.walk(TL) for f in fs
            if not f.endswith(".mxthumbs")]

def admin(path, token="devtoken"):
    hdrs = {"x-mx-token": token} if token else {}
    return urllib.request.Request(B + path, method="POST", headers=hdrs)

def wait_drain(timeout=180):
    """pending drains in background workers after a scan reports idle."""
    for _ in range(timeout // 3):
        cfg = get("/api/config")
        if cfg["pending"] == 0:
            return cfg
        time.sleep(3)
    return get("/api/config")

media_before = media_files()
purge_start = time.time() - 1
try:
    urllib.request.urlopen(admin("/api/admin/purge", None), timeout=30)
    check("purge without token is refused", False, "got 200")
except urllib.error.HTTPError as e:
    check("purge without token is refused", e.code == 403, f"HTTP {e.code}")

res = json.load(urllib.request.urlopen(admin("/api/admin/purge"), timeout=120))
now = packs()
stale = [p for p, (_ino, mt) in now.items() if mt < purge_start]
check("purge removed every pre-existing pack", not stale,
      f"{len(now)} packs on disk, {len(stale)} predate the purge"
      + (": " + ", ".join(stale) if stale else ""))
check("purge left the media alone", media_files() == media_before,
      f"{len(media_before)} media files")
cfg = get("/api/config")
check("purge re-pended the thumbs", cfg["pending"] > 0, f"pending {cfg['pending']}")
s = scan()
cfg = wait_drain()
check("scan rebuilds packs after a purge", len(packs()) > 0 and cfg["pending"] == 0,
      f"{len(packs())} packs, pending {cfg['pending']}")

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
