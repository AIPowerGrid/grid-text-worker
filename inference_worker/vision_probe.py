"""Nonce-in-image vision probe.

Auto-detecting whether a backend's model accepts image input is otherwise
unreliable: vLLM exposes no modality field and silently *ignores* an image on a
text model (HTTP 200), while ollama 400-rejects it — so neither status nor
metadata is a dependable signal across engines. But no blind (text-only) model
can ever reproduce a RANDOM nonce it never saw. So we render a 4-digit nonce into
a small PNG, ask the model to read it back, and treat reading it (>=3/4 digits) as
proof of genuine image input. This is the validator's nonce-echo canary moved
into the image channel; false positives are ~0 by construction.

No third-party imaging deps: the PNG is hand-encoded with a 5x7 bitmap font.
"""

import base64
import secrets
import struct
import zlib

# 5x7 bitmap font for digits 0-9 ('1' = ink).
_FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}


def make_nonce(length: int = 4) -> str:
    """A random digit string the model can only know by reading the image."""
    return "".join(secrets.choice("0123456789") for _ in range(length))


def render_nonce_png_b64(nonce: str, scale: int = 16, pad: int = 16) -> str:
    """Render `nonce` as black digits on white into a base64 PNG (grayscale)."""
    gw = len(nonce) * 6 - 1
    w = gw * scale + 2 * pad
    h = 7 * scale + 2 * pad
    img = [[255] * w for _ in range(h)]
    for ci, ch in enumerate(nonce):
        glyph = _FONT.get(ch)
        if not glyph:
            continue
        for ry, row in enumerate(glyph):
            for rx, bit in enumerate(row):
                if bit == "1":
                    for dy in range(scale):
                        base_y = pad + ry * scale + dy
                        base_x = pad + (ci * 6 + rx) * scale
                        for dx in range(scale):
                            img[base_y][base_x + dx] = 0
    raw = b"".join(b"\x00" + bytes(r) for r in img)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


def nonce_match(answer: str, nonce: str) -> int:
    """How many of the nonce's digits the model got, in order (0..len). The first
    run of digits in the answer is compared positionally to the nonce."""
    digits = "".join(c for c in answer if c.isdigit())[: len(nonce)]
    digits = digits.ljust(len(nonce))
    return sum(1 for a, b in zip(digits, nonce) if a == b)
