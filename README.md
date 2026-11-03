# MediaExplorer

A self-hosted browser for large image and video libraries — the folders you already have
on a NAS, opened from a phone in seconds instead of minutes.

Point it at a share and it indexes the tree, builds one thumbnail cache per folder, and
serves a grid you can page through with the mouse wheel. Zips are browsed without being
extracted. Videos play in place with a filmstrip preview. Nothing is uploaded, nothing
leaves the box, and there is no database to install: one container, SQLite, and a hidden
cache file per folder.

```
docker run -d --name mediaexplorer -p 8086:8000 \
  -v /mnt/user/appdata/mediaexplorer:/data \
  -v /mnt/user/Photos:/media1:rw \
  -e APP_NAME=Photos -e MEDIA_1_NAME=Photos \
  mediaexplorer:1.0.0
```

## Why another one

Library apps are built for curated media. This one is for the other kind of folder: 16
years of phone photos, screenshots, saved memes, zipped camera dumps and the odd home
video, spread over nested folders nobody wants to tidy. The requirements that shaped it:

- **Browsing must not touch the disk per tile.** Thumbnails are built once, stored inside
  the library, and served from one file per folder.
- **Zips are folders, not archives.** Pictures inside are read with byte ranges — no
  extraction, no temp copies, no double storage.
- **Videos get a poster and a filmstrip**, so you can tell which clip is which without
  opening it.
- **Nothing is decided for you.** No library-wide re-index at startup, no background
  sweep, no folder moved, renamed or reorganised. Every scan is scoped to the folder you
  asked for.

## Features

**Browsing**
- Resizable tree: locations → folders → subfolders, with zips listed separately
- Thumbnail grid with a size slider; sort by name (natural) or date
- Per-folder pins: a folder remembers its own tile size, sort order and animation
- Favourites menu for the folders you live in
- Light and dark theme, following the system setting

**Pictures**
- Full-screen viewer: arrow keys, PgUp/PgDn or the mouse wheel to step; neighbour
  pictures are preloaded, so paging is instant
- Zoom with `+`/`-` or Ctrl+wheel, drag to pan; a single click returns to the grid
- Info panel (`I`): camera and lens, exposure, ISO, date, GPS as a map link, and any text
  embedded in the file — ComfyUI/Stable Diffusion parameters are shown as prompt,
  negative prompt and settings

**Zips**
- Zip tiles show a 2×2 mosaic of what is inside; open one to browse its entries as a
  normal grid
- Entries are thumbnailed and viewed by byte range (fast path for stored/no-compression
  archives; deflate works too, just slower)
- Videos stuck inside a zip are flagged for extraction instead of silently breaking

**Videos**
- Poster plus a second still (3% and 6% into the clip), built in one ffmpeg call
- Optional 8-frame filmstrip: hover flips through it, or let every tile animate
- Built-in player: large transport bar, silent by default, wheel over the video seeks 5%
  per notch, chapters clickable in the info panel
- Video info panel (`I`): container, bitrate, codec and profile, resolution, frame rate,
  bit depth, HDR type, every audio and subtitle track with language and flags, chapters,
  and phone GPS when present
- "Open in VLC" writes an `.m3u` for the cases a browser cannot decode

**Scale and operations**
- Thumbnails live in one hidden `.Folder.mxthumbs` file beside each folder, zip or video
  — 60–400 thumbnails per file instead of thousands of loose JPEGs
- Index and cache are separate: clearing the cache costs a re-encode, never data
- Scan verbs name their scope: *here*, *in subfolders*, *in subfolders and zips*,
  *rebuild thumbnails*, *build filmstrips*
- Live progress: thumbnails appear as they are built, with a countdown and a queue
- `python -m app.packtool verify|gc|stats|purge` for the thumbnail store
- Multi-instance by design: one container per audience, each scoped to its own shares

## Configuration

Everything is environment; nothing is stored in a config file.

| Variable | Default | Meaning |
|---|---|---|
| `APP_NAME` | `MediaExplorer` | Name in the nav bar and window title |
| `PORT` | `8000` | Port inside the container |
| `DATA_PATH` | `/data` | SQLite index (and central packs). Keep it on SSD/cache |
| `MEDIA_DIRS` | `/media1` .. `/media8` | Container dirs to treat as locations |
| `MEDIA_1` .. `MEDIA_20` | — | `Display Name\|/container/path`, for scripted deploys |
| `MEDIA_1_NAME` .. | dir name | Display name for a mounted `/mediaN` |
| `MX_PACK_MODE` | `adjacent` | `adjacent` = cache beside the media; `central` = under `DATA_PATH/packs` |
| `MX_PACK_CACHE_MB` | `96` | RAM budget for hot packs |
| `MX_PACK_MIN_FREE_GB` | `5` | Refuse to write a pack onto a disk below this free space |
| `MX_THUMB_WORKERS` | `0` | Encode threads; `0` sizes the pools from the container's CPU allowance |
| `MX_VIDEO_JOBS` | `0` | Simultaneous video decodes; `0` = auto |
| `MX_HWACCEL` | `auto` | `auto`, `cuda` (NVDEC) or `none` |
| `MX_ANIM_MAX` | `500` | Filmstrips a scan may pre-build for hover previews |
| `MX_WIPE_ON_BUILD` | `1` | `1` = a `VERSION` change wipes the thumbnail store (dev builds); `0` = keep it |
| `MX_AUTO_REBUILD` | `0` | Re-encode thumbnails whose encoder generation is stale |
| `MX_AUTH` | `0` | `1` asks for a password before the library loads |
| `MX_AUTH_PASSWORD` | — | The password used when `MX_AUTH=1` |
| `MX_RESTART_TOKEN` | — | Enables `/api/admin/*` (restart, runtime settings) |
| `PUID` / `PGID` | `99:100` | Unraid-style file ownership |
| `TZ` | system | Timezone for dates and log lines |

### The password screen

`MX_AUTH=1` plus `MX_AUTH_PASSWORD` puts a lock in front of everything except the login
page and static assets: `401` for API calls, a redirect to `/login` for pages, until the
right password is entered.

It is a **casual lock, not a security boundary** — one shared password, salted SHA-256,
an HMAC-signed cookie for 30 days, no accounts, no lockout. Put a reverse proxy with real
SSO in front if the library needs to hold anything that matters. Requests carrying a
valid `X-MX-Token` skip the gate, so deploys keep working while the library is locked.

## How the thumbnail cache works

Every browsable unit gets exactly **one hidden file, in its parent folder, named after
itself**. No directories are ever created inside a media tree:

| Unit | Pack (hidden, beside the unit) |
|---|---|
| folder `Holiday/` | `Photos/.Holiday.mxthumbs` |
| zip `Trip.zip` | `Photos/.Trip.mxthumbs` — one record per entry |
| video `clip.mkv` | `Photos/.clip.mxthumbs` — `poster.jpg` + `f00..f07.jpg` |
| a location root's own loose images | `Photos/._root.mxthumbs` |

Masters are 300×225, letterboxed rather than cropped, so portrait pictures keep their top
and bottom. Each thumbnail records the encoder generation that produced it, which is why
changing how masters are encoded only affects the folders you rescan — never the whole
library at once.

The format, its versioning, rename adoption and garbage collection are documented in
[docs/thumbnail-store.md](docs/thumbnail-store.md).

## Operations

Scoped, never global — every job names the folder or zip it works on:

| Action | Where |
|---|---|
| Scan this folder | Scan button, or right-click → *Scan here* |
| Scan subfolders | right-click → *Scan subfolders* |
| Scan subfolders and zips | right-click → *Scan subfolders and zips* |
| Rebuild thumbnails | right-click → *Re-encode thumbnails here / in subfolders* |
| Build filmstrips | right-click → *Build filmstrips here / in subfolders* |

Scan is priority-queued, so pressing it during a long job jumps ahead instead of doing
nothing, and thumbnails appear in the grid as they are encoded.

Admin endpoints, with header `X-MX-Token: $MX_RESTART_TOKEN`:

```
POST /api/admin/restart                     re-exec the app in place (deploy without docker)
POST /api/admin/settings {"wipe_on_build": false}
GET  /api/admin/work                        what is running right now
GET  /api/admin/packs                       pack counts, and rows whose file is missing
```

The thumbnail store, from inside the container:

```
python -m app.packtool stats     # packs/bytes by kind, thumbs ready vs pending
python -m app.packtool verify    # CRC every record (MISSING = row with no file)
python -m app.packtool gc        # drop packs whose media is gone, plus dead rows
python -m app.packtool purge     # remove every pack and re-pend the thumbnails
```

`purge` (or `find <share> -name '.*.mxthumbs' -delete`) is always safe: the cache is
derived, the index is not.

## Deploying a code change

`VERSION` is the single source of truth. It is shown in the nav bar, returned by
`/api/config`, and used to version the static assets, so bumping it is what makes every
open browser reload itself.

For a source-mounted container (`-v <src>:/app:rw`) no image build is needed:

```
echo 1.0.1 > VERSION
curl -s -X POST -H "X-MX-Token: $MX_RESTART_TOKEN" http://host:8086/api/admin/restart
```

The endpoint answers, then re-execs the process (`os.execv` — same PID, same port). What
it costs: an in-memory scan dies, so don't bounce the app mid-scan or mid-rebuild. Tagged
images remain the release path.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATA_PATH=/tmp/mxdata MEDIA_1='Photos|/path/to/photos' MX_PACK_MODE=central \
  .venv/bin/python -m uvicorn app.server:app --port 8000
```

Build the image:

```
docker build -t mediaexplorer:1.0.0 .
```

Requirements: Python 3.11+, Pillow, and ffmpeg/ffprobe on `PATH` (the image ships them).
There is no JavaScript build step — the front end is plain ES modules and one stylesheet,
served straight from `web/`.

```
app/server.py    FastAPI app: listings, tree, scanning, SSE progress, admin, auth
app/media.py     thumbnails, video posters/filmstrips, ffmpeg plumbing, worker pools
app/packs.py     the .mxthumbs container format (read/write/verify/gc)
app/db.py        SQLite schema and migrations
app/info.py      EXIF/embedded-text and ffprobe readers for the info panel
app/auth.py      the optional password gate
web/             index.html, app.js, style.css, login.html
```

## Limitations, honestly

- The index belongs to the container: two instances pointed at one share keep two indexes.
- Adjacent packs mean the app writes into the media tree (one hidden file per folder). Use
  `MX_PACK_MODE=central` if the share must stay pristine or is mounted read-only.
- Picture thumbnails are 300×225; there is no preview size between that and the original.
- Video playback depends on the browser's codecs — HEVC and some MKV audio tracks need an
  external player, which is what "Open in VLC" is for.
- No accounts, no sharing links, no mobile app: it is a LAN tool, behind whatever proxy
  you put in front of it.

## License

MIT — see [LICENSE](LICENSE).
