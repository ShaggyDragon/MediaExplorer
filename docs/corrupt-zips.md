# Damaged images inside zips

Some archives contain entries that will never thumbnail: a photo half-written by a card that was
pulled mid-copy, a file renamed to `.jpg` that is really HTML, a zip whose directory table is
truncated. None of those are MediaExplorer's fault, and none of them should look like a thumbnail
that simply never arrived.

## What you see

- **A red ⚠ badge on the zip tile**, top-left, with how many entries inside cannot be read. Amber
  means "worth knowing" (contains video); red means "this file is damaged".
- **Inside the zip**, each damaged entry shows a ⚠ glyph in place of its picture, and hovering it
  names the reason ("image is truncated - the file is incomplete").
- Nothing is written to your media. No repair of the archive is attempted — the flag describes the
  file; what to do about it is yours to decide.

## Where the knowledge comes from

The flag is a by-product of thumbnail building. When MediaExplorer decodes an entry and the decode
fails, it records the entry name, the cause, and the size. It does not walk every archive separately:
a zip is examined when its tile is painted or when you open it, which is the same work that builds
its thumbnails.

## The list, for comparing against a backup

Two routes, both readable by a browser or `curl`:

| route | shape |
|---|---|
| `/api/defects` | JSON: one row per flagged zip with location, path, count, size, modified |
| `/api/defects?format=csv` | the same as a spreadsheet (`location,folder,zip_file,bad_entries,size,modified`) |
| `/api/zip/<id>/defects` | per-entry detail for one archive: which entry, what went wrong |

The CSV is designed for the case this was built for: sorting the flagged zips next to the same
archives on a second NAS, newest-modified first, and deciding per file whether to re-copy it.

## Repairing

Replace the archive with a good copy, then let MediaExplorer look at it again — reopening the zip
re-detects. A flag clears itself when the entry decodes, and a re-index of a changed archive drops
all of its old per-entry flags, so a repaired zip cannot keep a stale badge.

Archives indexed before this existed (first release: v1.4.0) have their flag added on first start
from the failures the old code had recorded, with the reason shown as
`unclassified (indexed before v1.4.0)`. A rescan or a rebuild of that zip replaces that placeholder
with the real cause.
