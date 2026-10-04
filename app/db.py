import sqlite3
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from contextlib import contextmanager

logger = logging.getLogger("wifi_guest.db")

DEFAULT_DB_PATH = "wifi_guest.db"

class Database:
    """Robust SQLite Database layer for persistent storage, user management, and legal audit logs."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_db()
        self._auto_migrate_from_json()

    @contextmanager
    def get_connection(self):
        """Context manager yielding a thread-safe SQLite connection with dictionary row access."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            # Enable WAL mode for high concurrency
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Initialize database schema with tables and indexes."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. Users Table (RBAC & MFA)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT,
                full_name TEXT,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'operator',
                is_active INTEGER NOT NULL DEFAULT 1,
                mfa_enabled INTEGER NOT NULL DEFAULT 0,
                mfa_type TEXT NOT NULL DEFAULT 'totp',
                totp_secret TEXT,
                created_at TEXT NOT NULL,
                last_login TEXT
            );
            """)

            # 2. Guest Passes Table (MPSK Passes)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS guest_passes (
                id TEXT PRIMARY KEY,
                guest_name TEXT NOT NULL,
                ssid TEXT NOT NULL,
                password TEXT NOT NULL,
                vlan_id INTEGER DEFAULT 190,
                profile TEXT NOT NULL,
                duration_hours REAL NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                note TEXT,
                client_ip TEXT,
                user_agent TEXT,
                terms_accepted INTEGER NOT NULL DEFAULT 1,
                guest_email TEXT,
                guest_phone TEXT,
                sponsor_name TEXT,
                sponsor_email TEXT,
                otp_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """)

            # 3. Audit Events Table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                target TEXT,
                ip_address TEXT,
                details TEXT
            );
            """)

            # 4. System Settings Table (Dynamic Configuration)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)

            # 5. Banned MACs Table (Live Blacklist)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS banned_macs (
                mac TEXT PRIMARY KEY,
                ip TEXT,
                guest_name TEXT,
                reason TEXT,
                banned_by TEXT,
                banned_at TEXT NOT NULL
            );
            """)

            # Migration: Ensure expiry_alert_sent exists in guest_passes
            try:
                cursor.execute("ALTER TABLE guest_passes ADD COLUMN expiry_alert_sent INTEGER DEFAULT 0;")
            except Exception:
                pass

            # Indexes for high performance
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_passes_expires ON guest_passes(expires_at);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_passes_active ON guest_passes(is_active);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(timestamp);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_banned_macs ON banned_macs(mac);")

        logger.info(f"Database initialized successfully at {self.db_path}")

    def _auto_migrate_from_json(self):
        """Automatically import passes from legacy active_passes.json if database is fresh."""
        legacy_json = "active_passes.json"
        if not os.path.exists(legacy_json):
            return

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM guest_passes;")
            count = cursor.fetchone()[0]
            if count > 0:
                return  # Database already has data

            try:
                with open(legacy_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    imported = 0
                    for gid, p in data.items():
                        cursor.execute("""
                        INSERT OR IGNORE INTO guest_passes (
                            id, guest_name, ssid, password, vlan_id, profile, duration_hours,
                            is_active, note, client_ip, user_agent, terms_accepted,
                            guest_email, guest_phone, sponsor_name, sponsor_email, otp_verified,
                            created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """, (
                            p.get("id", gid),
                            p.get("guest_name", "Invité"),
                            p.get("ssid", "Wi-Fi"),
                            p.get("password", ""),
                            p.get("vlan_id", 190),
                            str(p.get("profile", "standard")),
                            float(p.get("duration_hours", 2)),
                            1 if p.get("is_active", True) else 0,
                            p.get("note"),
                            p.get("client_ip"),
                            p.get("user_agent"),
                            1 if p.get("terms_accepted", True) else 0,
                            p.get("guest_email"),
                            p.get("guest_phone"),
                            p.get("sponsor_name"),
                            p.get("sponsor_email"),
                            1 if p.get("otp_verified") else 0,
                            p.get("created_at", datetime.now().isoformat()),
                            p.get("expires_at", datetime.now().isoformat())
                        ))
                        imported += 1
                logger.info(f"[DB MIGRATION] Imported {imported} passes from {legacy_json} into SQLite")
            except Exception as e:
                logger.warning(f"Failed to auto-migrate from {legacy_json}: {e}")

    # -------------------------------------------------------------------------
    # Guest Passes Operations
    # -------------------------------------------------------------------------

    def save_pass(self, pass_data: dict):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT OR REPLACE INTO guest_passes (
                id, guest_name, ssid, password, vlan_id, profile, duration_hours,
                is_active, note, client_ip, user_agent, terms_accepted,
                guest_email, guest_phone, sponsor_name, sponsor_email, otp_verified,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                pass_data["id"],
                pass_data["guest_name"],
                pass_data["ssid"],
                pass_data["password"],
                pass_data.get("vlan_id", 190),
                pass_data["profile"],
                float(pass_data["duration_hours"]),
                1 if pass_data.get("is_active", True) else 0,
                pass_data.get("note"),
                pass_data.get("client_ip"),
                pass_data.get("user_agent"),
                1 if pass_data.get("terms_accepted", True) else 0,
                pass_data.get("guest_email"),
                pass_data.get("guest_phone"),
                pass_data.get("sponsor_name"),
                pass_data.get("sponsor_email"),
                1 if pass_data.get("otp_verified") else 0,
                pass_data["created_at"],
                pass_data["expires_at"]
            ))

    def get_pass(self, guest_id: str) -> Optional[dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM guest_passes WHERE id = ?;", (guest_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_passes(self) -> List[dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM guest_passes ORDER BY created_at DESC;")
            return [dict(row) for row in cursor.fetchall()]

    def get_active_passes(self) -> List[dict]:
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM guest_passes 
            WHERE is_active = 1 AND expires_at > ? 
            ORDER BY created_at DESC;
            """, (now_str,))
            return [dict(row) for row in cursor.fetchall()]

    def revoke_pass(self, guest_id: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE guest_passes SET is_active = 0 WHERE id = ?;", (guest_id,))
            return cursor.rowcount > 0

    def get_expired_active_passes(self) -> List[dict]:
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM guest_passes 
            WHERE is_active = 1 AND expires_at <= ?;
            """, (now_str,))
            return [dict(row) for row in cursor.fetchall()]

    # -------------------------------------------------------------------------
    # Audit Events Operations
    # -------------------------------------------------------------------------

    def log_audit_event(self, event_type: str, actor: str, target: Optional[str] = None, ip_address: Optional[str] = None, details: Optional[str] = None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO audit_events (timestamp, event_type, actor, target, ip_address, details)
            VALUES (?, ?, ?, ?, ?, ?);
            """, (
                datetime.now().isoformat(),
                event_type,
                actor,
                target,
                ip_address,
                details
            ))

    def get_recent_audit_events(self, limit: int = 50) -> List[dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM audit_events ORDER BY timestamp DESC LIMIT ?;", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    # -------------------------------------------------------------------------
    # System Metrics & Health
    # -------------------------------------------------------------------------

    def get_metrics(self) -> dict:
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM guest_passes;")
            total_passes = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM guest_passes WHERE is_active = 1 AND expires_at > ?;", (now_str,))
            active_passes = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM users;")
            total_users = cursor.fetchone()[0]

            db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

            return {
                "total_passes": total_passes,
                "active_passes": active_passes,
                "revoked_or_expired_passes": total_passes - active_passes,
                "total_users": total_users,
                "db_size_bytes": db_size,
                "db_file": self.db_path
            }

    # -------------------------------------------------------------------------
    # System Settings Management (Key-Value Dynamic Configuration)
    # -------------------------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Retrieve a system setting by key."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM system_settings WHERE key = ?;", (key,))
            row = cursor.fetchone()
            if not row:
                return default
            val = row["value"]
            try:
                return json.loads(val)
            except Exception:
                return val

    def set_setting(self, key: str, value: Any):
        """Save or update a system setting by key."""
        val_str = json.dumps(value) if not isinstance(value, str) else value
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            conn.execute("""
            INSERT INTO system_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
            """, (key, val_str, now_str))

    def get_all_settings(self) -> Dict[str, Any]:
        """Retrieve all system settings as a dictionary."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM system_settings;")
            rows = cursor.fetchall()
            res = {}
            for r in rows:
                k = r["key"]
                v = r["value"]
                try:
                    res[k] = json.loads(v)
                except Exception:
                    res[k] = v
            return res

    def get_duration_presets(self) -> List[int]:
        """Retrieve the 5 configured duration presets in hours (defaults to [1, 2, 4, 8, 24])."""
        raw = self.get_setting("duration_presets", [1, 2, 4, 8, 24])
        durations = []
        if isinstance(raw, list):
            for x in raw:
                try:
                    val = int(x)
                    if 0 < val <= 720:
                        durations.append(val)
                except (ValueError, TypeError):
                    pass
        elif isinstance(raw, str):
            try:
                if raw.strip().startswith("["):
                    parsed = json.loads(raw)
                    durations = [int(x) for x in parsed if 0 < int(x) <= 720]
                else:
                    durations = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit() and 0 < int(x.strip()) <= 720]
            except Exception:
                durations = []

        if len(durations) != 5:
            return [1, 2, 4, 8, 24]
        return sorted(durations)

    def set_duration_presets(self, presets: List[int]) -> List[int]:
        """Validate, sort and persist the 5 duration presets."""
        if not presets or len(presets) != 5:
            raise ValueError("Exactement 5 durées doivent être spécifiées.")
        
        cleaned = []
        for p in presets:
            try:
                val = int(p)
                if val <= 0 or val > 720:
                    raise ValueError(f"Durée invalide : {val}h. Doit être comprise entre 1 et 720 heures.")
                cleaned.append(val)
            except (ValueError, TypeError):
                raise ValueError("Chaque palier de durée doit être un nombre d'heures entier valide.")

        cleaned = sorted(cleaned)
        self.set_setting("duration_presets", cleaned)
        return cleaned

    # -------------------------------------------------------------------------
    # Expiry Alerts & Pass Extension Operations
    # -------------------------------------------------------------------------

    def get_passes_needing_expiry_alert(self, threshold_minutes: int = 15) -> List[dict]:
        """Find active passes expiring within threshold_minutes that haven't received an alert yet."""
        now = datetime.now()
        threshold_time = now + timedelta(minutes=threshold_minutes)
        now_str = now.isoformat()
        threshold_str = threshold_time.isoformat()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM guest_passes 
            WHERE is_active = 1 
              AND (expiry_alert_sent IS NULL OR expiry_alert_sent = 0)
              AND expires_at > ? 
              AND expires_at <= ?
              AND guest_email IS NOT NULL 
              AND guest_email != '';
            """, (now_str, threshold_str))
            return [dict(row) for row in cursor.fetchall()]

    def mark_expiry_alert_sent(self, guest_id: str):
        """Mark pass as having sent its impending expiration alert."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE guest_passes SET expiry_alert_sent = 1 WHERE id = ?;", (guest_id,))

    def extend_pass(self, guest_id: str, additional_hours: float) -> Optional[dict]:
        """Extend expiration date of a guest pass by additional_hours."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM guest_passes WHERE id = ?;", (guest_id,))
            row = cursor.fetchone()
            if not row:
                return None
            p = dict(row)
            try:
                current_exp = datetime.fromisoformat(p["expires_at"])
            except Exception:
                current_exp = datetime.now()

            base_time = max(datetime.now(), current_exp)
            new_exp = base_time + timedelta(hours=additional_hours)
            new_exp_str = new_exp.isoformat()
            new_duration = float(p.get("duration_hours", 0)) + float(additional_hours)

            cursor.execute("""
            UPDATE guest_passes 
            SET expires_at = ?, duration_hours = ?, is_active = 1, expiry_alert_sent = 0 
            WHERE id = ?;
            """, (new_exp_str, new_duration, guest_id))

            p["expires_at"] = new_exp_str
            p["duration_hours"] = new_duration
            p["is_active"] = 1
            p["expiry_alert_sent"] = 0
            return p

    # -------------------------------------------------------------------------
    # MAC Ban & Blacklist Operations
    # -------------------------------------------------------------------------

    def ban_mac(self, mac: str, reason: str = "", actor: str = "admin", ip: Optional[str] = None, guest_name: Optional[str] = None) -> dict:
        """Add MAC address to live blacklist."""
        clean_mac = mac.strip().upper()
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            conn.execute("""
            INSERT INTO banned_macs (mac, ip, guest_name, reason, banned_by, banned_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET 
                reason = excluded.reason, 
                banned_by = excluded.banned_by, 
                banned_at = excluded.banned_at;
            """, (clean_mac, ip, guest_name, reason, actor, now_str))

        self.log_audit_event("MAC_BANNED", actor=actor, target=clean_mac, ip_address=ip, details=f"Raison: {reason or 'Non spécifiée'}")
        return {
            "mac": clean_mac,
            "ip": ip,
            "guest_name": guest_name,
            "reason": reason,
            "banned_by": actor,
            "banned_at": now_str
        }

    def unban_mac(self, mac: str, actor: str = "admin") -> bool:
        """Remove MAC address from blacklist."""
        clean_mac = mac.strip().upper()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM banned_macs WHERE mac = ?;", (clean_mac,))
            count = cursor.rowcount

        if count > 0:
            self.log_audit_event("MAC_UNBANNED", actor=actor, target=clean_mac, details="Débannissement d'adresse MAC")
            return True
        return False

    def is_mac_banned(self, mac: str) -> bool:
        """Check if MAC address is currently blacklisted."""
        if not mac:
            return False
        clean_mac = mac.strip().upper()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM banned_macs WHERE mac = ?;", (clean_mac,))
            return cursor.fetchone() is not None

    def list_banned_macs(self) -> List[dict]:
        """List all currently banned MAC addresses."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM banned_macs ORDER BY banned_at DESC;")
            return [dict(row) for row in cursor.fetchall()]

    # -------------------------------------------------------------------------
    # Branding & Customization Operations
    # -------------------------------------------------------------------------

    def get_branding(self) -> dict:
        """Retrieve dynamic branding settings with fallbacks."""
        primary_col = self.get_setting("branding_primary_color") or self.get_setting("branding_accent_color", "#111827")
        charter = self.get_setting("branding_charter_text") or self.get_setting("branding_custom_charter", "")
        return {
            "company_name": self.get_setting("branding_company_name", "WIFI Guest"),
            "portal_title": self.get_setting("branding_portal_title", "Wi-Fi Invités"),
            "portal_subtitle": self.get_setting("branding_portal_subtitle", "Connexion Wi-Fi simplifiée et sécurisée via clé unique (MPSK)"),
            "logo_url": self.get_setting("branding_logo_url", ""),
            "primary_color": primary_col,
            "accent_color": primary_col,
            "charter_text": charter,
            "custom_charter": charter,
            "footer_text": self.get_setting("branding_footer_text", "")
        }

    def set_branding(self, branding: dict):
        """Save dynamic branding settings."""
        if "company_name" in branding and branding["company_name"] is not None:
            self.set_setting("branding_company_name", str(branding["company_name"]).strip())
        if "portal_title" in branding and branding["portal_title"] is not None:
            self.set_setting("branding_portal_title", str(branding["portal_title"]).strip())
        if "portal_subtitle" in branding and branding["portal_subtitle"] is not None:
            self.set_setting("branding_portal_subtitle", str(branding["portal_subtitle"]).strip())
        if "logo_url" in branding and branding["logo_url"] is not None:
            self.set_setting("branding_logo_url", str(branding["logo_url"]).strip())
        if "primary_color" in branding and branding["primary_color"] is not None:
            col = str(branding["primary_color"]).strip()
            self.set_setting("branding_primary_color", col)
            self.set_setting("branding_accent_color", col)
        elif "accent_color" in branding and branding["accent_color"] is not None:
            col = str(branding["accent_color"]).strip()
            self.set_setting("branding_primary_color", col)
            self.set_setting("branding_accent_color", col)
        if "charter_text" in branding and branding["charter_text"] is not None:
            txt = str(branding["charter_text"]).strip()
            self.set_setting("branding_charter_text", txt)
            self.set_setting("branding_custom_charter", txt)
        elif "custom_charter" in branding and branding["custom_charter"] is not None:
            txt = str(branding["custom_charter"]).strip()
            self.set_setting("branding_charter_text", txt)
            self.set_setting("branding_custom_charter", txt)
        if "footer_text" in branding and branding["footer_text"] is not None:
            self.set_setting("branding_footer_text", str(branding["footer_text"]).strip())

# Global DB instance
db = Database()


