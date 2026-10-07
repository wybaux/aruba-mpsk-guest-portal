import os
import json
import logging
from typing import Dict, List, Optional
from pydantic import BaseModel
from app.config import settings

logger = logging.getLogger("wifi_guest.profiles")

class AccessProfileInfo(BaseModel):
    id: str
    name: str
    description: str
    vlan_id: int = 190
    bandwidth_limit_mbps: Optional[int] = None
    role_name: Optional[str] = None
    is_default: bool = False
    requires_password: bool = False
    access_password: Optional[str] = None

    def get_role_name(self) -> str:
        if self.role_name and self.role_name.strip():
            return self.role_name.strip()
        # Clean slug to PascalCase for Aruba Role
        clean_id = "".join(part.capitalize() for part in self.id.replace("-", "_").split("_"))
        return f"Guest-{clean_id}"

DEFAULT_PROFILES = {
    "standard": AccessProfileInfo(
        id="standard",
        name="Invité Standard",
        description="Usage web, messagerie et réseaux sociaux",
        vlan_id=190,
        bandwidth_limit_mbps=10,
        role_name="Guest-Standard",
        is_default=True,
        requires_password=False,
        access_password=None
    ),
    "streaming": AccessProfileInfo(
        id="streaming",
        name="Streaming & Télétravail",
        description="Haute vitesse et faible latence pour la vidéo",
        vlan_id=190,
        bandwidth_limit_mbps=50,
        role_name="Guest-Streaming",
        is_default=False,
        requires_password=True,
        access_password=None
    ),
    "vip": AccessProfileInfo(
        id="vip",
        name="VIP & Illimité",
        description="Bande passante maximale sans aucune restriction",
        vlan_id=190,
        bandwidth_limit_mbps=None,
        role_name="Guest-VIP",
        is_default=False,
        requires_password=True,
        access_password=None
    )
}

class ProfileManager:
    """Manages Wi-Fi access profiles with persistence in profiles.json and dynamic AP sync."""

    def __init__(self, filepath: str = "profiles.json"):
        self.filepath = filepath
        self._profiles: Dict[str, AccessProfileInfo] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        self._profiles[k] = AccessProfileInfo(**v)
                
                # Ensure at least one profile is marked as default
                if not any(p.is_default for p in self._profiles.values()):
                    if "standard" in self._profiles:
                        self._profiles["standard"].is_default = True
                    elif self._profiles:
                        next(iter(self._profiles.values())).is_default = True
                    self._save()

                logger.info(f"Loaded {len(self._profiles)} profiles from {self.filepath}")
                return
            except Exception as e:
                logger.error(f"Error loading {self.filepath}, falling back to defaults: {e}")

        # Fallback to defaults
        self._profiles = {k: v.model_copy() for k, v in DEFAULT_PROFILES.items()}
        self._save()

    def _save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                raw = {k: v.model_dump() for k, v in self._profiles.items()}
                json.dump(raw, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save {self.filepath}: {e}")

    def get_all(self) -> Dict[str, AccessProfileInfo]:
        return self._profiles

    def get_default(self) -> AccessProfileInfo:
        for p in self._profiles.values():
            if p.is_default:
                return p
        return self._profiles.get("standard", DEFAULT_PROFILES["standard"])

    def get(self, profile_id: str) -> Optional[AccessProfileInfo]:
        # Case insensitive lookup
        pid = profile_id.lower().strip()
        if pid in self._profiles:
            return self._profiles[pid]
        for k, v in self._profiles.items():
            if k.lower() == pid:
                return v
        return self.get_default()

    def verify_access(self, profile_id: str, password: Optional[str]) -> bool:
        """Verify whether a user or guest is authorized to select this profile."""
        prof = self.get(profile_id)
        if not prof:
            return False
        # Default profile or unprotected profiles are open to all
        if prof.is_default or not prof.requires_password:
            return True
        if not password:
            return False
        # If a specific profile access_password is set, match it
        if prof.access_password and password.strip() == prof.access_password.strip():
            return True
        # Master Admin Password always authorizes any profile
        if settings.ADMIN_PASSWORD and password.strip() == settings.ADMIN_PASSWORD.strip():
            return True
        # Check active database admin user password
        try:
            from app.auth_service import auth_service
            admin_u = auth_service.get_user_by_username("admin")
            if admin_u and auth_service.verify_password(password.strip(), admin_u["password_hash"], admin_u["password_salt"]):
                return True
        except Exception:
            pass
        return False

    def save_profile(self, profile: AccessProfileInfo, sync_to_ap: bool = True) -> AccessProfileInfo:
        # Standardize ID
        clean_id = profile.id.lower().strip().replace(" ", "_")
        profile.id = clean_id
        if not profile.role_name:
            profile.role_name = profile.get_role_name()

        # If this profile is default, it cannot require a password and unsets default on others
        if profile.is_default:
            profile.requires_password = False
            profile.access_password = None
            for p in self._profiles.values():
                if p.id != clean_id:
                    p.is_default = False

        self._profiles[clean_id] = profile

        # Ensure at least one profile remains default
        if not any(p.is_default for p in self._profiles.values()):
            if "standard" in self._profiles:
                self._profiles["standard"].is_default = True
            elif self._profiles:
                next(iter(self._profiles.values())).is_default = True

        self._save()
        logger.info(f"Saved profile '{clean_id}' ({profile.name}, default={profile.is_default}, protected={profile.requires_password})")

        if sync_to_ap:
            self.sync_role_to_aruba(profile)

        return profile

    def get_by_role_name(self, role_name: str) -> Optional[AccessProfileInfo]:
        r_clean = role_name.strip().lower()
        for p in self._profiles.values():
            if p.get_role_name().lower() == r_clean or (p.role_name and p.role_name.strip().lower() == r_clean):
                return p
        return None

    def delete_profile(self, profile_id: str, sync_to_ap: bool = True) -> bool:
        pid = profile_id.lower().strip()
        if pid in self._profiles and len(self._profiles) > 1:
            was_default = self._profiles[pid].is_default
            prof_to_delete = self._profiles[pid]
            role_to_delete = prof_to_delete.get_role_name()
            removed = self._profiles.pop(pid)
            if was_default and self._profiles:
                next(iter(self._profiles.values())).is_default = True
            self._save()
            logger.info(f"Deleted profile '{pid}'")
            if sync_to_ap and role_to_delete:
                self.delete_role_from_aruba(role_to_delete)
            return True
        return False

    def sync_role_to_aruba(self, profile: AccessProfileInfo) -> bool:
        """Sync access-rule bandwidth limit and VLAN to Aruba Instant AP Virtual Controller."""
        try:
            from app.aruba_client import get_aruba_config, _ssh_connect_iap
            import urllib.parse
            import time

            cfg = get_aruba_config()
            if (cfg.get("mode") or "").lower() != "instant" or not cfg.get("host"):
                return True

            parsed = urllib.parse.urlparse(cfg["host"])
            ip = parsed.hostname or cfg["host"].replace("https://", "").replace("http://", "").split(":")[0].strip()
            user = cfg.get("username") or "admin"
            pwd = cfg.get("password") or ""

            role_name = profile.get_role_name()
            client = _ssh_connect_iap(ip, user, pwd, port=22, timeout=10)
            channel = client.invoke_shell()
            channel.send("\n")

            start_t = time.time()
            prompt_buf = ""
            while not any(p in prompt_buf for p in ("#", ">")) and (time.time() - start_t) < 5:
                if channel.recv_ready():
                    prompt_buf += channel.recv(4096).decode("utf-8", errors="ignore")
                time.sleep(0.04)

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

            _send_cmd("conf t")
            _send_cmd(f"wlan access-rule {role_name}")

            if profile.vlan_id and profile.vlan_id > 0:
                _send_cmd(f"vlan {profile.vlan_id}")

            if profile.bandwidth_limit_mbps and profile.bandwidth_limit_mbps > 0:
                kbps = int(profile.bandwidth_limit_mbps * 1000)
                _send_cmd(f"bandwidth-limit peruser downstream {kbps}")
                _send_cmd(f"bandwidth-limit peruser upstream {kbps}")
            else:
                _send_cmd("no bandwidth-limit peruser downstream")
                _send_cmd("no bandwidth-limit peruser upstream")

            _send_cmd("rule any any match any any any permit")
            _send_cmd("end")
            out_commit = _send_cmd("commit apply", timeout=8.0)

            channel.close()
            client.close()
            elapsed = time.time() - start_t
            logger.info(f"[Aruba AP] Successfully synced role '{role_name}' (BW: {profile.bandwidth_limit_mbps} Mbps, VLAN: {profile.vlan_id}) to {ip} in {elapsed:.2f}s")
            return True
        except Exception as e:
            logger.warning(f"[Aruba AP] Failed to sync role '{profile.name}' to AP: {e}")
            return False

    def delete_role_from_aruba(self, role_name: str) -> bool:
        """Delete access-rule from Aruba Instant AP Virtual Controller."""
        try:
            from app.aruba_client import get_aruba_config, _ssh_connect_iap
            import urllib.parse
            import time

            cfg = get_aruba_config()
            if (cfg.get("mode") or "").lower() != "instant" or not cfg.get("host"):
                return True

            parsed = urllib.parse.urlparse(cfg["host"])
            ip = parsed.hostname or cfg["host"].replace("https://", "").replace("http://", "").split(":")[0].strip()
            user = cfg.get("username") or "admin"
            pwd = cfg.get("password") or ""

            client = _ssh_connect_iap(ip, user, pwd, port=22, timeout=10)
            channel = client.invoke_shell()
            channel.send("\n")

            start_t = time.time()
            prompt_buf = ""
            while not any(p in prompt_buf for p in ("#", ">")) and (time.time() - start_t) < 5:
                if channel.recv_ready():
                    prompt_buf += channel.recv(4096).decode("utf-8", errors="ignore")
                time.sleep(0.04)

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

            _send_cmd("conf t")
            _send_cmd(f"no wlan access-rule {role_name}")
            _send_cmd("end")
            _send_cmd("commit apply", timeout=8.0)

            channel.close()
            client.close()
            logger.info(f"[Aruba AP] Deleted role '{role_name}' from VC {ip}")
            return True
        except Exception as e:
            logger.warning(f"[Aruba AP] Failed to delete role '{role_name}' from AP: {e}")
            return False

# Global instance
profile_manager = ProfileManager()
