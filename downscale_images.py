#!/usr/bin/env python3
"""Halve the dimensions of already-generated WebP images in place.

Idempotent by construction: only images whose long edge exceeds THRESHOLD are
resized (2K ~2528px -> ~1264px, which is below THRESHOLD, so a second run is a
no-op). Re-saves as quality-80 WebP to match the pipeline.

    python3 downscale_images.py [root_dir]      # default: output
"""
import glob
import os
import sys

from PIL import Image

THRESHOLD = 1500  # long-edge px above which an image is considered "2K-ish"

root = sys.argv[1] if len(sys.argv) > 1 else "output"
files = sorted(glob.glob(os.path.join(root, "**", "*.webp"), recursive=True))

before = after = 0
done = skipped = 0
for f in files:
    sz = os.path.getsize(f)
    before += sz
    im = Image.open(f)
    if max(im.size) <= THRESHOLD:
        after += sz
        skipped += 1
        continue
    w, h = im.size
    im = im.convert("RGB").resize((w // 2, h // 2), Image.LANCZOS)
    im.save(f, "WEBP", quality=80, method=6)
    after += os.path.getsize(f)
    done += 1

print(f"downscaled {done} / {len(files)} webp under {root}/ "
      f"({skipped} already small)")
print(f"size: {before/1024/1024:.1f} MB -> {after/1024/1024:.1f} MB "
      f"({(before-after)/1024/1024:.1f} MB saved)")
