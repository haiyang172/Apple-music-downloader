"""Render the amdl mark at native Windows and sidebar sizes (requires Pillow)."""

from pathlib import Path

from PIL import Image, ImageDraw


HERE = Path(__file__).resolve().parent
INK = "#102d32"
MINT = "#64f1c7"
ICON_SIZES = (16, 20, 24, 28, 32, 36, 40, 48, 56, 64, 72, 96, 128, 256)
SIDEBAR_SIZES = (32, 36, 45, 54, 63, 72, 90, 108)
AVATAR_SIZES = (20, 24, 30, 36, 42, 48, 60, 72)


def rounded_path(draw, points, width, fill):
    draw.line(points, fill=fill, width=width, joint="curve")
    radius = width / 2
    for x, y in points:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def note(size, tile):
    pixels = size * 8
    image = Image.new("RGBA", (pixels, pixels))
    draw = ImageDraw.Draw(image)
    unit = pixels / 100
    if tile:
        draw.rounded_rectangle((unit, unit, pixels - unit, pixels - unit), radius=21 * unit, fill=INK)
    width = round(9.5 * unit)
    stem = [(43, 70), (43, 29), (73, 22), (73, 61)]
    arrow = [(61, 50), (73, 62), (85, 50)]
    for path in (stem, arrow):
        rounded_path(draw, [(round(x * unit), round(y * unit)) for x, y in path], width, MINT)
    draw.ellipse(tuple(round(value * unit) for value in (17, 60, 48, 83)), fill=MINT)
    if not tile:
        image = image.crop(image.getbbox())
        limit = size - max(3, size // 10)
        image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
        frame = Image.new("RGBA", (size, size))
        frame.alpha_composite(image, ((size - image.width) // 2, (size - image.height) // 2))
        return frame
    return image.resize((size, size), Image.Resampling.LANCZOS)


def avatar(size):
    pixels = size * 8
    unit = pixels / 100
    image = Image.new("RGBA", (pixels, pixels))
    draw = ImageDraw.Draw(image)
    width = max(1, round(6 * unit))
    draw.ellipse((8 * unit, 8 * unit, 92 * unit, 92 * unit), outline=MINT, width=width)
    draw.ellipse((37 * unit, 27 * unit, 63 * unit, 53 * unit), outline=MINT, width=width)
    draw.arc((26 * unit, 57 * unit, 74 * unit, 88 * unit), 180, 360, fill=MINT, width=width)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main():
    note(512, tile=True).save(HERE / "AMDLStudio.png", optimize=True)
    frames = [note(size, tile=True) for size in ICON_SIZES]
    frames[-1].save(HERE / "AMDLStudio.ico", sizes=[frame.size for frame in frames], append_images=frames[:-1])
    for size in SIDEBAR_SIZES:
        note(size, tile=False).save(HERE / f"amdl-{size}.png", optimize=True)
    for size in AVATAR_SIZES:
        avatar(size).save(HERE / f"account-{size}.png", optimize=True)


if __name__ == "__main__":
    main()
