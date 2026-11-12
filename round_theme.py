"""Rounded, native ttk controls backed by small in-memory PNGs."""

import math
import struct
import tkinter as tk
import zlib


def _chunk(name, payload):
    crc = zlib.crc32(name + payload)
    return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", crc)


def _photo(window, width, height, scanlines):
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(scanlines)) + _chunk(b"IEND", b"")
    )
    return tk.PhotoImage(master=window, data=png, format="png")


def _round_image(window, radius, fill, stroke, stroke_width, tile_size, backdrop):
    seam = radius + 1
    size = 2 * seam + tile_size

    def coverage(distance, inset):
        return max(0, min(1, radius + 0.5 - distance - inset))

    def pixel(x, y):
        distance = math.hypot(max(radius - x, x - size + radius, 0),
                              max(radius - y, y - size + radius, 0))
        # Distances from the straight sides matter for their inner stroke.
        if distance == 0:
            distance = max(radius - min(x, size - x, y, size - y), 0)
        alpha = coverage(distance, 0)
        if alpha == 0:
            return *backdrop, 255
        inside = coverage(distance, stroke_width)
        fraction = inside / alpha
        return (
            *(round(outer + (edge + (center - edge) * fraction - outer) * alpha)
              for center, edge, outer in zip(fill, stroke, backdrop)),
            255,
        )

    def row(y):
        edge = [bytes(pixel(x + 0.5, y)) for x in range(seam)]
        center = bytes(pixel(seam + 0.5, y)) * tile_size
        return b"\0" + b"".join(edge) + center + b"".join(reversed(edge))

    top = [row(y + 0.5) for y in range(seam)]
    scanlines = b"".join(top) + row(seam + 0.5) * tile_size + b"".join(reversed(top))
    return _photo(window, size, size, scanlines)


def _chevron(window, scale, color):
    width, height = round(25 * scale), round(15 * scale)
    center_x, center_y = width * 0.43, height / 2
    points = (
        (center_x - 4 * scale, center_y - 2 * scale),
        (center_x, center_y + 2 * scale),
        (center_x + 4 * scale, center_y - 2 * scale),
    )

    def distance(x, y, start, end):
        dx, dy = end[0] - start[0], end[1] - start[1]
        amount = ((x - start[0]) * dx + (y - start[1]) * dy) / (dx * dx + dy * dy)
        amount = max(0, min(1, amount))
        return math.hypot(x - start[0] - amount * dx, y - start[1] - amount * dy)

    def pixel(x, y):
        d = min(distance(x, y, points[0], points[1]), distance(x, y, points[1], points[2]))
        alpha = max(0, min(1, 0.85 * scale + 0.5 - d))
        return *color, round(alpha * 255)

    rows = [
        b"\0" + b"".join(bytes(pixel(x + 0.5, y + 0.5)) for x in range(width))
        for y in range(height)
    ]
    return _photo(window, width, height, b"".join(rows))


def install(style, window, scale, *, bg, panel, ink, muted, accent, border,
            nav, nav_active, nav_account, nav_signed_in):
    """Apply round assets to the current theme and retain their Tk images."""
    scale = max(0.75, scale)
    images = {}

    def rgb(color):
        return tuple(value >> 8 for value in window.winfo_rgb(color))

    def blend(first, second, weight):
        return "#{:02x}{:02x}{:02x}".format(
            *(round(a + (b - a) * weight) for a, b in zip(rgb(first), rgb(second)))
        )

    def image(radius, fill, stroke, line, tile_size, backdrop):
        fill = rgb(fill)
        stroke = rgb(stroke) if stroke else fill
        key = (radius, fill, stroke, line, tile_size, rgb(backdrop))
        if key not in images:
            images[key] = _round_image(window, *key)
        return images[key]

    def surface(name, radius, normal, variants=(), *, minimum=True, backdrop=bg):
        radius = round(radius * scale)
        line = max(1, round(scale))
        # Tk tiles the center. A single-pixel tile makes large cards costly to repaint.
        tile_size = round((32 if minimum else 96) * scale)
        fill, stroke = normal
        default = image(radius, fill, stroke, line, tile_size, backdrop)
        states = [
            (*state, image(radius, state_fill, state_stroke, max(line, round(state_line * scale)),
                           tile_size, backdrop))
            for state, state_fill, state_stroke, state_line in variants
        ]
        minimum_size = 2 * radius + 3 if minimum else 0
        style.element_create(name, "image", default, *states, border=radius + 1,
                             padding=0, sticky="nswe", width=minimum_size, height=minimum_size)
        return name

    def button(name, fill, edge, foreground, hover, pressed, disabled,
               focus, *, radius=9, anchor="center", padding=None, backdrop=bg):
        element = surface(
            f"amdl.{name}.border", radius, (fill, edge),
            (
                (("disabled",), disabled, None, 1),
                (("focus", "pressed"), pressed, focus, 2),
                (("focus", "active"), hover, focus, 2),
                (("focus",), fill, focus, 2),
                (("pressed",), pressed, edge, 1),
                (("active",), hover, edge, 1),
            ), backdrop=backdrop,
        )
        style.layout(name, [(element, {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]}),
        ]})])
        style.configure(name, anchor=anchor, foreground=foreground)
        if padding is not None:
            style.configure(name, padding=padding)
        style.map(name, foreground=[("disabled", blend(disabled, muted, 0.74)),
                                    ("!disabled", foreground)])

    for standard, primary, outer in (("TButton", "Primary.TButton", bg),
                                     ("Card.TButton", "CardPrimary.TButton", panel)):
        button(standard, panel, border, ink, blend(panel, accent, 0.08),
               blend(panel, accent, 0.15), blend(panel, bg, 0.6), accent, backdrop=outer)
        button(primary, accent, None, "white", blend(accent, ink, 0.18),
               blend(accent, ink, 0.3), blend(panel, accent, 0.18), blend(accent, ink, 0.65),
               backdrop=outer)
    selected = blend(panel, accent, 0.13)
    button("Selected.TButton", selected, blend(border, accent, 0.26), accent,
           blend(panel, accent, 0.18), blend(panel, accent, 0.24), bg, accent)

    for name, fill, foreground in (
        ("Nav.TButton", nav, "white"),
        ("NavActive.TButton", nav_active, "white"),
        ("Account.TButton", nav_account, "white"),
        ("AccountActive.TButton", nav_active, "white"),
        ("AccountSignedIn.TButton", nav_account, nav_signed_in),
        ("AccountSignedInActive.TButton", nav_active, nav_signed_in),
    ):
        button(name, fill, None, foreground, blend(fill, nav_signed_in, 0.1),
               blend(fill, nav, 0.24), blend(fill, nav, 0.5), nav_signed_in,
               radius=10, anchor="w", padding=(round(11 * scale), round(8 * scale)), backdrop=nav)

    fields = (
        (("disabled",), blend(panel, bg, 0.63), border, 1),
        (("focus",), panel, accent, 2),
    )
    for name, radius, outer in (("TEntry", 8, panel), ("MusicSearch.TEntry", 11, bg)):
        element = surface(f"amdl.{name}.field", radius, (panel, border), fields, backdrop=outer)
        style.layout(name, [(element, {"sticky": "nswe", "children": [
            ("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]}),
        ]})])
    style.configure("TEntry", padding=(round(9 * scale), round(6 * scale)))

    images["chevron"] = _chevron(window, scale, rgb(muted))
    style.element_create("amdl.Combobox.downarrow", "image", images["chevron"], sticky="")
    for name, outer in (("TCombobox", panel), ("Surface.TCombobox", bg)):
        field = surface(f"amdl.{name}.field", 8, (panel, border), fields, backdrop=outer)
        style.layout(name, [(field, {"sticky": "nswe", "children": [
            ("amdl.Combobox.downarrow", {"side": "right", "sticky": ""}),
            ("Combobox.padding", {"sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"}),
            ]}),
        ]})])
    style.configure("TCombobox", padding=(round(9 * scale), round(6 * scale), 2, round(6 * scale)))

    card = surface("amdl.Card.border", 16, (panel, border), minimum=False)
    style.layout("Card.TFrame", [(card, {"sticky": "nswe"})])
    style.configure("Card.TFrame", background=panel)
    window._amdl_round_assets = images
    return images
