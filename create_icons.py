#!/usr/bin/env python3
"""Generate Reggie PWA icons (run once during setup/deploy)."""

import os
os.makedirs("static", exist_ok=True)

try:
    from PIL import Image, ImageDraw, ImageFont

    def make_icon(size):
        img  = Image.new("RGB", (size, size), "#0077B6")
        draw = ImageDraw.Draw(img)

        # Lighter blue inner circle
        pad = size // 7
        draw.ellipse([pad, pad, size - pad, size - pad], fill="#00B4D8")

        # "R" centred
        font = None
        for path in [
            "/System/Library/Fonts/SFNSDisplay.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]:
            try:
                font = ImageFont.truetype(path, size // 2)
                break
            except Exception:
                pass

        if font is None:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), "R", font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        draw.text(
            ((size - w) // 2 - bbox[0], (size - h) // 2 - bbox[1]),
            "R", fill="white", font=font,
        )
        return img

    for sz in [192, 512]:
        make_icon(sz).save(f"static/icon-{sz}.png")
    make_icon(180).save("static/apple-touch-icon.png")
    print("Icons created with Pillow.")

except ImportError:
    # Fallback: write a minimal 1×1 blue PNG repeated for each required size
    import struct, zlib

    def tiny_png(r, g, b):
        def chunk(tag, data):
            crc = zlib.crc32(tag + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        idat = zlib.compress(b"\x00" + bytes([r, g, b]))
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")

    data = tiny_png(0, 119, 182)  # #0077B6
    for name in ["icon-192.png", "icon-512.png", "apple-touch-icon.png"]:
        with open(f"static/{name}", "wb") as f:
            f.write(data)
    print("Minimal placeholder icons created. Install Pillow for real icons: pip install Pillow")
