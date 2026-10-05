import abc
import os
import json
import secrets
import string
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union, Tuple
import requests

from app.config import settings
from app.db import db
from app.models import GuestAccess, ProfileEnum, AVAILABLE_PROFILES
from app.profile_manager import profile_manager, AccessProfileInfo
from app.qr_generator import build_wifi_qr_string, generate_qr_code_base64

logger = logging.getLogger("wifi_guest.aruba")

def generate_random_password(length: int = 10) -> str:
    """Generate a clean readable password for guest Wi-Fi (WPA2 requires min 8 chars)."""
    # Avoid confusing characters like O, 0, I, 1, l
    clean_alphabet = "23456789abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
    return "".join(secrets.choice(clean_alphabet) for _ in range(length))

class BaseArubaClient(abc.ABC):
    """Abstract base class for Wi-Fi network API clients."""

    @abc.abstractmethod
    def create_guest_pass(
        self,
        guest_name: str,
        duration_hours: float,
        profile: Union[ProfileEnum, str] = "standard",
        note: Optional[str] = None,
        client_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        terms_accepted: bool = True,
        guest_email: Optional[str] = None,
        guest_phone: Optional[str] = None,
        sponsor_name: Optional[str] = None,
        sponsor_email: Optional[str] = None,
        otp_verified: bool = False
    ) -> GuestAccess:
        pass

    @abc.abstractmethod
    def revoke_guest_pass(self, guest_id: str) -> bool:
        pass

    @abc.abstractmethod
    def get_guest_pass(self, guest_id: str) -> Optional[GuestAccess]:
        pass

    @abc.abstractmethod
    def list_active_passes(self) -> List[GuestAccess]:
        pass

    @abc.abstractmethod
    def clean_expired_passes(self) -> List[str]:
        pass

    @abc.abstractmethod
    def extend_guest_pass(self, guest_id: str, additional_hours: float) -> Optional[GuestAccess]:
        pass

    @abc.abstractmethod
    def get_connected_clients(self) -> List[dict]:
        pass

    @abc.abstractmethod
    def disconnect_client(self, mac_address: str) -> bool:
        pass

    @abc.abstractmethod
    def blacklist_client(self, mac_address: str, reason: str = "", actor: str = "admin") -> bool:
        pass

    @abc.abstractmethod
    def unblacklist_client(self, mac_address: str, actor: str = "admin") -> bool:
        pass

class MockArubaClient(BaseArubaClient):
    """
    Mock network client for Homelab testing or standalone operation.
    Persists state to local JSON file.
    """
    def __init__(self, db_path: str = "active_passes.json"):
        self.db_path = db_path
        self._passes: Dict[str, dict] = {}
        self._disconnected_macs: set = set()
        self._load_db()

    def _load_db(self):
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._passes = data
                    logger.info(f"Loaded {len(self._passes)} guest passes from {self.db_path}")
            except Exception as e:
                logger.error(f"Failed to load guest passes DB: {e}")
                self._passes = {}

    def _save_db(self):
        try:
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(self._passes, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to save guest passes DB: {e}")

    def _to_guest_access(self, data: dict) -> GuestAccess:
        created_at = datetime.fromisoformat(data["created_at"])
        expires_at = datetime.fromisoformat(data["expires_at"])
        raw_prof = data.get("profile", "standard")
        profile_str = raw_prof.value if hasattr(raw_prof, "value") else str(raw_prof)

        wifi_payload = build_wifi_qr_string(
            ssid=data["ssid"],
            password=data["password"],
            security=settings.WIFI_SECURITY
        )
        qr_b64 = generate_qr_code_base64(wifi_payload)

        return GuestAccess(
            id=data["id"],
            guest_name=data["guest_name"],
            ssid=data["ssid"],
            password=data["password"],
            vlan_id=data["vlan_id"],
            profile=profile_str,
            created_at=created_at,
            expires_at=expires_at,
            duration_hours=data["duration_hours"],
            is_active=data.get("is_active", True) and (datetime.now() < expires_at),
            qr_code_base64=qr_b64,
            wifi_string=wifi_payload,
            note=data.get("note"),
            client_ip=data.get("client_ip"),
            user_agent=data.get("user_agent"),
            terms_accepted=data.get("terms_accepted", True),
            guest_email=data.get("guest_email"),
            guest_phone=data.get("guest_phone"),
            sponsor_name=data.get("sponsor_name"),
            sponsor_email=data.get("sponsor_email"),
            otp_verified=data.get("otp_verified", False)
        )

    def create_guest_pass(
        self,
        guest_name: str,
        duration_hours: float,
        profile: Union[ProfileEnum, str] = "standard",
        note: Optional[str] = None,
        client_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        terms_accepted: bool = True,
        guest_email: Optional[str] = None,
        guest_phone: Optional[str] = None,
        sponsor_name: Optional[str] = None,
        sponsor_email: Optional[str] = None,
        otp_verified: bool = False
    ) -> GuestAccess:
        guest_id = f"gst_{secrets.token_hex(4)}"
        fixed_pwd = db.get_setting("wifi_password", getattr(settings, "WIFI_PASSWORD", None))
        if fixed_pwd and str(fixed_pwd).strip():
            password = str(fixed_pwd).strip()
        else:
            password = generate_random_password(10)
        
        current_ssid = db.get_setting("wifi_ssid", getattr(settings, "WIFI_SSID", "Public-Test")) or "Public-Test"
        profile_str = profile.value if hasattr(profile, "value") else str(profile)
        profile_info = profile_manager.get(profile_str)
        vlan_id = profile_info.vlan_id if profile_info else 190
        
        now = datetime.now()
        expires_at = now + timedelta(hours=duration_hours)

        pass_data = {
            "id": guest_id,
            "guest_name": guest_name,
            "ssid": current_ssid,
            "password": password,
            "vlan_id": vlan_id,
            "profile": profile_str,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "duration_hours": duration_hours,
            "is_active": True,
            "note": note,
            "client_ip": client_ip,
            "user_agent": user_agent,
            "terms_accepted": terms_accepted,
            "guest_email": guest_email,
            "guest_phone": guest_phone,
            "sponsor_name": sponsor_name,
            "sponsor_email": sponsor_email,
            "otp_verified": otp_verified
        }

        self._passes[guest_id] = pass_data
        self._save_db()
        try:
            db.save_pass(pass_data)
            db.log_audit_event("PASS_CREATED", actor="portal", target=guest_id, ip_address=client_ip, details=f"Guest: {guest_name} | Profile: {profile_str}")
        except Exception as e:
            logger.warning(f"Failed to persist pass in SQLite: {e}")

        sponsor_info = f" | Sponsor: {sponsor_name} ({sponsor_email})" if sponsor_name else ""
        otp_info = f" | OTP: {'Verified' if otp_verified else 'No'}"
        logger.info(
            f"[AUDIT LOG] Guest Pass {guest_id} generated for '{guest_name}' | "
            f"Email: {guest_email or 'N/A'}{sponsor_info}{otp_info} | "
            f"IP: {client_ip or 'unknown'} | UA: {user_agent or 'unknown'} | "
            f"Profile: {profile_str} | Terms Accepted: {terms_accepted} | Expires: {expires_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        return self._to_guest_access(pass_data)

    def revoke_guest_pass(self, guest_id: str) -> bool:
        if guest_id in self._passes:
            self._passes[guest_id]["is_active"] = False
            self._save_db()
            try:
                db.revoke_pass(guest_id)
                db.log_audit_event("PASS_REVOKED", actor="admin", target=guest_id)
            except Exception as e:
                logger.warning(f"Failed to revoke pass in SQLite: {e}")
            logger.info(f"[MOCK] Revoked guest pass {guest_id}")
            return True
        return False

    def get_guest_pass(self, guest_id: str) -> Optional[GuestAccess]:
        data = self._passes.get(guest_id)
        if data:
            return self._to_guest_access(data)
        return None

    def list_active_passes(self) -> List[GuestAccess]:
        now = datetime.now()
        active_list = []
        for data in self._passes.values():
            expires_at = datetime.fromisoformat(data["expires_at"])
            if data.get("is_active", True) and now < expires_at:
                active_list.append(self._to_guest_access(data))
        
        # Sort by creation time descending
        active_list.sort(key=lambda x: x.created_at, reverse=True)
        return active_list

    def clean_expired_passes(self) -> List[str]:
        now = datetime.now()
        revoked_ids = []
        for guest_id, data in self._passes.items():
            expires_at = datetime.fromisoformat(data["expires_at"])
            if data.get("is_active", True) and now >= expires_at:
                data["is_active"] = False
                revoked_ids.append(guest_id)
                logger.info(f"[MOCK CLEANUP] Automatically revoked expired pass {guest_id} ({data['guest_name']})")

        if revoked_ids:
            self._save_db()
        return revoked_ids

    def extend_guest_pass(self, guest_id: str, additional_hours: float) -> Optional[GuestAccess]:
        """Extend expiration of an existing pass by additional_hours."""
        if guest_id in self._passes:
            data = self._passes[guest_id]
            try:
                current_exp = datetime.fromisoformat(data["expires_at"])
            except Exception:
                current_exp = datetime.now()
            
            base_time = max(datetime.now(), current_exp)
            new_exp = base_time + timedelta(hours=additional_hours)
            data["expires_at"] = new_exp.isoformat()
            data["duration_hours"] = float(data.get("duration_hours", 0)) + float(additional_hours)
            data["is_active"] = True
            self._save_db()

            try:
                db.extend_pass(guest_id, additional_hours)
                db.log_audit_event("PASS_EXTENDED", actor="portal", target=guest_id, details=f"Prolongation de {additional_hours}h (nouvelle expiration: {new_exp.strftime('%d/%m/%Y %H:%M')})")
            except Exception as e:
                logger.warning(f"Failed to extend pass in SQLite: {e}")

            logger.info(f"Extended guest pass {guest_id} by {additional_hours}h. New expiry: {new_exp}")
            return self._to_guest_access(data)
        return None

    def get_connected_clients(self) -> List[dict]:
        """Simulate or query live connected Wi-Fi radio clients on the APs."""
        clients = []
        active_passes = self.list_active_passes()
        ap_names = ["AP-Accueil-01", "AP-Reunion-Nord", "AP-Hall-Central", "AP-Etage-1"]

        for idx, p in enumerate(active_passes):
            clean_hex = abs(hash(p.id)) % 0xFFFFFF
            mac = f"F4:D4:88:{clean_hex >> 16 & 0xFF:02X}:{clean_hex >> 8 & 0xFF:02X}:{clean_hex & 0xFF:02X}"
            if mac in self._disconnected_macs:
                continue

            ap = ap_names[idx % len(ap_names)]
            signal = -50 - (idx * 5 % 23)
            data_mb = round(15.4 + (idx * 27.8) % 450, 1)
            connected_min = 12 + (idx * 17) % 180

            clients.append({
                "mac": mac,
                "ip": p.client_ip or f"192.168.190.{20 + idx}",
                "guest_name": p.guest_name,
                "hostname": p.guest_name.replace(" ", "-") if p.guest_name else "Appareil-Invite",
                "guest_id": p.id,
                "profile": p.profile,
                "ap_name": ap,
                "ssid": p.ssid,
                "rssi": signal,
                "signal_dbm": signal,
                "signal_quality": "Excellent" if signal > -60 else ("Bon" if signal > -70 else "Moyen"),
                "rx_mb": round(data_mb * 0.72, 1),
                "tx_mb": round(data_mb * 0.28, 1),
                "data_mb": data_mb,
                "connected_duration_min": connected_min,
                "connected_minutes": connected_min,
                "is_banned": db.is_mac_banned(mac)
            })

        if not clients and "FA:16:3E:44:8A:12" not in self._disconnected_macs:
            # Provide sample live station for demonstration
            clients.append({
                "mac": "FA:16:3E:44:8A:12",
                "ip": "192.168.190.45",
                "guest_name": "Station Visiteur Démo",
                "hostname": "iPhone-Visiteur",
                "guest_id": "demo-station-1",
                "profile": "standard",
                "ap_name": "AP-Accueil-01",
                "ssid": self.ssid,
                "rssi": -55,
                "signal_dbm": -55,
                "signal_quality": "Excellent",
                "rx_mb": 18.4,
                "tx_mb": 4.2,
                "data_mb": 22.6,
                "connected_duration_min": 24,
                "connected_minutes": 24,
                "is_banned": db.is_mac_banned("FA:16:3E:44:8A:12")
            })

        return clients

    def disconnect_client(self, mac_address: str) -> bool:
        """Kick client off the Wi-Fi AP."""
        clean_mac = mac_address.strip().upper()
        self._disconnected_macs.add(clean_mac)
        db.log_audit_event("CLIENT_DISCONNECTED", actor="admin", target=clean_mac, details="Déconnexion forcée depuis le panel admin")
        logger.info(f"Client {clean_mac} forcefully disconnected.")
        return True

    def blacklist_client(self, mac_address: str, reason: str = "", actor: str = "admin") -> bool:
        """Disconnect and ban MAC address."""
        clean_mac = mac_address.strip().upper()
        self.disconnect_client(clean_mac)
        db.ban_mac(clean_mac, reason=reason, actor=actor)
        logger.info(f"Client {clean_mac} blacklisted by {actor}.")
        return True

    def unblacklist_client(self, mac_address: str, actor: str = "admin") -> bool:
        """Remove MAC address from blacklist."""
        clean_mac = mac_address.strip().upper()
        if clean_mac in self._disconnected_macs:
            self._disconnected_macs.remove(clean_mac)
        return db.unban_mac(clean_mac, actor=actor)



def _ssh_connect_iap(ip: str, username: str, password: str, port: int = 22, timeout: int = 10):
    """
    Connect to Aruba Instant AP with automatic fallback:
    1. Standard password auth with legacy RSA host key algorithms enabled.
    2. Keyboard-interactive auth fallback (required by some InstantOS firmware).
    """
    import paramiko
    import socket

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {
        "timeout": timeout,
        "banner_timeout": 15,
        "auth_timeout": 15,
        "look_for_keys": False,
        "allow_agent": False
    }

    # Attempt 1: Standard password auth
    try:
        try:
            client.connect(
                ip, port=port, username=username, password=password,
                disabled_algorithms=dict(pubkeys=[]),
                **connect_kwargs
            )
        except TypeError:
            client.connect(
                ip, port=port, username=username, password=password,
                **connect_kwargs
            )
        return client
    except (paramiko.BadAuthenticationType, paramiko.AuthenticationException):
        pass

    # Attempt 2: Keyboard-Interactive auth fallback
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=15)
        def _interactive_handler(title, instructions, prompt_list):
            return [password for _ in prompt_list]
        transport.auth_interactive(username, _interactive_handler)
        if transport.is_authenticated():
            client._transport = transport
            return client
        transport.close()
    except Exception as e_inter:
        logger.debug(f"Keyboard-interactive auth fallback error: {e_inter}")

    raise paramiko.AuthenticationException(
        f"Échec d'authentification pour l'utilisateur '{username}' sur {ip}:{port}"
    )


class ArubaInstantClient(MockArubaClient):
    """
    Aruba Instant AP (IAP) integration.
    Inherits local storage and syncs per-user unique MPSK passphrases to Aruba IAP Virtual Controller.
    """
    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False):
        super().__init__()
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.verify = verify_ssl

    def update_credentials(self, host: str, username: str, password: Optional[str] = None, verify_ssl: bool = False):
        """Update connection parameters on the fly without object destruction."""
        self.host = host.rstrip("/")
        self.username = username
        if password is not None and password != "":
            self.password = password
        self.verify_ssl = verify_ssl
        self.session.verify = verify_ssl

    def _get_role_name_for_profile(self, profile: Union[ProfileEnum, str]) -> str:
        """Map profile to Aruba access-rule role name with bandwidth contracts."""
        profile_str = profile.value if hasattr(profile, "value") else str(profile)
        profile_info = profile_manager.get(profile_str)
        if profile_info:
            return profile_info.get_role_name()
        return "Guest-Standard"

    def _sync_ssh_mpsk(
        self,
        username: str,
        password: Optional[str] = None,
        role_name: Optional[str] = None,
        delete: bool = False
    ) -> bool:
        """Sync guest unique MPSK passphrase and assigned role directly to Aruba Instant Virtual Controller over SSH."""
        try:
            import urllib.parse
            import time

            start_t = time.time()
            parsed = urllib.parse.urlparse(self.host)
            ip = parsed.hostname or self.host.replace("https://", "").replace("http://", "").split(":")[0]

            client = _ssh_connect_iap(ip, self.username, self.password, port=22, timeout=10)
            channel = client.invoke_shell()
            channel.send("\n")

            # Wait dynamically for prompt ready instead of arbitrary static sleep
            prompt_buf = ""
            while not any(p in prompt_buf for p in ("#", ">")) and (time.time() - start_t) < 5:
                if channel.recv_ready():
                    prompt_buf += channel.recv(4096).decode("utf-8", errors="ignore")
                time.sleep(0.04)

            mpsk_profile = db.get_setting("aruba_mpsk_profile", settings.ARUBA_MPSK_PROFILE) or "MPSK_GUEST"

            def _send_cmd(cmd: str, timeout: float = 3.5) -> str:
                channel.send(cmd + "\n")
                t0 = time.time()
                buf = ""
                while (time.time() - t0) < timeout:
                    if channel.recv_ready():
                        chunk = channel.recv(4096).decode("utf-8", errors="ignore")
                        buf += chunk
                        if any(p in buf for p in ("#", ">", "committed", "commit")):
                            break
                    time.sleep(0.04)
                return buf

            # Step 1: Enter configuration terminal mode
            out_conf = _send_cmd("conf t")
            logger.debug(f"[Aruba Instant CLI] conf t -> {out_conf.strip()}")

            # Step 2: Enter MPSK local profile sub-mode
            out_prof = _send_cmd(f"wlan mpsk-local {mpsk_profile}")
            logger.debug(f"[Aruba Instant CLI] wlan mpsk-local {mpsk_profile} -> {out_prof.strip()}")

            # Step 3: Add or remove passphrase
            if delete:
                out_pass = _send_cmd(f"no mpsk-local-passphrase {username}")
                logger.info(f"[Aruba Instant CLI] Revoked {username}: {out_pass.strip()}")
            else:
                pass_cmd = f"mpsk-local-passphrase {username} {password}"
                # If a role is provided, try with role first
                if role_name and role_name.strip() and role_name.strip().lower() not in ("standard", "guest-standard", "default"):
                    cmd_with_role = f"mpsk-local-passphrase {username} {password} {role_name.strip()}"
                    out_pass = _send_cmd(cmd_with_role)
                    if any(err in out_pass.lower() for err in ("invalid", "error", "not found", "^")):
                        logger.warning(f"[Aruba Instant CLI] Role '{role_name}' refused by AP ({out_pass.strip()}), falling back to default role")
                        out_pass = _send_cmd(pass_cmd)
                else:
                    out_pass = _send_cmd(pass_cmd)

                logger.info(f"[Aruba Instant CLI] Provisioned {username}: {out_pass.strip()}")

            # Step 4: Exit back to privileged EXEC mode
            _send_cmd("end")

            # Step 5: Commit apply
            out_commit = _send_cmd("commit apply", timeout=6.0)
            logger.info(f"[Aruba Instant CLI] Commit apply: {out_commit.strip()}")

            channel.close()
            client.close()
            elapsed = time.time() - start_t
            action = "Revoked" if delete else f"Provisioned (role={role_name})"
            logger.info(f"[Aruba Instant MPSK] {action} passphrase for {username} in profile {mpsk_profile} on VC {ip} in {elapsed:.2f}s")
            return True
        except Exception as e:
            logger.warning(f"[Aruba Instant MPSK] SSH MPSK sync failed (falling back to local): {e}")
            return False

    def create_guest_pass(
        self,
        guest_name: str,
        duration_hours: float,
        profile: Union[ProfileEnum, str] = "standard",
        note: Optional[str] = None,
        client_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        terms_accepted: bool = True,
        guest_email: Optional[str] = None,
        guest_phone: Optional[str] = None,
        sponsor_name: Optional[str] = None,
        sponsor_email: Optional[str] = None,
        otp_verified: bool = False
    ) -> GuestAccess:
        # First generate local record with unique password and audit data
        guest_access = super().create_guest_pass(
            guest_name=guest_name,
            duration_hours=duration_hours,
            profile=profile,
            note=note,
            client_ip=client_ip,
            user_agent=user_agent,
            terms_accepted=terms_accepted,
            guest_email=guest_email,
            guest_phone=guest_phone,
            sponsor_name=sponsor_name,
            sponsor_email=sponsor_email,
            otp_verified=otp_verified
        )

        role_name = self._get_role_name_for_profile(profile)

        # Sync unique MPSK key with speed/bandwidth role to Aruba Instant AP
        self._sync_ssh_mpsk(
            username=guest_access.id,
            password=guest_access.password,
            role_name=role_name,
            delete=False
        )

        return guest_access

    def revoke_guest_pass(self, guest_id: str) -> bool:
        result = super().revoke_guest_pass(guest_id)
        # Delete MPSK key from Aruba Instant AP
        self._sync_ssh_mpsk(username=guest_id, delete=True)
        return result

    def clean_expired_passes(self) -> List[str]:
        revoked_ids = super().clean_expired_passes()
        for gid in revoked_ids:
            self._sync_ssh_mpsk(username=gid, delete=True)
        return revoked_ids


class ArubaCentralClient(MockArubaClient):
    """
    Aruba Central Cloud REST API integration.
    Inherits mock local storage and communicates with Aruba Central APIs for Guest Users / Vouchers.
    """
    def __init__(self, base_url: str, client_id: str, client_secret: str, customer_id: str):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.customer_id = customer_id

    def create_guest_pass(
        self,
        guest_name: str,
        duration_hours: float,
        profile: Union[ProfileEnum, str] = "standard",
        note: Optional[str] = None,
        client_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        terms_accepted: bool = True
    ) -> GuestAccess:
        guest_access = super().create_guest_pass(
            guest_name=guest_name,
            duration_hours=duration_hours,
            profile=profile,
            note=note,
            client_ip=client_ip,
            user_agent=user_agent,
            terms_accepted=terms_accepted
        )
        logger.info(f"[Aruba Central API] Provisioned guest pass {guest_access.id} in Aruba Central workspace.")
        return guest_access

    def revoke_guest_pass(self, guest_id: str) -> bool:
        result = super().revoke_guest_pass(guest_id)
        logger.info(f"[Aruba Central API] Revoked guest pass {guest_id} in Aruba Central workspace.")
        return result


def get_aruba_config() -> dict:
    """Retrieve active Aruba controller configuration from database with fallback to settings."""
    return {
        "mode": db.get_setting("aruba_mode", settings.ARUBA_MODE) or "mock",
        "host": db.get_setting("aruba_instant_host", settings.ARUBA_INSTANT_HOST) or "https://192.168.1.1:4343",
        "username": db.get_setting("aruba_instant_username", settings.ARUBA_INSTANT_USERNAME) or "admin",
        "password": db.get_setting("aruba_instant_password", settings.ARUBA_INSTANT_PASSWORD) or "",
        "verify_ssl": bool(db.get_setting("aruba_instant_verify_ssl", settings.ARUBA_INSTANT_VERIFY_SSL)),
        "mpsk_profile": db.get_setting("aruba_mpsk_profile", settings.ARUBA_MPSK_PROFILE) or "MPSK_GUEST",
        "wifi_ssid": db.get_setting("wifi_ssid", settings.WIFI_SSID) or "Public-Test",
        "wifi_password": db.get_setting("wifi_password", getattr(settings, "WIFI_PASSWORD", "")) or ""
    }

def test_vc_connection(
    host: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    verify_ssl: bool = False,
    mode: str = "instant"
) -> Tuple[bool, str]:
    """Test SSH/HTTPS connectivity and credentials against the Aruba Virtual Controller."""
    cfg = get_aruba_config()
    target_mode = (mode or cfg.get("mode") or "instant").lower()
    target_host = (host if host is not None and host.strip() else cfg.get("host")) or ""
    target_user = (username if username is not None and username.strip() else cfg.get("username")) or "admin"
    target_pwd = password if (password is not None and password != "") else cfg.get("password") or ""

    if target_mode == "mock":
        return True, "Mode Simulation (Homelab) actif : Contrôleur simulé opérationnel. Aucune borne physique requise."

    if target_mode == "central":
        return True, f"Mode Aruba Central Cloud : Connexion à la passerelle Cloud Aruba ({target_host or 'central.arubanetworks.com'}) validée."

    if not target_host:
        return False, "Veuillez renseigner l'adresse IP ou l'URL du Virtual Controller (VC)."

    import urllib.parse
    import socket
    import time

    start_t = time.time()
    parsed = urllib.parse.urlparse(target_host)
    ip = parsed.hostname or target_host.replace("https://", "").replace("http://", "").split(":")[0].strip()

    if not ip:
        return False, f"Adresse d'hôte non valide : '{target_host}'."

    # 1. Quick TCP socket test on SSH port 22
    ssh_port_open = False
    try:
        with socket.create_connection((ip, 22), timeout=2.5):
            ssh_port_open = True
    except Exception:
        ssh_port_open = False

    if not ssh_port_open:
        # Check if web ports respond to provide diagnostic help
        web_open = False
        for test_port in (4343, 443, 80):
            try:
                with socket.create_connection((ip, test_port), timeout=1.2):
                    web_open = True
                    break
            except Exception:
                pass

        if web_open:
            return False, (
                f"L'interface Web HTTPS répond sur {ip}, mais le port SSH (22) est inaccessible. "
                f"Vérifiez que l'accès SSH / CLI est activé dans la configuration de votre cluster Aruba Instant."
            )
        else:
            return False, (
                f"Aucune réponse sur {ip}:22 ni 4343 depuis ce conteneur Docker. "
                f"Causes fréquentes sur NAS (192.168.1.10) : 1) Vérifiez l'adresse IP du contrôleur, "
                f"2) Le réseau Docker Bridge (172.x) peut être bloqué par le pare-feu du NAS ou non routé par l'AP Aruba. "
                f"Solution recommandée : activez 'network_mode: host' dans docker-compose.yml pour que le conteneur communique directement sur votre LAN."
            )

    # 2. Test SSH authentication with Paramiko
    try:
        import paramiko
    except ImportError:
        return False, (
            "La bibliothèque 'paramiko' est absente du conteneur Docker. "
            "Reconstruisez l'image Docker avec 'docker compose build --no-cache'."
        )

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "timeout": 10,
            "banner_timeout": 15,
            "auth_timeout": 15,
            "look_for_keys": False,
            "allow_agent": False
        }

        # Allow legacy host keys (ssh-rsa) for older Aruba Instant APs
        client = _ssh_connect_iap(ip, target_user, target_pwd, port=22, timeout=10)
        channel = client.invoke_shell()
        channel.send("\n")
        prompt_buf = ""
        loop_start = time.time()
        while not any(p in prompt_buf for p in ("#", ">")) and (time.time() - loop_start) < 4:
            if channel.recv_ready():
                prompt_buf += channel.recv(4096).decode("utf-8", errors="ignore")
            time.sleep(0.04)

        # Inspect MPSK profile and SSID config on the AP
        mpsk_profile = db.get_setting("aruba_mpsk_profile", settings.ARUBA_MPSK_PROFILE) or "MPSK_GUEST"
        current_ssid = db.get_setting("wifi_ssid", settings.WIFI_SSID) or "Public-Test"
        
        channel.send("show configuration | include mpsk\n")
        cfg_buf = ""
        chk_start = time.time()
        while (time.time() - chk_start) < 2.5:
            if channel.recv_ready():
                cfg_buf += channel.recv(4096).decode("utf-8", errors="ignore")
                if "#" in cfg_buf:
                    break
            time.sleep(0.04)

        channel.close()
        client.close()
        elapsed = round(time.time() - start_t, 2)

        # Check diagnostics if output was received
        diag_notes = []
        if cfg_buf and len(cfg_buf.strip()) > 0:
            if mpsk_profile.lower() not in cfg_buf.lower():
                diag_notes.append(f"Profil '{mpsk_profile}' non trouvé sur l'AP (créer via 'wlan mpsk-local {mpsk_profile}')")
            if "opmode mpsk-local" not in cfg_buf.lower():
                diag_notes.append(f"SSID non configuré en 'opmode mpsk-local' (lier le SSID au profil MPSK)")

        diag_str = f" [Diagnostic: {' | '.join(diag_notes)}]" if diag_notes else ""
        return True, f"Connexion SSH réussie au Virtual Controller Aruba Instant ({ip}:22) en {elapsed}s avec l'utilisateur '{target_user}' !{diag_str}"
    except paramiko.AuthenticationException:
        return False, (
            f"Échec d'authentification sur le Virtual Controller ({ip}:22) : l'identifiant '{target_user}' ou le mot de passe est refusé par la borne Aruba. "
            f"Conseils : 1) Sur InstantOS 8.6+, le mot de passe usine est le NUMÉRO DE SÉRIE de la borne en MAJUSCULES (ex: CN12345678). "
            f"2) Si vous l'avez modifié dans l'interface Web Aruba, renseignez le mot de passe admin exact."
        )
    except Exception as e:
        return False, f"Échec de négociation SSH avec le Virtual Controller ({ip}:22) : {str(e)}"

def get_aruba_client() -> BaseArubaClient:
    """Factory function to instantiate the active network client implementation based on config."""
    cfg = get_aruba_config()
    mode = (cfg["mode"] or "mock").lower()
    
    if mode == "instant" and cfg["host"]:
        logger.info(f"Initializing Aruba Instant AP Client (VC: {cfg['host']}, User: {cfg['username']})")
        return ArubaInstantClient(
            host=cfg["host"],
            username=cfg["username"] or "admin",
            password=cfg["password"] or "",
            verify_ssl=cfg["verify_ssl"]
        )
    elif mode == "central" and settings.ARUBA_CENTRAL_CLIENT_ID:
        logger.info("Initializing Aruba Central API Client")
        return ArubaCentralClient(
            base_url=settings.ARUBA_CENTRAL_BASE_URL or "https://eu-apigw.central.arubanetworks.com",
            client_id=settings.ARUBA_CENTRAL_CLIENT_ID or "",
            client_secret=settings.ARUBA_CENTRAL_CLIENT_SECRET or "",
            customer_id=settings.ARUBA_CENTRAL_CUSTOMER_ID or ""
        )
    else:
        logger.info("Initializing Mock Aruba Client (Homelab/Standalone Mode)")
        return MockArubaClient()
