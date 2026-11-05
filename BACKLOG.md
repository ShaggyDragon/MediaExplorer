# MediaExplorer backlog

Ideas that are agreed but not built. Keep the reasoning with the idea — the
"why" is what stops a future session from re-litigating it.

## RAW support: CR2 / DNG (parked, Andy 2026-11-01)

**Status:** parked. Andy: "One for the future. Maybe we put this on the to-do list."

**What breaks today:** `_classify()` in `app/media.py` returns `None` for an
unknown extension and the scan does `if kind is None: continue`, so a DNG or CR2
is not even indexed — it never reaches the tree, the listing or the DB. There
ARE DNGs in `USA/2017/San Diego`; the app shows that folder as 71 jpg + 43 mkv
purely because of that skip.

**Why the allowlist alone is not the fix:** Pillow opens a DNG because a DNG is
a TIFF container, but it reads the first IFD — the embedded preview — and calls
that the image. Pillow issue #6482 ("Raw formats (DNG, GPR, NEF)", open since
Aug 2022) exists for exactly this; a Nikon Z7 DNG reports 256x195. So adding
`.dng` to `IMAGE_EXTS` would silently produce thumbnails of a 256x195 preview.
CR2 is not decodable by Pillow at all.

**Andy's proposed shape (2026-11-01):** "When I have CR2 or DNG files, they are
most definitely not zipped. So maybe we could create JPG versions of the folder,
zip them and treat them like other ZIP files."

**Assessment of that shape:**
- Reuses the entire zip path end to end — entry indexing, `\x01tile.jpg` 2x2
  tiles, entry thumbs, viewer, VLC-style serving — with no RAW code in the
  serve path at all. That is its strength.
- Cost, after Andy's cap (2026-11-01, "capping at a certain resolution is great"): the JPEG set is
  written at **MX_RAW_JPEG_MAX = 4000px long edge, q90** — a 1,000-shot DNG folder goes ~35GB RAW ->
  ~3-4GB of JPEG instead of ~10GB at full 45MP size. 4000px is still 2-3x a 4K screen edge-to-edge and
  survives the viewer's 8x zoom on a 1280 tile box; make it env-tunable so he can raise it per
  deployment without a rebuild. Strip EXIF orientation is already applied (a rotated RAW must not
  produce a sideways JPEG).
- It also adds a `.zip` beside the folder in the media tree, which the tree will
  show as its own amber row. Fine if the zip is a real thing he wants (shareable,
  backup-able); wrong if it is only a cache, in which case the JPEGs belong as
  members of the folder's existing `.mxthumbs` pack with no new files in the tree.
- Deciding question: **is the JPEG set a deliverable or a cache?** Deliverable ->
  zip as proposed. Cache -> pack members, one file per folder, nothing to see.

**Implementation notes if it goes ahead:**
- Generate the JPEGs with `rawpy` (`extract_thumb()` = embedded camera preview,
  typically 1024-2048px; `postprocess()` = full decode). Prefer `extract_thumb`
  when it is >= THUMB_MAX*2, fall back to a decode — same "cheap path first"
  pattern as the GPU/CPU choice for video frames.
- Never write the zip into a folder the scanner walks without deciding what it
  should look like in the tree (it will be classified `zip` and get a tile).
- RAW decode is memory-hungry (a 45MP postprocess is ~300-400MB peak); cap
  concurrency to 1-2, far below MX_VIDEO_JOBS.
- DNG previews are sometimes the only sane representation (compressed, lossy
  sensor data differs per camera) — test colour against the camera JPEG before
  defaulting to a full decode.

**Blocked on:** real CR2/DNG samples to measure preview size against, and an
answer to the deliverable-vs-cache question.

## Bookmarks: right-click a folder, keep it handy (parked, Andy 2026-11-01)

**Status:** parked. Andy: "Something else for the future — bookmarks — right click
a folder, add it to a list of easily accessible folders."

**Shape:** a Bookmarks block pinned at the top of the left tree pane, above the
locations; right-click any folder (tree or grid) -> "Add to bookmarks"; a
bookmarked folder carries a mark in the tree; right-click a bookmark -> "Remove"
and "Reveal in tree". Clicking one navigates exactly as the tree does.

**The one design decision that matters — identity:** node ids are NOT durable
(a `?full=1` purge or a fresh discovery restarts them at 1; the same trap that
forced `?v=` on every thumb URL). A bookmark must therefore store
`(loc_id, rel_path, label)` — e.g. `1, "USA/2017/San Diego", "San Diego"` — and
resolve to a node id at render time. Consequences to design for, not discover:
- A renamed or moved folder leaves a dangling bookmark. Show it dimmed/"missing"
  rather than dropping it silently, and offer "re-point" by suffix match — Andy's
  folders get renamed often enough that this will happen.
- A rescan that hasn't reached the folder yet must not delete the bookmark; only
  an explicit remove does.
- Duplicate detection is on the rel path, not the label.

**Server-side, not localStorage:** Andy browses from the desktop and his Fold 7;
a browser-local list silently forks into two different lists. Store it in the
app's SQLite (`bookmarks(id, loc_id, rel, label, ord, created)`) behind
`GET/POST/DELETE /api/bookmarks` — a few rows, no perf question, and it survives
the container rebuild that a localStorage list would survive too but a *shared*
list would not.

**Small extras worth having when it lands:** drag to reorder; a keyboard way to
open the first N (`Alt+1..9`) since "easily accessible" is the point; and
"Bookmark this folder" in the same context menu that already carries Rescan and
Build filmstrips, so there is one place to look.

