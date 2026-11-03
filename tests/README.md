# Pack-store end-to-end tests

These three files are the harness the pack store was proven with. They run the
real app against a synthetic library and assert end-to-end behaviour (not units):

- `make_testlib.py`  builds `testlib/` — folders, zips (incl. a stray video inside one),
  a video with a filmstrip, and a folder+zip that share a stem (`Small/` + `Small.zip`).
- `mx_run2.sh`       starts uvicorn on :8765 against `testlib/` with `DATA_PATH=mxdata`.
- `pack_store_e2e.py` drives the API: scan, thumbs, filmstrips, folder/zip/video renames
  (adoption must keep inode+mtime), the browse-time orphan sweep, gc/purge, the pack
  format version gate, and the dead-row repair.

Paths are absolute to `/opt/data/cache/scratch` (scratch is pruned when idle) — copy the
three files there before running, or edit `TL`/`ENV` at the top of the test:

```
cd /opt/data/cache/scratch && python3 make_testlib.py && mkdir -p mxdata
(setsid bash mx_run2.sh > mx_dev.log 2>&1 &) ; sleep 9
/opt/data/cache/scratch/mxenv/bin/python pack_store_e2e.py   # expect: FAILURES: none
```

Always reset `testlib/` and `mxdata/` between runs; several checks rename or delete media.
