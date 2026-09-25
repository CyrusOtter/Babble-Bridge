"""Generates icon.png (dashboard thumbnail) for the FCAM overlay with Pillow on a development PC.

    python steamframe/overlay/tools/gen_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

size = 256
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)
draw.rounded_rectangle((8, 8, size - 8, size - 8), radius=40, fill=(24, 32, 44, 255), outline=(78, 160, 168, 255), width=6)
# camera body and lens
draw.rounded_rectangle((52, 96, 176, 184), radius=14, fill=(78, 160, 168, 255))
draw.polygon([(176, 118), (212, 98), (212, 182), (176, 162)], fill=(78, 160, 168, 255))
draw.ellipse((84, 112, 140, 168), fill=(24, 32, 44, 255))
draw.ellipse((98, 126, 126, 154), fill=(78, 160, 168, 255))
# wifi arcs
for r, w in ((30, 5), (48, 5), (66, 5)):
    draw.arc((160 - r, 30 - r + 24, 160 + r, 30 + r + 24), start=205, end=335, fill=(230, 236, 240, 255), width=w)
font = ImageFont.truetype(r"C:\Windows\Fonts\consolab.ttf", 40) if Path(r"C:\Windows\Fonts\consolab.ttf").exists() \
    else ImageFont.load_default(size=40)
draw.text((size / 2, 214), "FCAM", font=font, fill=(230, 236, 240, 255), anchor="mm")
out = Path(__file__).resolve().parent.parent / "icon.png"
img.save(out)
print(f"wrote {out}")
