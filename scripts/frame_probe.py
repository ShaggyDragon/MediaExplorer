"""Probe filmstrip members of video clips inside the running app's store.

Usage (inside the container):
    python3 /app/scripts/frame_probe.py MATCH [limit]

MATCH is a fragment of a node's rel path (case sensitive), e.g. "www.FakeAgentUK.com".
Prints, per video: member count, how many DISTINCT frame hashes there are, and
whether the poster equals any frame -- i.e. whether the strip is actually frozen.
"""

import hashlib
import sys

from app import packs
from app.db import db, q
from app.media import video_folder_spot, video_ns, POSTER_NAME, FRAME_COUNT, _frame_name


def main() -> int:
    db()
    match = sys.argv[1] if len(sys.argv) > 1 else ""
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    rows = q("SELECT id, name, rel, loc_id, parent_id, size, mtime, thumb_state,"
             " thumb_gen, pack_id FROM nodes WHERE kind='video' AND rel LIKE ?"
             " ORDER BY rel LIMIT ?", (f"%{match}%", limit))
    print(f"{len(rows)} video(s) matching {match!r}\n")
    for r in rows:
        owner, hint, key = video_folder_spot(r)
        path = packs.resolve(r["loc_id"], key, owner, hint)
        ns = video_ns(r["id"])
        try:
            _b, index, _d = packs.read(path)
        except (packs.PackError, OSError) as e:
            print(f"[{r['id']}] {r['rel']}: pack unreadable ({e}) {path}")
            continue
        digests, sizes = {}, {}
        for name in [POSTER_NAME] + [_frame_name(i) for i in range(FRAME_COUNT)]:
            hit = index.get(ns + name)
            if not hit:
                continue
            blob = packs.item(path, ns + name)
            digests[name] = hashlib.md5(blob).hexdigest()[:10]
            sizes[name] = len(blob)
        frames = [n for n in digests if n != POSTER_NAME]
        distinct = len(set(digests[n] for n in frames))
        frozen = "FROZEN" if frames and distinct <= 1 else ("ok" if frames else "no strip")
        print(f"[{r['id']}] {r['rel']}")
        print(f"    members={len(digests)} frames={len(frames)} distinct_frame_hashes={distinct}"
              f"  -> {frozen}  state={r['thumb_state']} gen={r['thumb_gen']} bytes={r['size']}")
        print("    " + " ".join(f"{n}:{d}" for n, d in digests.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
