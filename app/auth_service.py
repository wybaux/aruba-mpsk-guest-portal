import hashlib
import hmac
import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from enum import Enum
import pyotp
import jwt
from pydantic import BaseModel, Field, EmailStr

from app.config import settings
from app.db import db
from app.notification_service import notification_service

logger = logging.getLogger("wifi_guest.auth")

JWT_ALGORITHM = "HS256"

class UserRole(str, Enum):
    ADMIN = "admin"         # Full system control (profiles, AP config, user management, audit)
    OPERATOR = "operator"   # Reception / Front-desk (create passes, email vouchers, print)
    AUDITOR = "auditor"     # Read-only audit logs & legal compliance export

class UserOut(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: str
    is_active: bool
    mfa_enabled: bool
    mfa_type: str
    created_at: str
    last_login: Optional[str] = None

class CreateUserSchema(BaseModel):
    username: str = Field(..., min_length=3, max_length=40)
    password: str = Field(..., min_length=6, max_length=100)
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: UserRole = UserRole.OPERATOR
    mfa_enabled: bool = False
    mfa_type: str = "totp"

class UpdateUserSchema(BaseModel):
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None
    mfa_enabled: Optional[bool] = None
    mfa_type: Optional[str] = None
    new_password: Optional[str] = None

class LoginPayload(BaseModel):
    username: str
    password: str

class MfaVerifyPayload(BaseModel):
    temp_token: str
    code: str

class AuthService:
    """Enterprise-grade Authentication & RBAC service with Password Salt/Hash and OTP/TOTP 2FA."""

    def __init__(self):
        self.init_default_admin()

    def hash_password(self, password: str, salt: Optional[str] = None) -> Tuple[str, str]:
        """Hash password using PBKDF2-HMAC-SHA256 with 120,000 iterations and 32-byte salt."""
        salt_bytes = bytes.fromhex(salt) if salt else secrets.token_bytes(32)
        pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, 120000)
        return pwd_hash.hex(), salt_bytes.hex()

    def verify_password(self, password: str, password_hash: str, password_salt: str) -> bool:
        """Constant-time password verification against timing attacks."""
        expected_hash, _ = self.hash_password(password, salt=password_salt)
        return hmac.compare_digest(expected_hash, password_hash)

    def create_jwt_token(self, payload: dict, expires_in_hours: int = 12) -> str:
        data = payload.copy()
        now = datetime.now(timezone.utc)
        exp = now + timedelta(hours=expires_in_hours)
        data.update({"exp": exp, "iat": now})
        return jwt.encode(data, settings.SECRET_KEY, algorithm=JWT_ALGORITHM)

    def decode_jwt_token(self, token: str) -> Optional[dict]:
        try:
            return jwt.decode(token, settings.SECRET_KEY, algorithms=[JWT_ALGORITHM])
        except Exception:
            return None

    def init_default_admin(self):
        """Seed default admin user if database users table is empty."""
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users;")
            if cursor.fetchone()[0] == 0:
                admin_pwd = settings.ADMIN_PASSWORD or "admin123"
                p_hash, p_salt = self.hash_password(admin_pwd)
                totp_sec = pyotp.random_base32()
                cursor.execute("""
                INSERT INTO users (
                    username, email, full_name, password_hash, password_salt, role,
                    is_active, mfa_enabled, mfa_type, totp_secret, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    "admin",
                    "admin@entreprise.local",
                    "Administrateur Système",
                    p_hash,
                    p_salt,
                    UserRole.ADMIN.value,
                    1,
                    0,
                    "totp",
                    totp_sec,
                    datetime.now().isoformat()
                ))
                logger.info("[AUTH] Initialized default user 'admin' with RBAC role 'admin'")

    def get_user_by_username(self, username: str) -> Optional[dict]:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE username = ?;", (username.strip().lower(),))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> Optional[dict]:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            try:
                uid = int(user_id)
            except (ValueError, TypeError):
                return None
            cursor.execute("SELECT * FROM users WHERE id = ?;", (uid,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_users(self) -> List[UserOut]:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, email, full_name, role, is_active, mfa_enabled, mfa_type, created_at, last_login FROM users ORDER BY id ASC;")
            rows = cursor.fetchall()
            return [
                UserOut(
                    id=r["id"],
                    username=r["username"],
                    email=r["email"],
                    full_name=r["full_name"],
                    role=r["role"],
                    is_active=bool(r["is_active"]),
                    mfa_enabled=bool(r["mfa_enabled"]),
                    mfa_type=r["mfa_type"] or "totp",
                    created_at=r["created_at"],
                    last_login=r["last_login"]
                )
                for r in rows
            ]

    def create_user(self, payload: CreateUserSchema, creator: str = "system") -> UserOut:
        username = payload.username.strip().lower()
        if self.get_user_by_username(username):
            raise ValueError(f"L'identifiant '{username}' est déjà utilisé.")

        p_hash, p_salt = self.hash_password(payload.password)
        totp_sec = pyotp.random_base32()
        now_str = datetime.now().isoformat()

        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO users (
                username, email, full_name, password_hash, password_salt, role,
                is_active, mfa_enabled, mfa_type, totp_secret, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                username,
                payload.email.strip() if payload.email else None,
                payload.full_name.strip() if payload.full_name else None,
                p_hash,
                p_salt,
                payload.role.value,
                1,
                1 if payload.mfa_enabled else 0,
                payload.mfa_type,
                totp_sec,
                now_str
            ))
            uid = cursor.lastrowid

        db.log_audit_event("USER_CREATED", actor=creator, target=username, details=f"Role: {payload.role.value}")
        logger.info(f"[AUTH] User '{username}' created with role '{payload.role.value}' by '{creator}'")
        return UserOut(
            id=uid,
            username=username,
            email=payload.email,
            full_name=payload.full_name,
            role=payload.role.value,
            is_active=True,
            mfa_enabled=payload.mfa_enabled,
            mfa_type=payload.mfa_type,
            created_at=now_str
        )

    def update_user(self, user_id: int, payload: UpdateUserSchema, actor: str = "system") -> Optional[UserOut]:
        user = self.get_user_by_id(user_id)
        if not user:
            return None

        # Build update queries
        updates = []
        params = []

        if payload.email is not None:
            updates.append("email = ?")
            params.append(payload.email.strip() if payload.email else None)
        if payload.full_name is not None:
            updates.append("full_name = ?")
            params.append(payload.full_name.strip() if payload.full_name else None)
        if payload.role is not None:
            updates.append("role = ?")
            params.append(payload.role.value)
        if payload.is_active is not None:
            updates.append("is_active = ?")
            params.append(1 if payload.is_active else 0)
        if payload.mfa_enabled is not None:
            updates.append("mfa_enabled = ?")
            params.append(1 if payload.mfa_enabled else 0)
        if payload.mfa_type is not None:
            updates.append("mfa_type = ?")
            params.append(payload.mfa_type)
        if payload.new_password and payload.new_password.strip():
            p_hash, p_salt = self.hash_password(payload.new_password.strip())
            updates.append("password_hash = ?")
            params.append(p_hash)
            updates.append("password_salt = ?")
            params.append(p_salt)

        if not updates:
            return self.get_user_out(user_id)

        params.append(user_id)
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?;", params)

        db.log_audit_event("USER_UPDATED", actor=actor, target=user["username"])
        return self.get_user_out(user_id)

    def delete_user(self, user_id: int, actor: str = "system") -> bool:
        user = self.get_user_by_id(user_id)
        if not user:
            return False

        # Protect last admin from deletion
        if user["role"] == UserRole.ADMIN.value:
            with db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1;")
                admin_count = cursor.fetchone()[0]
                if admin_count <= 1:
                    raise ValueError("Impossible de supprimer le dernier compte administrateur actif.")

        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE id = ?;", (user_id,))

        db.log_audit_event("USER_DELETED", actor=actor, target=user["username"])
        logger.info(f"[AUTH] User '{user['username']}' deleted by '{actor}'")
        return True

    def get_user_out(self, user_id: int) -> Optional[UserOut]:
        u = self.get_user_by_id(user_id)
        if not u:
            return None
        return UserOut(
            id=u["id"],
            username=u["username"],
            email=u["email"],
            full_name=u["full_name"],
            role=u["role"],
            is_active=bool(u["is_active"]),
            mfa_enabled=bool(u["mfa_enabled"]),
            mfa_type=u["mfa_type"] or "totp",
            created_at=u["created_at"],
            last_login=u["last_login"]
        )

    # -------------------------------------------------------------------------
    # Authentication & MFA Flow
    # -------------------------------------------------------------------------

    def login_step1(self, username: str, password: str, client_ip: Optional[str] = None) -> dict:
        """
        Step 1 of Login: verifies username and password.
        - If MFA is disabled: returns access JWT token directly.
        - If MFA is enabled: generates temporary MFA challenge token and triggers OTP/TOTP request.
        """
        user = self.get_user_by_username(username)
        if not user or not user["is_active"]:
            db.log_audit_event("LOGIN_FAILED", actor=username, ip_address=client_ip, details="User not found or inactive")
            return {"success": False, "detail": "Identifiant ou mot de passe incorrect"}

        if not self.verify_password(password, user["password_hash"], user["password_salt"]):
            db.log_audit_event("LOGIN_FAILED", actor=username, ip_address=client_ip, details="Invalid password")
            return {"success": False, "detail": "Identifiant ou mot de passe incorrect"}

        # Check if MFA is enabled on this account
        if user["mfa_enabled"]:
            mfa_type = user["mfa_type"] or "totp"
            temp_token = self.create_jwt_token(
                {"sub": str(user["id"]), "type": "mfa_challenge", "username": user["username"]},
                expires_in_hours=1
            )

            # If Email OTP mode, dispatch OTP email
            demo_otp = None
            if mfa_type == "email" and user["email"]:
                code = notification_service.generate_otp(user["email"])
                notification_service.send_otp_email(user["email"], code)
                demo_otp = code if not settings.SMTP_HOST else None

            logger.info(f"[AUTH] MFA challenge initiated for user '{username}' (type: {mfa_type})")
            return {
                "success": True,
                "mfa_required": True,
                "mfa_type": mfa_type,
                "temp_token": temp_token,
                "destination": user["email"] if mfa_type == "email" else None,
                "demo_code": demo_otp
            }

        # No MFA: issue final access token
        self._record_login(user["id"])
        db.log_audit_event("LOGIN_SUCCESS", actor=username, ip_address=client_ip, details="Direct password login")
        token = self.create_jwt_token({
            "sub": str(user["id"]),
            "username": user["username"],
            "role": user["role"],
            "full_name": user["full_name"]
        })
        return {
            "success": True,
            "mfa_required": False,
            "access_token": token,
            "user": self.get_user_out(user["id"]).model_dump()
        }

    def login_step2_mfa(self, temp_token: str, code: str, client_ip: Optional[str] = None) -> dict:
        """
        Step 2 of Login: validates MFA code (TOTP authenticator or Email OTP).
        """
        payload = self.decode_jwt_token(temp_token)
        if not payload or payload.get("type") != "mfa_challenge":
            return {"success": False, "detail": "Session de vérification MFA invalide ou expirée."}

        user = self.get_user_by_id(payload["sub"])
        if not user or not user["is_active"]:
            return {"success": False, "detail": "Utilisateur introuvable ou inactif."}

        mfa_type = user["mfa_type"] or "totp"
        valid = False

        if mfa_type == "totp":
            if user["totp_secret"]:
                totp = pyotp.TOTP(user["totp_secret"])
                valid = totp.verify(code.strip(), valid_window=1)
        elif mfa_type == "email" and user["email"]:
            valid = notification_service.verify_otp(user["email"], code.strip())

        if not valid:
            db.log_audit_event("MFA_FAILED", actor=user["username"], ip_address=client_ip, details=f"Invalid {mfa_type} code")
            return {"success": False, "detail": "Code de sécurité 2FA incorrect ou expiré."}

        self._record_login(user["id"])
        db.log_audit_event("MFA_SUCCESS", actor=user["username"], ip_address=client_ip, details=f"Verified via {mfa_type}")
        token = self.create_jwt_token({
            "sub": str(user["id"]),
            "username": user["username"],
            "role": user["role"],
            "full_name": user["full_name"]
        })
        return {
            "success": True,
            "access_token": token,
            "user": self.get_user_out(user["id"]).model_dump()
        }

    def _record_login(self, user_id: int):
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET last_login = ? WHERE id = ?;", (datetime.now().isoformat(), user_id))

    def setup_totp(self, user_id: int) -> dict:
        """Generate TOTP Secret and QR Code for Google Authenticator / Microsoft Authenticator."""
        user = self.get_user_by_id(user_id)
        if not user:
            raise ValueError("Utilisateur introuvable")

        secret = user["totp_secret"] or pyotp.random_base32()
        if not user["totp_secret"]:
            with db.get_connection() as conn:
                conn.execute("UPDATE users SET totp_secret = ? WHERE id = ?;", (secret, user_id))

        totp = pyotp.TOTP(secret)
        uri = totp.provisioning_uri(name=user["username"], issuer_name=settings.APP_TITLE)
        
        from app.qr_generator import generate_qr_code_base64
        qr_b64 = generate_qr_code_base64(uri)

        return {
            "secret": secret,
            "manual_code": secret,
            "otpauth_uri": uri,
            "qr_code_base64": qr_b64
        }

# Global service instance
auth_service = AuthService()
