"""Generates icon.png (dashboard thumbnail) for the FCAM overlay with Pillow on a development PC.

    python overlay/tools/gen_icon.py

Layout (256x256): rounded card with a teal border, a camera in the middle with a Wi-Fi symbol
(dot and three arcs) rising from its top edge, and "FCAM" underneath. Every element stays inside
the card's border; the script checks that before saving.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 256
BG = (24, 32, 44, 255)
TEAL = (78, 160, 168, 255)
WHITE = (230, 236, 240, 255)
CARD = (8, 8, SIZE - 8, SIZE - 8)
BORDER = 6
INNER = CARD[1] + BORDER  # first pixel row/column inside the border

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)
draw.rounded_rectangle(CARD, radius=40, fill=BG, outline=TEAL, width=BORDER)

# camera: body, lens housing, lens
BODY = (52, 110, 176, 190)
draw.rounded_rectangle(BODY, radius=14, fill=TEAL)
draw.polygon([(176, 130), (212, 112), (212, 188), (176, 170)], fill=TEAL)
draw.ellipse((86, 122, 142, 178), fill=BG)
draw.ellipse((100, 136, 128, 164), fill=TEAL)

# Wi-Fi symbol centred over the camera (body plus lens housing span x 52..212)
CX, CY = 132, 96
ARC_WIDTH = 7
RADII = (18, 34, 50)
draw.ellipse((CX - 6, CY - 6, CX + 6, CY + 6), fill=WHITE)
for r in RADII:
    draw.arc((CX - r, CY - r, CX + r, CY + r), start=225, end=315, fill=WHITE, width=ARC_WIDTH)

font_path = Path(r"C:\Windows\Fonts\consolab.ttf")
font = ImageFont.truetype(str(font_path), 38) if font_path.exists() else ImageFont.load_default(size=38)
draw.text((SIZE / 2, 218), "FCAM", font=font, fill=WHITE, anchor="mm")

# every drawn element must stay inside the border
top_of_arcs = CY - max(RADII)
assert top_of_arcs >= INNER + 6, "Wi-Fi arcs reach the border"
assert CY + 6 < BODY[1], "Wi-Fi dot overlaps the camera"
text_box = draw.textbbox((SIZE / 2, 218), "FCAM", font=font, anchor="mm")
assert text_box[1] > BODY[3] + 4 and text_box[3] <= SIZE - INNER - 4, "text collides with the camera or the border"

out = Path(__file__).resolve().parent.parent / "icon.png"
img.save(out)
print(f"wrote {out}")
