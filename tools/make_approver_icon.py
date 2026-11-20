# -*- coding: utf-8 -*-
"""Generate the AEDT Builder Approver logo/icon (source asset)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(r"C:\ProgramData\ansys-aedt-mcp\tools\assets")
OUT.mkdir(parents=True, exist_ok=True)

S = 2048  # supersampled canvas
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# rounded-square gradient background
for y in range(S):
    t = y / (S - 1)
    r = int(8 + (18 - 8) * t)
    g = int(15 + (32 - 15) * t)
    b = int(30 + (52 - 30) * t)
    draw.line([(0, y), (S, y)], fill=(r, g, b, 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=430, fill=255)
img.putalpha(mask)
draw = ImageDraw.Draw(img)

# subtle glow ring
draw.ellipse([250, 250, S - 250, S - 250], outline=(45, 212, 191, 40), width=10)

# shield
shield = [
    (S * 0.50, S * 0.16),
    (S * 0.78, S * 0.27),
    (S * 0.755, S * 0.605),
    (S * 0.50, S * 0.85),
    (S * 0.245, S * 0.605),
    (S * 0.22, S * 0.27),
]
draw.polygon(shield, fill=(10, 45, 55, 255))
draw.line(shield + [shield[0]], fill=(45, 212, 191, 255), width=34, joint="curve")

# inner shield
inner = [
    (S * 0.50, S * 0.23),
    (S * 0.71, S * 0.315),
    (S * 0.69, S * 0.58),
    (S * 0.50, S * 0.78),
    (S * 0.31, S * 0.58),
    (S * 0.29, S * 0.315),
]
draw.polygon(inner, outline=(20, 120, 120, 255), width=8)

# antenna arcs (top-right)
for i, radius in enumerate((150, 230, 310)):
    box = [S * 0.5 - radius, S * 0.5 - radius, S * 0.5 + radius, S * 0.5 + radius]
    draw.arc(box, start=-55, end=15, fill=(245, 158, 11, 235), width=26)
    break

# check mark
check = [(S * 0.365, S * 0.50), (S * 0.465, S * 0.60), (S * 0.645, S * 0.395)]
draw.line(check, fill=(248, 250, 252, 255), width=64, joint="curve")
for point in check:
    draw.ellipse(
        [point[0] - 32, point[1] - 32, point[0] + 32, point[1] + 32],
        fill=(248, 250, 252, 255),
    )

# small amber status dot
draw.ellipse([S * 0.68, S * 0.69, S * 0.76, S * 0.77], fill=(245, 158, 11, 255))
draw.ellipse([S * 0.68, S * 0.69, S * 0.76, S * 0.77], outline=(255, 255, 255, 120), width=6)

# export
big = img.resize((1024, 1024), Image.LANCZOS)
big.save(OUT / "aedt_builder_approver_1024.png")
for size in (256, 128, 64, 32):
    big.resize((size, size), Image.LANCZOS).save(OUT / f"aedt_builder_approver_{size}.png")
big.resize((256, 256), Image.LANCZOS).save(
    OUT / "aedt_builder_approver.ico",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("wrote", OUT)
