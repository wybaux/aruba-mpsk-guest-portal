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

    def delete_profile(self, profile_id: str) -> bool:
        pid = profile_id.lower().strip()
        if pid in self._profiles and len(self._profiles) > 1:
            was_default = self._profiles[pid].is_default
            removed = self._profiles.pop(pid)
            if was_default and self._profiles:
                next(iter(self._profiles.values())).is_default = True
            self._save()
            logger.info(f"Deleted profile '{pid}'")
            return True
        return False

    def sync_role_to_aruba(self, profile: AccessProfileInfo) -> bool:
        """Sync access-rule bandwidth limit and VLAN to Aruba Instant AP Virtual Controller."""
        if settings.ARUBA_MODE.lower() != "instant" or not settings.ARUBA_INSTANT_HOST:
            return True

        try:
            import paramiko
            import urllib.parse
            import time

            parsed = urllib.parse.urlparse(settings.ARUBA_INSTANT_HOST)
            ip = parsed.hostname or settings.ARUBA_INSTANT_HOST.replace("https://", "").replace("http://", "").split(":")[0]

            role_name = profile.get_role_name()
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                ip, port=22,
                username=settings.ARUBA_INSTANT_USERNAME or "admin",
                password=settings.ARUBA_INSTANT_PASSWORD or "",
                timeout=10, look_for_keys=False, allow_agent=False
            )
            channel = client.invoke_shell()
            time.sleep(1)
            while channel.recv_ready():
                channel.recv(4096)

            cmds = [
                "conf t",
                f"wlan access-rule {role_name}",
                f"vlan {profile.vlan_id}"
            ]

            if profile.bandwidth_limit_mbps and profile.bandwidth_limit_mbps > 0:
                kbps = int(profile.bandwidth_limit_mbps * 1000)
                cmds.append(f"bandwidth-limit peruser downstream {kbps}")
                cmds.append(f"bandwidth-limit peruser upstream {kbps}")
            else:
                cmds.append("no bandwidth-limit peruser downstream")
                cmds.append("no bandwidth-limit peruser upstream")

            cmds.append("rule any any match any any any permit")
            cmds.extend(["exit", "exit", "commit apply"])

            for c in cmds:
                channel.send(c + "\n")
                time.sleep(0.5 if "commit" not in c else 2.5)

            channel.close()
            client.close()
            logger.info(f"[Aruba AP] Synced role '{role_name}' (BW: {profile.bandwidth_limit_mbps} Mbps, VLAN: {profile.vlan_id}) to {ip}")
            return True
        except Exception as e:
            logger.warning(f"[Aruba AP] Failed to sync role '{profile.name}' to AP: {e}")
            return False

# Global instance
profile_manager = ProfileManager()
