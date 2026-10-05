"""Validate avatar uploads by content (not by client-declared type). PNG / JPEG / WebP only; no SVG."""
import struct

MAX_BYTES = 256 * 1024
MAX_DIM = 1024


def _jpeg_size(d):
    i, n = 2, len(d)
    while i + 9 < n:
        if d[i] != 0xFF:
            i += 1
            continue
        m = d[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m in (0x01,) or 0xD0 <= m <= 0xD9:
            i += 2
            continue
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", d[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", d[i + 2:i + 4])[0]
    raise ValueError("unreadable JPEG")


def _webp_size(d):
    kind = d[12:16]
    if kind == b"VP8X":
        return 1 + int.from_bytes(d[24:27], "little"), 1 + int.from_bytes(d[27:30], "little")
    if kind == b"VP8 " and d[23:26] == b"\x9d\x01\x2a":
        return struct.unpack("<H", d[26:28])[0] & 0x3FFF, struct.unpack("<H", d[28:30])[0] & 0x3FFF
    if kind == b"VP8L" and d[20:21] == b"\x2f":
        bits = int.from_bytes(d[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise ValueError("unreadable WebP")


def sniff(data):
    """-> (mime, width, height). Raises ValueError with a user-facing message."""
    if not data:
        raise ValueError("empty upload")
    if len(data) > MAX_BYTES:
        raise ValueError("image too large (max %d KB)" % (MAX_BYTES // 1024))
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            mime, (w, h) = "image/png", struct.unpack(">II", data[16:24])
        elif data[:3] == b"\xff\xd8\xff":
            mime, (w, h) = "image/jpeg", _jpeg_size(data)
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            mime, (w, h) = "image/webp", _webp_size(data)
        else:
            raise ValueError("unsupported image type (use PNG, JPEG or WebP)")
    except (struct.error, IndexError):
        raise ValueError("corrupt image")
    if not (1 <= w <= MAX_DIM and 1 <= h <= MAX_DIM):
        raise ValueError("image dimensions must be at most %dx%d" % (MAX_DIM, MAX_DIM))
    return mime, w, h
