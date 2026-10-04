import smtplib
import secrets
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import base64

from app.config import settings

logger = logging.getLogger("wifi_guest.notifications")

class NotificationService:
    """Handles OTP generation/verification and email notifications (OTP codes & Wi-Fi boarding pass vouchers)."""

    def __init__(self):
        # In-memory OTP storage: destination -> {code, expires_at, attempts, verified}
        self._otp_store: Dict[str, dict] = {}
        # Stores last simulated OTP/email for offline homelab & test inspection
        self.last_simulated_codes: Dict[str, str] = {}
        self.sent_vouchers_log: List[dict] = []

    def normalize_destination(self, dest: str) -> str:
        return dest.lower().strip()

    def generate_otp(self, destination: str) -> str:
        """Generate a secure 6-digit OTP code valid for 10 minutes."""
        clean_dest = self.normalize_destination(destination)
        code = f"{secrets.randbelow(900000) + 100000}"
        expires_at = datetime.now() + timedelta(minutes=10)
        
        self._otp_store[clean_dest] = {
            "code": code,
            "expires_at": expires_at,
            "attempts": 0,
            "verified": False,
            "created_at": datetime.now()
        }
        self.last_simulated_codes[clean_dest] = code
        logger.info(f"Generated OTP code for {clean_dest} (Expires in 10m)")
        return code

    def verify_otp(self, destination: str, code: str) -> bool:
        """Verify an entered OTP code with brute-force protection (max 5 attempts)."""
        clean_dest = self.normalize_destination(destination)
        record = self._otp_store.get(clean_dest)
        if not record:
            return False

        if datetime.now() > record["expires_at"]:
            logger.warning(f"OTP verification failed for {clean_dest}: Code expired")
            self._otp_store.pop(clean_dest, None)
            return False

        record["attempts"] += 1
        if record["attempts"] > 5:
            logger.warning(f"OTP verification locked for {clean_dest}: Too many attempts")
            self._otp_store.pop(clean_dest, None)
            return False

        if record["code"] == code.strip():
            record["verified"] = True
            logger.info(f"OTP verification succeeded for {clean_dest}")
            return True

        return False

    def is_verified(self, destination: str) -> bool:
        """Check if destination was recently verified (within 20 minutes)."""
        clean_dest = self.normalize_destination(destination)
        record = self._otp_store.get(clean_dest)
        if not record or not record.get("verified"):
            return False
        
        # Valid if created within the last 20 minutes
        if (datetime.now() - record["created_at"]).total_seconds() > 1200:
            return False
        return True

    def validate_sponsor_domain(self, sponsor_email: str) -> bool:
        """Check if sponsor email belongs to allowed internal corporate domains."""
        if not settings.ALLOWED_SPONSOR_DOMAINS:
            return True
        if not sponsor_email or "@" not in sponsor_email:
            return False
        
        domain = sponsor_email.split("@")[-1].lower().strip()
        allowed = [d.strip().lower() for d in settings.ALLOWED_SPONSOR_DOMAINS.split(",") if d.strip()]
        return domain in allowed

    def _send_email(self, recipient: str, subject: str, html_body: str, qr_png_bytes: Optional[bytes] = None) -> bool:
        """Internal helper to dispatch email via SMTP or simulate offline."""
        if not settings.SMTP_HOST:
            logger.info(
                f"[EMAIL SIMULATION] SMTP not configured. Simulating email to '{recipient}' | "
                f"Subject: {subject}"
            )
            return True

        try:
            msg = MIMEMultipart("related")
            msg["Subject"] = subject
            msg["From"] = settings.SMTP_FROM
            msg["To"] = recipient

            alt = MIMEMultipart("alternative")
            msg.attach(alt)

            # HTML part
            html_part = MIMEText(html_body, "html", "utf-8")
            alt.attach(html_part)

            # Embedded QR Code Image (if provided)
            if qr_png_bytes:
                img_part = MIMEImage(qr_png_bytes, "png")
                img_part.add_header("Content-ID", "<wifi_qrcode>")
                img_part.add_header("Content-Disposition", "inline", filename="qrcode.png")
                msg.attach(img_part)

            # Connect SMTP
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10)
            if settings.SMTP_TLS:
                server.starttls()
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)

            server.send_message(msg)
            server.quit()
            logger.info(f"Email successfully sent to {recipient} via {settings.SMTP_HOST}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email to {recipient} via SMTP: {e}")
            return False

    def send_otp_email(self, recipient_email: str, code: str) -> bool:
        """Send verification OTP code via email."""
        clean_email = self.normalize_destination(recipient_email)
        subject = f"Votre code de vérification Wi-Fi : {code}"
        
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f7f7f8; margin: 0; padding: 24px; color: #171717; }}
            .card {{ max-width: 480px; margin: 0 auto; background: #ffffff; border: 1px solid #e5e5e5; border-radius: 20px; padding: 32px; box-shadow: 0 4px 20px rgba(0,0,0,0.03); }}
            .brand {{ font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #737373; margin-bottom: 8px; }}
            h1 {{ font-size: 22px; font-weight: 800; color: #0a0a0a; margin: 0 0 16px 0; }}
            .code-box {{ background: #fafafa; border: 2px dashed #0a0a0a; border-radius: 14px; text-align: center; padding: 20px; margin: 24px 0; }}
            .code {{ font-family: monospace; font-size: 32px; font-weight: 900; letter-spacing: 0.25em; color: #0a0a0a; }}
            p {{ font-size: 14px; line-height: 1.5; color: #525252; margin: 0 0 12px 0; }}
            .footer {{ font-size: 11px; color: #a3a3a3; margin-top: 24px; border-top: 1px solid #f0f0f0; pt: 16px; text-align: center; }}
          </style>
        </head>
        <body>
          <div class="card">
            <div class="brand">{settings.APP_TITLE}</div>
            <h1>Vérification de votre identité</h1>
            <p>Voici votre code de sécurité temporaire pour valider votre demande d'accès au réseau Wi-Fi invité :</p>
            <div class="code-box">
              <div class="code">{code}</div>
            </div>
            <p>Ce code est valable pendant <strong>10 minutes</strong>. Ne le partagez avec personne.</p>
            <div class="footer">
              Ce message est automatique. Si vous n'êtes pas à l'origine de cette demande, vous pouvez l'ignorer.
            </div>
          </div>
        </body>
        </html>
        """
        return self._send_email(clean_email, subject, html_body)

    def send_voucher_email(
        self,
        guest_name: str,
        ssid: str,
        password: str,
        expires_at_str: str,
        profile_name: str,
        recipient_email: str,
        sponsor_name: Optional[str] = None,
        qr_b64: Optional[str] = None
    ) -> bool:
        """Send complete boarding pass voucher to guest or sponsor with embedded QR code."""
        clean_email = self.normalize_destination(recipient_email)
        subject = f"Vos identifiants Wi-Fi Invité ({ssid}) - {guest_name}"

        # Decode base64 PNG if available
        qr_bytes = None
        qr_img_src = ""
        if qr_b64 and "," in qr_b64:
            try:
                qr_bytes = base64.b64decode(qr_b64.split(",")[1])
                qr_img_src = 'cid:wifi_qrcode'
            except Exception:
                qr_img_src = qr_b64
        elif qr_b64:
            qr_img_src = qr_b64

        sponsor_badge = f"<p style='margin: 4px 0 0 0; font-size: 12px; color: #525252;'>Parrainé par : <strong>{sponsor_name}</strong></p>" if sponsor_name else ""

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f7f7f8; margin: 0; padding: 24px; color: #171717; }}
            .card {{ max-width: 480px; margin: 0 auto; background: #ffffff; border: 1px solid #e5e5e5; border-radius: 24px; padding: 32px; box-shadow: 0 4px 25px rgba(0,0,0,0.04); }}
            .badge {{ display: inline-block; background: #f5f5f5; border: 1px solid #e5e5e5; border-radius: 20px; padding: 4px 12px; font-size: 11px; font-weight: 700; color: #525252; margin-bottom: 12px; }}
            h1 {{ font-size: 24px; font-weight: 800; color: #0a0a0a; margin: 0 0 4px 0; }}
            .qr-container {{ text-align: center; margin: 24px 0; padding: 16px; background: #fafafa; border: 1px solid #f0f0f0; border-radius: 16px; }}
            .qr-container img {{ width: 180px; height: 180px; border-radius: 8px; }}
            .credentials {{ background: #fbfbfb; border: 1px solid #ebebeb; border-radius: 16px; padding: 16px; margin: 20px 0; }}
            .cred-row {{ display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 13px; }}
            .cred-label {{ color: #737373; font-weight: 500; }}
            .cred-val {{ font-weight: 700; color: #0a0a0a; }}
            .code-pill {{ font-family: monospace; font-size: 14px; font-weight: 800; background: #ffffff; border: 1px solid #d4d4d4; padding: 3px 8px; border-radius: 6px; letter-spacing: 0.05em; }}
            .notice {{ font-size: 11px; color: #737373; line-height: 1.4; border-top: 1px solid #f0f0f0; padding-top: 14px; margin-top: 20px; }}
          </style>
        </head>
        <body>
          <div class="card">
            <div class="badge">Pass Wi-Fi Invité Officiel</div>
            <h1>Bienvenue, {guest_name}</h1>
            {sponsor_badge}

            <div class="qr-container">
              <img src="{qr_img_src}" alt="QR Code Wi-Fi">
              <p style="font-size: 11px; color: #737373; margin: 8px 0 0 0;">Scannez ce QR Code avec votre smartphone pour vous connecter directement</p>
            </div>

            <div class="credentials">
              <div class="cred-row">
                <span class="cred-label">Réseau Wi-Fi (SSID) :</span>
                <span class="cred-val">{ssid}</span>
              </div>
              <div class="cred-row">
                <span class="cred-label">Clé personnelle (WPA) :</span>
                <span class="code-pill">{password}</span>
              </div>
              <div class="cred-row">
                <span class="cred-label">Profil alloué :</span>
                <span class="cred-val">{profile_name}</span>
              </div>
              <div class="cred-row" style="margin-bottom: 0;">
                <span class="cred-label">Fin de validité :</span>
                <span class="cred-val">{expires_at_str}</span>
              </div>
            </div>

            <div class="notice">
              🔒 <strong>Clé éphémère sécurisée :</strong> Votre clé Wi-Fi est strictement personnelle et sera révoquée automatiquement à la fin de la période indiquée. L'usage de cette connexion implique le respect de la charte informatique de l'établissement.
            </div>
          </div>
        </body>
        </html>
        """
        
        success = self._send_email(clean_email, subject, html_body, qr_png_bytes=qr_bytes)
        if success:
            self.sent_vouchers_log.append({
                "recipient": clean_email,
                "guest_name": guest_name,
                "sent_at": datetime.now().isoformat()
            })
        return success

# Global service instance
notification_service = NotificationService()
