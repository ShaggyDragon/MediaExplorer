"""Print what the running app would answer for a path fragment: session token and
folder ids, so a browser test can drive the same data the UI sees.

    python3 scripts/prod_ctx.py www.FakeAgentUK.com
"""

import sys

from app import auth
from app.db import db, q


def main() -> int:
    db()
    match = sys.argv[1] if len(sys.argv) > 1 else ""
    print("TOKEN", auth.token())
    for r in q("SELECT id, loc_id, parent_id, rel FROM nodes WHERE kind='dir'"
               " AND rel LIKE ? ORDER BY rel", (f"%{match}%",)):
        n = (q("SELECT COUNT(*) c FROM nodes WHERE parent_id=?", (r["id"],)) or [{"c": 0}])[0]["c"]
        print("DIR", r["id"], r["loc_id"], r["parent_id"], n, r["rel"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
