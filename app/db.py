import sqlite3
import os
import json
import logging
from datetime import datetime
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

            # Indexes for high performance
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_passes_expires ON guest_passes(expires_at);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_passes_active ON guest_passes(is_active);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(timestamp);")

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

# Global DB instance
db = Database()
