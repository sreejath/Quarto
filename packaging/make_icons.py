"""Generate crisp macOS (.icns) and Windows (.ico) app icons from the Quarto logo geometry."""
import math
import os
import shutil
import struct
import subprocess
import tempfile
import zlib


def _pt_in_poly(x, y, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
            inside = not inside
    return inside


def _dist_seg(x, y, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / l2)) if l2 else 0.0
    px, py = x1 + t * dx, y1 + t * dy
    return math.hypot(x - px, y - py)


def _in_round_rect(x, y, w, h, r):
    cx = max(r, min(w - r, x))
    cy = max(r, min(h - r, y))
    return math.hypot(x - cx, y - cy) <= r


def _encode_png(width, height, rgba_bytes):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw.extend(rgba_bytes[y * stride:(y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


def render_logo_png(size, path):
    """Render the Quarto 64x64 logo at size x size with 4x4 supersampling."""
    chevron = [(14, 32), (24, 22), (29, 27), (24, 32), (29, 37), (24, 42)]
    shield = [(44, 18), (52, 22), (52, 42), (44, 46)]
    bg = (15, 23, 42)       # #0F172A
    c_chev = (38, 132, 255)  # #2684FF
    c_arr = (56, 189, 248)   # #38BDF8
    c_shd = (0, 120, 212)    # #0078D4

    ss = 4
    sub_offsets = [(i + 0.5) / ss for i in range(ss)]
    inv_samples = 1.0 / (ss * ss)
    buf = bytearray(size * size * 4)
    scale = 64.0 / size

    for py in range(size):
        for px in range(size):
            r_acc = g_acc = b_acc = a_acc = 0.0
            for sy in sub_offsets:
                y = (py + sy) * scale
                for sx in sub_offsets:
                    x = (px + sx) * scale
                    if not _in_round_rect(x, y, 64.0, 64.0, 14.0):
                        continue
                    r, g, b = bg
                    if _pt_in_poly(x, y, shield):
                        r, g, b = c_shd
                    if _pt_in_poly(x, y, chevron):
                        r, g, b = c_chev
                    if (
                        _dist_seg(x, y, 27, 32, 43, 32) <= 1.75
                        or _dist_seg(x, y, 43, 32, 37, 26) <= 1.75
                        or _dist_seg(x, y, 43, 32, 37, 38) <= 1.75
                    ):
                        r, g, b = c_arr
                    r_acc += r
                    g_acc += g
                    b_acc += b
                    a_acc += 255.0
            idx = (py * size + px) * 4
            if a_acc > 0:
                buf[idx] = int(round(r_acc / a_acc * 255.0))
                buf[idx + 1] = int(round(g_acc / a_acc * 255.0))
                buf[idx + 2] = int(round(b_acc / a_acc * 255.0))
                buf[idx + 3] = int(round(a_acc * inv_samples))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(_encode_png(size, size, buf))


def build_ico(ico_path, png_paths_by_size):
    """Write a Windows multi-resolution .ico embedding PNG frames."""
    frames = []
    for sz in sorted(png_paths_by_size):
        with open(png_paths_by_size[sz], "rb") as fh:
            frames.append((sz, fh.read()))
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries = bytearray()
    payload = bytearray()
    for sz, data in frames:
        w = 0 if sz >= 256 else sz
        entries.extend(struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset))
        payload.extend(data)
        offset += len(data)
    with open(ico_path, "wb") as fh:
        fh.write(header + entries + payload)


def generate(assets_dir):
    tmp = tempfile.mkdtemp(prefix="quarto-icons-")
    try:
        master = os.path.join(tmp, "icon_512.png")
        render_logo_png(512, master)
        iconset = os.path.join(tmp, "Quarto.iconset")
        os.makedirs(iconset, exist_ok=True)
        needed = {16: "icon_16x16.png", 32: "icon_32x32.png", 48: None, 64: None,
                  128: "icon_128x128.png", 256: "icon_256x256.png", 512: "icon_512x512.png"}
        sized = {512: master}
        shutil.copy2(master, os.path.join(iconset, "icon_512x512.png"))
        for sz in (16, 32, 48, 64, 128, 256):
            p = os.path.join(tmp, f"icon_{sz}.png")
            if shutil.which("sips"):
                subprocess.run(["sips", "-z", str(sz), str(sz), master, "--out", p],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                render_logo_png(sz, p)
            sized[sz] = p
            if needed.get(sz):
                shutil.copy2(p, os.path.join(iconset, needed[sz]))
        # Retina @2x slots for macOS iconset
        for lo, hi in ((16, 32), (32, 64), (128, 256), (256, 512)):
            shutil.copy2(sized[hi], os.path.join(iconset, f"icon_{lo}x{lo}@2x.png"))

        build_ico(os.path.join(assets_dir, "Quarto.ico"), {sz: sized[sz] for sz in (16, 32, 48, 256)})
        if shutil.which("iconutil"):
            subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(assets_dir, "Quarto.icns")],
                           check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    generate(os.path.join(root, "assets"))
    print("Generated icons in", os.path.join(root, "assets"))
