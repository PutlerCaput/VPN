"""QR code rendering for config links and SBP payment links."""

from __future__ import annotations

import io

import qrcode
from qrcode.constants import ERROR_CORRECT_M


def render_qr_png(data: str, box_size: int = 8) -> bytes:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=box_size, border=3)
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
