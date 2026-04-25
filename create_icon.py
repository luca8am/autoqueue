"""
Genera icon.ico con diseño verde estilo AutoQueue.
Tres círculos: fondo oscuro + anillo intermedio + punto verde central.
Requiere: pip install Pillow
"""

import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("[ERROR] Pillow no instalado. Ejecuta: pip install Pillow")
    sys.exit(1)

SIZES = [16, 32, 48, 64, 128, 256]

def _crear_frame(size):
    img = Image.new('RGBA', (size, size), color=(17, 17, 17, 255))
    draw = ImageDraw.Draw(img)

    center = size / 2
    margin = size * 0.08

    outer_r = size / 2 - margin
    mid_r = outer_r * 0.7
    inner_r = mid_r * 0.55

    draw.ellipse(
        [(center - outer_r, center - outer_r), (center + outer_r, center + outer_r)],
        fill=(42, 92, 58, 80),
        outline=None
    )

    draw.ellipse(
        [(center - mid_r, center - mid_r), (center + mid_r, center + mid_r)],
        fill=(42, 92, 58, 200),
        outline=None
    )

    draw.ellipse(
        [(center - inner_r, center - inner_r), (center + inner_r, center + inner_r)],
        fill=(76, 175, 80, 255),
        outline=None
    )

    return img

frames = [_crear_frame(s) for s in SIZES]
try:
    frames[0].save(
        'icon.ico',
        format='ICO',
        sizes=[(s, s) for s in SIZES],
        append_images=frames[1:] if len(frames) > 1 else []
    )
    print("[OK] icon.ico generado correctamente.")
except Exception as e:
    print(f"[ERROR] {e}")
