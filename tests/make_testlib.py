#!/usr/bin/env python3
"""Build a test media library for MediaExplorer."""
import io
import os
import random
import zipfile
from pathlib import Path

from PIL import Image

ROOT = Path("/opt/data/cache/scratch/testlib")
random.seed(42)

def img_bytes(w, h, base, label=""):
    im = Image.new("RGB", (w, h))
    px = im.load()
    for x in range(w):
        for y in range(h):
            px[x, y] = ((base * 37 + x * 255 // w) % 256,
                        (base * 91 + y * 255 // h) % 256,
                        (base * 13 + (x + y) * 255 // (w + h)) % 256)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()

def png_bytes(w, h, base):
    im = Image.new("RGB", (w, h))
    px = im.load()
    for x in range(w):
        for y in range(h):
            px[x, y] = ((base + x) % 256, (base + y) % 256, 255 - base % 256)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()

# Photos location
vac = ROOT / "photos" / "Vacation2019"
vac.mkdir(parents=True, exist_ok=True)
for i in [1, 2, 9, 10, 42, 100]:
    w, h = random.choice([(800, 600), (640, 960), (1024, 768), (900, 900)])
    (vac / f"IMG_{i}.jpg").write_bytes(img_bytes(w, h, i * 7))

port = ROOT / "photos" / "Portraits"
port.mkdir(parents=True, exist_ok=True)
for i in range(4):
    (port / f"pic{i}.png").write_bytes(png_bytes(500, 800, i * 50))

arch = ROOT / "photos" / "Archive2020"
arch.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(arch / "Trip.zip", "w", zipfile.ZIP_STORED) as zf:
    for i in range(1, 7):
        zf.writestr(f"photo_{i:02d}.jpg", img_bytes(900, 600, i * 20))
# stray video inside the zip
sub = os.system("ffmpeg -v error -y -f lavfi -i testsrc=duration=4:size=320x240:rate=10 "
                "-pix_fmt yuv420p /opt/data/cache/scratch/stray.mp4")
assert sub == 0
with zipfile.ZipFile(arch / "Trip.zip", "a") as zf:
    zf.write("/opt/data/cache/scratch/stray.mp4", "holiday_clip.mp4")

with zipfile.ZipFile(arch / "Small.zip", "w", zipfile.ZIP_STORED) as zf:
    for i in range(3):
        zf.writestr(f"scan_{i}.png", png_bytes(700, 500, i * 80))

# a folder and a zip sharing a stem: their packs must not fight over one name
small = arch / "Small"
small.mkdir(parents=True, exist_ok=True)
(small / "loose.png").write_bytes(png_bytes(800, 600, 5))

vids = ROOT / "photos" / "Videos"
vids.mkdir(parents=True, exist_ok=True)
os.system("ffmpeg -v error -y -f lavfi -i testsrc2=duration=45:size=640x360:rate=24 "
          f"-pix_fmt yuv420p {vids}/short.mp4")
os.system("ffmpeg -v error -y -f lavfi -i testsrc2=duration=480:size=640x360:rate=5 "
          f"-b:v 120k -pix_fmt yuv420p {vids}/eightmin.mp4")

# Work location
work = ROOT / "work" / "Scans"
(work / "Invoices").mkdir(parents=True, exist_ok=True)
(work / "Receipts").mkdir(parents=True, exist_ok=True)
(work / "doc1.png").write_bytes(png_bytes(800, 1000, 30))
for i in range(3):
    (work / "Invoices" / f"inv_{i}.png").write_bytes(png_bytes(800, 1000, 60 + i))
print("test library built at", ROOT)
for p in sorted(ROOT.rglob("*")):
    if p.is_file():
        print(f"{p.stat().st_size:>10}  {p.relative_to(ROOT)}")
