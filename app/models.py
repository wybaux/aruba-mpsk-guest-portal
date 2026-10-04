from datetime import datetime
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field

from app.profile_manager import profile_manager, AccessProfileInfo

class ProfileEnum(str, Enum):
    STANDARD = "standard"
    VIP = "vip"
    RESTRICTED = "restricted"
    STREAMING = "streaming"

# Backward compatibility reference
AVAILABLE_PROFILES = profile_manager.get_all()

class CreateGuestRequest(BaseModel):
    guest_name: str = Field(..., min_length=2, max_length=50, description="Nom de l'invité ou de l'événement")
    duration_hours: float = Field(..., gt=0, le=720, description="Durée de validité en heures (ex: 2, 8, 24, 168)")
    profile: str = Field(default="standard", description="Profil d'accès / VLAN")
    profile_password: Optional[str] = Field(default=None, description="Mot de passe d'accès pour profil protégé")
    terms_accepted: bool = Field(default=True, description="Acceptation de la charte d'utilisation du réseau")
    note: Optional[str] = Field(default=None, max_length=100, description="Note optionnelle")
    
    # Guest Contact & Verification
    guest_email: Optional[str] = Field(default=None, description="Adresse email de l'invité")
    guest_phone: Optional[str] = Field(default=None, description="Numéro de téléphone portable")
    otp_code: Optional[str] = Field(default=None, description="Code OTP de vérification d'identité")
    send_email_voucher: bool = Field(default=False, description="Envoyer le voucher Wi-Fi par email à l'invité")
    
    # Corporate Sponsorship (Parrainage)
    sponsor_name: Optional[str] = Field(default=None, description="Nom du collaborateur interne parrain")
    sponsor_email: Optional[str] = Field(default=None, description="Email professionnel du collaborateur parrain")
    send_sponsor_copy: bool = Field(default=False, description="Envoyer une copie du pass au parrain")

class GuestAccess(BaseModel):
    id: str
    guest_name: str
    ssid: str
    password: str
    vlan_id: int
    profile: str
    created_at: datetime
    expires_at: datetime
    duration_hours: float
    is_active: bool = True
    qr_code_base64: str
    wifi_string: str
    note: Optional[str] = None
    client_ip: Optional[str] = None
    user_agent: Optional[str] = None
    terms_accepted: bool = True
    
    # Contact, Sponsorship & Verification
    guest_email: Optional[str] = None
    guest_phone: Optional[str] = None
    sponsor_name: Optional[str] = None
    sponsor_email: Optional[str] = None
    otp_verified: bool = False

    @property
    def remaining_seconds(self) -> float:
        now = datetime.now()
        if now >= self.expires_at:
            return 0.0
        return (self.expires_at - now).total_seconds()

class GuestAccessSummary(BaseModel):
    id: str
    guest_name: str
    ssid: str
    vlan_id: int
    profile_name: str
    created_at: datetime
    expires_at: datetime
    remaining_minutes: int
    is_active: bool
    note: Optional[str] = None
    client_ip: Optional[str] = None
    user_agent: Optional[str] = None
    terms_accepted: bool = True
    guest_email: Optional[str] = None
    guest_phone: Optional[str] = None
    sponsor_name: Optional[str] = None
    sponsor_email: Optional[str] = None
    otp_verified: bool = False

class RequestOtpRequest(BaseModel):
    destination: str = Field(..., description="Adresse email ou numéro de téléphone")
    type: str = Field(default="email", description="Type de canal : email ou sms")

class VerifyOtpRequest(BaseModel):
    destination: str = Field(..., description="Adresse email ou numéro de téléphone")
    code: str = Field(..., min_length=4, max_length=10, description="Code OTP reçu")

class SendVoucherEmailRequest(BaseModel):
    recipient_email: str = Field(..., description="Adresse email destinataire")
