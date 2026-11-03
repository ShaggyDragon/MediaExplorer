# The thumbnail store (`.mxthumbs`)

Design notes for the cache that makes browsing fast. Read this before changing how
thumbnails are written, named or invalidated.

## One hidden file per browsable unit

Every browsable unit gets exactly **one hidden file, in its parent folder, named after
itself**. No directories are ever created inside a media tree:

| Unit | Pack (hidden, beside the unit) |
|---|---|
| folder `Holiday/` | `Photos/.Holiday.mxthumbs` |
| zip `Trip.zip` | `Photos/.Trip.mxthumbs` (one record per entry thumb) |
| video `clip.mkv` | `Photos/.clip.mxthumbs` (`poster.jpg` + `f00..f07.jpg`) |
| location root's own loose images | `Photos/._root.mxthumbs` |

The extension is dropped, so a folder's pack list reads the same whatever the unit is: a
`Holiday/` folder and a `Trip.zip` archive both produce `.<name>.mxthumbs`. Only
`.gz`/`.tar`-style double extensions keep their last part (`backup.tar.gz` →
`.backup.tar.mxthumbs`). If two units in the same folder share a stem — `Small/` next to
`Small.zip`, or `clip.mkv` next to `clip.mp4` — both keep their full name
(`.Small.mxthumbs` for the folder, `.Small.zip.mxthumbs` for the archive) so they can
never overwrite each other. Packs written by an older version under the full name are
still recognised as live, and are renamed to the stem form the next time they are
written.

A folder with three subfolders `ABC/DEF/GHI` therefore holds exactly three packs —
`.ABC.mxthumbs`, `.DEF.mxthumbs`, `.GHI.mxthumbs` — plus its own `.Name.mxthumbs` in *its*
parent. The leading dot hides them from `ls`/Finder, and the DOS hidden attribute
(`user.DOSATTRIB=0x22`) hides them from SMB/Windows.

60–400 thumbnails per file instead of 60–400 loose files: one sequential write per folder
on a spinning disk instead of hundreds of small ones, and one pack per browsable unit
instead of millions of JPEGs.

## Format

```
MXPACK1\n        magic
<records>        raw JPEG bytes, back to back
<JSON TOC>       {members:[…], gens:{member: gen}, …}
<trailer>        8-byte TOC length + CRC32
```

A truncated or foreign file is never read as valid, bit rot is detectable per thumbnail,
and the index stores only `(pack, offset, length)`, so serving a thumbnail never walks the
filesystem layout.

**The format is versioned twice, on purpose.** `MXPACK1` is the *container* version (bump
to `MXPACK2` only for a change the trailer/TOC layout cannot express) and the TOC carries
`"v": 1`, the *schema* version (bump when the item tuple or doc keys change).
`packs.parse()` refuses `v > TOC_VERSION` and any other magic, so an older build meeting a
newer pack treats it as unreadable and rebuilds rather than misreading it — the safe
failure, and the reason a downgrade is survivable. `python -m app.packtool stats` prints
both versions, and `verify` names the offending one (`toc v2 newer than supported v1`).

## Encoder generations

Every thumbnail records the generation of the code that encoded it (in the TOC as
`gens: {member: gen}` and on the `nodes` / `zip_entries` row as `thumb_gen`). A folder is
brought up to the current generation only by an explicit, folder-scoped action — Scan, or
*Re-encode thumbnails here* — so a change to how masters are made costs one slow visit per
folder, never a library-wide rebuild.

Rules that were learned the hard way:

- **A pack-level stamp is not evidence about its members.** The pack is rewritten on every
  append (a new 4-up tile today, one new photo), so a single stamp dates year-old pictures
  by the most recent write. Provenance is per member, and a pack written before `gens`
  existed reports generation 0 (stale) for every member.
- **Every free-reuse path must check the generation**, or a rebuild silently does nothing:
  a reuse path may only mark a row `ready` when *that member's* recorded generation is
  current; otherwise re-encode.
- **A forced rebuild must skip adoption.** `adopt_pack` and the poster path both happily
  reuse existing bytes unless told `force=True`.

## Renames and moves

- **Renamed folder / zip / video** → the pack is found by content identity (TOC members +
  source sizes + mtimes) among the hidden packs in the same directory, `os.replace()`d to
  the new name and adopted (same inode *and* mtime afterwards, so nothing was rewritten).
- **Moved to a different parent** → the old pack stays behind and is collected by `gc`;
  the thumbnails are regenerated on the next view.
- **Source deleted** → its pack is deleted with the folder's contents, and any remaining
  pack with no index row is removed by `gc`, which only stats directories it already
  tracks — it never walks the media tree.

### Orphan sweep on folder entry

Every time a folder is listed, the listing that `diff_dir` already made is reused to look
for hidden packs whose unit is no longer beside them: `.Holiday.mxthumbs` with no
`Holiday/` next to it is unlinked and its `thumb_pack` row dropped. Guards: a pack is only
touched when its name matches no entry in that directory (compared on the truncated name
too, so >120-character names are safe), `._root.mxthumbs` is exempt, and a pack with no
`thumb_pack` row is never deleted (it may not be ours). The sweep is **browse-only** —
during a scan it must be off, because the rename phase still needs those strays to adopt.

## Operating it

```
python -m app.packtool stats     # packs/bytes by kind, thumbs ready vs pending
python -m app.packtool verify    # CRC every record (MISSING = row with no file)
python -m app.packtool gc        # drop packs whose media is gone + dead rows + leftovers
python -m app.packtool purge     # rm every pack (incl. pre-0.1.18 .mxthumbs/ dirs), re-pend thumbs
```

`gc` is also the self-heal for a pack that moved: a row is keyed by `(location, pack key)`,
so a rename updates the row in place, and any row left pointing at a file that is no
longer there is dropped with its thumbnails re-pended (they rebuild on the next view).
`verify` reports `MISSING` for that case rather than misreporting it as corruption.

Sanitising is `packtool purge` or `find <share> -name '.*.mxthumbs' -delete`: the SQLite
index (small, precious) stays on the SSD and only ever holds offsets, so it is never
damaged by clearing the cache, and the cache is never copied wholesale by a backup.
