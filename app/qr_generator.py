import io
import base64
import qrcode
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.pil import PilImage

def escape_wifi_string(val: str) -> str:
    """Escape special characters for standard Wi-Fi QR Code string according to ZXing format."""
    if not val:
        return ""
    # Characters \ ;, : " need to be escaped with a backslash \
    for char in ["\\", ";", ",", ":", '"']:
        val = val.replace(char, f"\\{char}")
    return val

def build_wifi_qr_string(ssid: str, password: str, security: str = "WPA", hidden: bool = False) -> str:
    """
    Build standard Wi-Fi QR Code payload according to universal ZXing standard.
    Format: WIFI:T:<WPA|WEP|nopass>;S:<SSID>;P:<PASSWORD>;;
    """
    escaped_ssid = escape_wifi_string(ssid)
    escaped_password = escape_wifi_string(password)
    sec = security.upper() if security else "WPA"
    
    if sec == "NOPASS" or not password:
        return f"WIFI:T:nopass;S:{escaped_ssid};;"
    
    hidden_part = "H:true;" if hidden else ""
    return f"WIFI:T:{sec};S:{escaped_ssid};P:{escaped_password};{hidden_part};"

def generate_qr_code_bytes(wifi_payload: str) -> bytes:
    """Generate raw PNG bytes for a Wi-Fi QR code."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(wifi_payload)
    qr.make(fit=True)

    img = qr.make_image(fill_color="#0f172a", back_color="#ffffff")
    
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()

def generate_qr_code_base64(wifi_payload: str) -> str:
    """Generate Base64 data URI string for embedding directly in HTML <img> tags."""
    raw_bytes = generate_qr_code_bytes(wifi_payload)
    base64_encoded = base64.b64encode(raw_bytes).decode("utf-8")
    return f"data:image/png;base64,{base64_encoded}"
