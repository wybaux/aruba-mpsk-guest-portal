import logging
import csv
import io
from datetime import datetime
from contextlib import asynccontextmanager
from typing import List, Optional
from pydantic import BaseModel
import pyotp

from fastapi import FastAPI, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.models import (
    CreateGuestRequest,
    GuestAccess,
    GuestAccessSummary,
    ProfileEnum,
    AVAILABLE_PROFILES,
    RequestOtpRequest,
    VerifyOtpRequest,
    SendVoucherEmailRequest
)
from app.profile_manager import profile_manager, AccessProfileInfo
from app.aruba_client import get_aruba_client, get_aruba_config, test_vc_connection
from app.scheduler import CleanupScheduler
from app.qr_generator import generate_qr_code_bytes
from app.notification_service import notification_service
from app.db import db
from app.auth_service import (
    auth_service,
    UserRole,
    UserOut,
    CreateUserSchema,
    UpdateUserSchema,
    LoginPayload,
    MfaVerifyPayload
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("wifi_guest.main")

app_start_time = datetime.now()

# Initialize Aruba Client
aruba_client = get_aruba_client()
cleanup_scheduler = CleanupScheduler(
    aruba_client=aruba_client,
    interval_seconds=settings.CLEANUP_CHECK_INTERVAL
)

def reload_active_aruba_client():
    """Hot-reload in-memory network client when VC credentials or integration mode change."""
    global aruba_client, cleanup_scheduler
    aruba_client = get_aruba_client()
    cleanup_scheduler.aruba_client = aruba_client
    logger.info("Active Aruba Client reloaded dynamically.")
    return aruba_client

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start background cleanup task
    logger.info("Starting Wi-Fi Guest Manager application...")
    cleanup_scheduler.start()
    yield
    # Shutdown: Stop background cleanup task
    logger.info("Shutting down Wi-Fi Guest Manager application...")
    cleanup_scheduler.stop()

app = FastAPI(
    title=settings.APP_TITLE,
    description="Générateur intelligent de configuration Wi-Fi Invités Homelab avec intégration API Aruba et Authentification Sécurisée RBAC / 2FA",
    version="1.2.0",
    lifespan=lifespan
)

# Set up templates
templates = Jinja2Templates(directory="app/templates")


# -----------------------------------------------------------------------------
# WEB FRONTEND ROUTES
# -----------------------------------------------------------------------------

def detect_client_language(accept_language_header: Optional[str], default_lang: str = "fr") -> str:
    """Detect client language from Accept-Language header (fr, en, es, de, pt)."""
    supported = ("fr", "en", "es", "de", "pt")
    if not accept_language_header:
        return default_lang if default_lang in supported else "fr"
    try:
        for part in accept_language_header.split(","):
            token = part.split(";")[0].strip().lower()
            code = token.split("-")[0]
            if code in supported:
                return code
    except Exception:
        pass
    return default_lang if default_lang in supported else "fr"

@app.get("/", response_class=HTMLResponse)
async def home_dashboard(request: Request):
    # Cleanup expired passes first
    aruba_client.clean_expired_passes()
    active_passes = aruba_client.list_active_passes()
    profiles = profile_manager.get_all()
    default_profile = profile_manager.get_default()
    users = auth_service.list_users()
    db_metrics = db.get_metrics()
    
    require_otp = bool(db.get_setting("require_otp_verification", settings.REQUIRE_OTP_VERIFICATION))
    require_guest_email = bool(db.get_setting("require_guest_email", getattr(settings, "REQUIRE_GUEST_EMAIL", False)))
    allowed_sponsor_domains = db.get_setting("allowed_sponsor_domains", settings.ALLOWED_SPONSOR_DOMAINS)
    smtp_cfg = notification_service.get_smtp_config()
    duration_presets = db.get_duration_presets()
    branding = db.get_branding()
    connected_clients = aruba_client.get_connected_clients()
    banned_macs = db.list_banned_macs()
    
    show_generation_details = db.get_setting("show_generation_details", "true") == "true"
    default_language = db.get_setting("default_language", getattr(settings, "DEFAULT_LANGUAGE", "fr"))
    detected_language = detect_client_language(request.headers.get("accept-language"), default_language)
    
    vc_cfg = get_aruba_config()
    vc_cfg["has_password"] = bool(vc_cfg.get("password"))

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_title": branding.get("portal_title") or settings.APP_TITLE,
            "branding": branding,
            "ssid": db.get_setting("wifi_ssid", settings.WIFI_SSID) or "Public-Test",
            "active_passes": active_passes,
            "connected_clients": connected_clients,
            "banned_macs": banned_macs,
            "profiles": profiles,
            "default_profile": default_profile,
            "aruba_mode": vc_cfg["mode"],
            "vc_cfg": vc_cfg,
            "require_otp": require_otp,
            "require_guest_email": require_guest_email,
            "allowed_sponsor_domains": allowed_sponsor_domains,
            "smtp_cfg": smtp_cfg,
            "duration_presets": duration_presets,
            "show_generation_details": show_generation_details,
            "default_language": default_language,
            "detected_language": detected_language,
            "users": users,
            "db_metrics": db_metrics
        }
    )

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return "127.0.0.1"

@app.post("/create", response_class=HTMLResponse)
async def create_guest_form(
    request: Request,
    guest_name: str = Form(...),
    duration_hours: float = Form(...),
    profile: str = Form("standard"),
    profile_password: Optional[str] = Form(None),
    terms_accepted: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    guest_email: Optional[str] = Form(None),
    guest_phone: Optional[str] = Form(None),
    sponsor_name: Optional[str] = Form(None),
    sponsor_email: Optional[str] = Form(None),
    otp_code: Optional[str] = Form(None),
    send_email_voucher: Optional[str] = Form(None),
    send_sponsor_copy: Optional[str] = Form(None)
):
    # Verify terms of use acceptance
    accepted = terms_accepted in ("on", "true", "1", True)
    if not accepted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vous devez accepter la charte d'utilisation du réseau Wi-Fi pour continuer."
        )

    profile_clean = profile.strip()
    if not profile_manager.verify_access(profile_clean, profile_password):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ce profil nécessite un mot de passe ou une autorisation valide."
        )

    # Validate sponsor domain if provided
    clean_sp_email = sponsor_email.strip() if sponsor_email and sponsor_email.strip() else None
    if clean_sp_email:
        if not notification_service.validate_sponsor_domain(clean_sp_email):
            allowed = settings.ALLOWED_SPONSOR_DOMAINS or "domaines d'entreprise autorisés"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"L'adresse email du parrain ({clean_sp_email}) doit appartenir à : {allowed}"
            )

    # Guest email mandatory check
    require_guest_email = bool(db.get_setting("require_guest_email", getattr(settings, "REQUIRE_GUEST_EMAIL", False)))
    clean_guest_email = guest_email.strip() if guest_email and guest_email.strip() else None
    if require_guest_email and not clean_guest_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Une adresse email est obligatoire dans vos coordonnées pour continuer."
        )

    # OTP Verification check
    dest = clean_guest_email or (guest_phone.strip() if guest_phone else None)
    otp_verified = False

    require_otp = bool(db.get_setting("require_otp_verification", settings.REQUIRE_OTP_VERIFICATION))
    if require_otp:
        if not dest:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Une adresse email ou numéro de mobile est obligatoire pour la vérification d'identité."
            )
        if otp_code and otp_code.strip():
            if not notification_service.verify_otp(dest, otp_code.strip()):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Code de vérification OTP incorrect ou expiré."
                )
            otp_verified = True
        elif notification_service.is_verified(dest):
            otp_verified = True
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Vous devez valider votre code de vérification avant de continuer."
            )
    else:
        if dest and otp_code and otp_code.strip():
            if notification_service.verify_otp(dest, otp_code.strip()) or notification_service.is_verified(dest):
                otp_verified = True

    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "Inconnu")

    guest_access = aruba_client.create_guest_pass(
        guest_name=guest_name.strip(),
        duration_hours=duration_hours,
        profile=profile_clean,
        note=note.strip() if note else None,
        client_ip=client_ip,
        user_agent=user_agent,
        terms_accepted=True,
        guest_email=guest_email.strip() if guest_email and guest_email.strip() else None,
        guest_phone=guest_phone.strip() if guest_phone and guest_phone.strip() else None,
        sponsor_name=sponsor_name.strip() if sponsor_name and sponsor_name.strip() else None,
        sponsor_email=clean_sp_email,
        otp_verified=otp_verified
    )

    # Automatic email delivery
    p_info = profile_manager.get(profile_clean)
    prof_name = p_info.name if p_info else profile_clean
    exp_str = guest_access.expires_at.strftime("%d/%m/%Y à %H:%M")

    # To guest
    if send_email_voucher in ("on", "true", "1", True) and guest_access.guest_email:
        notification_service.send_voucher_email(
            guest_name=guest_access.guest_name,
            ssid=guest_access.ssid,
            password=guest_access.password,
            expires_at_str=exp_str,
            profile_name=prof_name,
            recipient_email=guest_access.guest_email,
            sponsor_name=guest_access.sponsor_name,
            qr_b64=guest_access.qr_code_base64
        )

    # To sponsor copy
    if send_sponsor_copy in ("on", "true", "1", True) and clean_sp_email:
        notification_service.send_voucher_email(
            guest_name=guest_access.guest_name,
            ssid=guest_access.ssid,
            password=guest_access.password,
            expires_at_str=exp_str,
            profile_name=prof_name,
            recipient_email=clean_sp_email,
            sponsor_name=guest_access.sponsor_name,
            qr_b64=guest_access.qr_code_base64
        )

    # Return JSON if requested by AJAX / progress-bar frontend
    accept_hdr = request.headers.get("accept", "")
    if "application/json" in accept_hdr or request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(
            content={
                "success": True,
                "redirect_url": f"/guest/{guest_access.id}",
                "guest_id": guest_access.id
            }
        )

    # Redirect to individual voucher view page
    return RedirectResponse(
        url=f"/guest/{guest_access.id}",
        status_code=status.HTTP_303_SEE_OTHER
    )

@app.get("/guest/{guest_id}", response_class=HTMLResponse)
async def view_guest_voucher(request: Request, guest_id: str):
    guest = aruba_client.get_guest_pass(guest_id)
    if not guest:
        raise HTTPException(status_code=404, detail="Accès invité introuvable ou expiré")
    
    profile_info = profile_manager.get(guest.profile)
    email_sent = request.query_params.get("email_sent") == "true"
    
    branding = db.get_branding()
    return templates.TemplateResponse(
        request=request,
        name="view.html",
        context={
            "app_title": branding.get("portal_title") or settings.APP_TITLE,
            "branding": branding,
            "guest": guest,
            "profile_info": profile_info,
            "email_sent": email_sent
        }
    )

@app.post("/guest/{guest_id}/send-email", response_class=HTMLResponse)
async def web_send_voucher_email(request: Request, guest_id: str, email: str = Form(...)):
    guest = aruba_client.get_guest_pass(guest_id)
    if not guest:
        raise HTTPException(status_code=404, detail="Accès invité introuvable")
    
    profile_info = profile_manager.get(guest.profile)
    profile_name = profile_info.name if profile_info else guest.profile
    expires_str = guest.expires_at.strftime("%d/%m/%Y à %H:%M")

    notification_service.send_voucher_email(
        guest_name=guest.guest_name,
        ssid=guest.ssid,
        password=guest.password,
        expires_at_str=expires_str,
        profile_name=profile_name,
        recipient_email=email.strip(),
        sponsor_name=guest.sponsor_name,
        qr_b64=guest.qr_code_base64
    )
    return RedirectResponse(
        url=f"/guest/{guest_id}?email_sent=true",
        status_code=status.HTTP_303_SEE_OTHER
    )

@app.post("/admin/profiles", response_class=HTMLResponse)
async def admin_save_profile_form(
    id: str = Form(...),
    name: str = Form(...),
    description: str = Form(...),
    vlan_id: int = Form(190),
    bandwidth_limit_mbps: Optional[str] = Form(None),
    is_default: Optional[str] = Form(None),
    requires_password: Optional[str] = Form(None),
    access_password: Optional[str] = Form(None)
):
    bw = int(bandwidth_limit_mbps) if bandwidth_limit_mbps and bandwidth_limit_mbps.strip() and int(bandwidth_limit_mbps) > 0 else None
    is_def = is_default in ("true", "1", "on", True)
    req_pwd = (requires_password in ("true", "1", "on", True)) if not is_def else False
    clean_pwd = access_password.strip() if access_password and access_password.strip() and req_pwd else None

    prof = AccessProfileInfo(
        id=id.strip(),
        name=name.strip(),
        description=description.strip(),
        vlan_id=vlan_id,
        bandwidth_limit_mbps=bw,
        is_default=is_def,
        requires_password=req_pwd,
        access_password=clean_pwd
    )
    profile_manager.save_profile(prof, sync_to_ap=True)
    return RedirectResponse(url="/?tab=profiles", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/profiles/{profile_id}/delete", response_class=HTMLResponse)
async def admin_delete_profile_form(profile_id: str):
    profile_manager.delete_profile(profile_id, sync_to_ap=True)
    return RedirectResponse(url="/?tab=profiles", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/guest/{guest_id}/revoke", response_class=HTMLResponse)
async def revoke_guest_form(guest_id: str):
    aruba_client.revoke_guest_pass(guest_id)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/guest/{guest_id}/qr.png")
async def get_guest_qr_png(guest_id: str):
    guest = aruba_client.get_guest_pass(guest_id)
    if not guest:
        raise HTTPException(status_code=404, detail="Accès invité introuvable")
    
    png_bytes = generate_qr_code_bytes(guest.wifi_string)
    return Response(content=png_bytes, media_type="image/png")


# -----------------------------------------------------------------------------
# REST API ENDPOINTS
# -----------------------------------------------------------------------------

@app.get("/api/profiles")
async def api_list_profiles():
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "vlan_id": p.vlan_id,
            "bandwidth_limit_mbps": p.bandwidth_limit_mbps,
            "role_name": p.get_role_name(),
            "is_default": p.is_default,
            "requires_password": p.requires_password,
            "has_password": bool(p.access_password and p.access_password.strip())
        }
        for p in profile_manager.get_all().values()
    ]

class VerifyProfileRequest(BaseModel):
    password: str

@app.post("/api/profiles/{profile_id}/verify")
async def api_verify_profile_password(profile_id: str, payload: VerifyProfileRequest):
    valid = profile_manager.verify_access(profile_id, payload.password)
    if not valid:
        raise HTTPException(status_code=401, detail="Mot de passe ou code d'autorisation invalide")
    return {"valid": True, "message": "Profil autorisé avec succès"}

@app.post("/api/profiles", response_model=AccessProfileInfo, status_code=status.HTTP_201_CREATED)
async def api_create_or_update_profile(profile: AccessProfileInfo):
    return profile_manager.save_profile(profile, sync_to_ap=True)

@app.delete("/api/profiles/{profile_id}")
async def api_delete_profile(profile_id: str):
    success = profile_manager.delete_profile(profile_id)
    if not success:
        raise HTTPException(status_code=400, detail="Impossible de supprimer ce profil")
    return {"status": "success", "message": f"Profil {profile_id} supprimé"}

@app.get("/api/guests", response_model=List[GuestAccessSummary])
async def api_list_guests():
    aruba_client.clean_expired_passes()
    passes = aruba_client.list_active_passes()
    
    summaries = []
    for p in passes:
        p_info = profile_manager.get(p.profile)
        summaries.append(
            GuestAccessSummary(
                id=p.id,
                guest_name=p.guest_name,
                ssid=p.ssid,
                vlan_id=p.vlan_id,
                profile_name=p_info.name if p_info else str(p.profile),
                created_at=p.created_at,
                expires_at=p.expires_at,
                remaining_minutes=int(p.remaining_seconds // 60),
                is_active=p.is_active,
                note=p.note,
                client_ip=p.client_ip,
                user_agent=p.user_agent,
                terms_accepted=p.terms_accepted,
                guest_email=p.guest_email,
                guest_phone=p.guest_phone,
                sponsor_name=p.sponsor_name,
                sponsor_email=p.sponsor_email,
                otp_verified=p.otp_verified
            )
        )
    return summaries

@app.post("/api/guests", response_model=GuestAccess, status_code=status.HTTP_201_CREATED)
async def api_create_guest(req: CreateGuestRequest, request: Request):
    if not req.terms_accepted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vous devez accepter la charte d'utilisation du réseau Wi-Fi."
        )
    if not profile_manager.verify_access(req.profile, req.profile_password):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ce profil nécessite un mot de passe ou une autorisation valide."
        )

    # Validate sponsor domain if provided
    if req.sponsor_email:
        if not notification_service.validate_sponsor_domain(req.sponsor_email):
            allowed = settings.ALLOWED_SPONSOR_DOMAINS or "domaines autorisés"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"L'adresse email du parrain doit appartenir à : {allowed}"
            )

    # Guest email mandatory check
    require_guest_email = bool(db.get_setting("require_guest_email", getattr(settings, "REQUIRE_GUEST_EMAIL", False)))
    if require_guest_email and (not req.guest_email or not req.guest_email.strip()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Une adresse email est obligatoire dans vos coordonnées pour continuer."
        )

    # OTP Verification check
    dest = (req.guest_email.strip() if req.guest_email else None) or (req.guest_phone.strip() if req.guest_phone else None)
    otp_verified = False

    require_otp = bool(db.get_setting("require_otp_verification", settings.REQUIRE_OTP_VERIFICATION))
    if require_otp:
        if not dest:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Une adresse email ou numéro de mobile est obligatoire pour la vérification d'identité."
            )
        if req.otp_code and req.otp_code.strip():
            if not notification_service.verify_otp(dest, req.otp_code.strip()):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Code de vérification OTP incorrect ou expiré."
                )
            otp_verified = True
        elif notification_service.is_verified(dest):
            otp_verified = True
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Vous devez valider votre code de vérification avant de continuer."
            )
    else:
        if dest and req.otp_code and req.otp_code.strip():
            if notification_service.verify_otp(dest, req.otp_code.strip()) or notification_service.is_verified(dest):
                otp_verified = True

    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "Inconnu")

    guest_access = aruba_client.create_guest_pass(
        guest_name=req.guest_name,
        duration_hours=req.duration_hours,
        profile=req.profile,
        note=req.note,
        client_ip=client_ip,
        user_agent=user_agent,
        terms_accepted=req.terms_accepted,
        guest_email=req.guest_email,
        guest_phone=req.guest_phone,
        sponsor_name=req.sponsor_name,
        sponsor_email=req.sponsor_email,
        otp_verified=otp_verified
    )

    # Dispatch email if requested
    if req.send_email_voucher and req.guest_email:
        p_info = profile_manager.get(req.profile)
        prof_name = p_info.name if p_info else req.profile
        exp_str = guest_access.expires_at.strftime("%d/%m/%Y à %H:%M")
        notification_service.send_voucher_email(
            guest_name=guest_access.guest_name,
            ssid=guest_access.ssid,
            password=guest_access.password,
            expires_at_str=exp_str,
            profile_name=prof_name,
            recipient_email=req.guest_email,
            sponsor_name=req.sponsor_name,
            qr_b64=guest_access.qr_code_base64
        )

    if req.send_sponsor_copy and req.sponsor_email:
        p_info = profile_manager.get(req.profile)
        prof_name = p_info.name if p_info else req.profile
        exp_str = guest_access.expires_at.strftime("%d/%m/%Y à %H:%M")
        notification_service.send_voucher_email(
            guest_name=guest_access.guest_name,
            ssid=guest_access.ssid,
            password=guest_access.password,
            expires_at_str=exp_str,
            profile_name=prof_name,
            recipient_email=req.sponsor_email,
            sponsor_name=req.sponsor_name,
            qr_b64=guest_access.qr_code_base64
        )

    return guest_access

@app.get("/api/guests/{guest_id}", response_model=GuestAccess)
async def api_get_guest(guest_id: str):
    guest = aruba_client.get_guest_pass(guest_id)
    if not guest:
        raise HTTPException(status_code=404, detail="Accès invité introuvable")
    return guest

@app.delete("/api/guests/{guest_id}")
async def api_revoke_guest(guest_id: str):
    success = aruba_client.revoke_guest_pass(guest_id)
    if not success:
        raise HTTPException(status_code=404, detail="Accès invité introuvable")
    return {"status": "success", "message": f"Accès invité {guest_id} révoqué"}


# -----------------------------------------------------------------------------
# OTP & NOTIFICATION API ENDPOINTS
# -----------------------------------------------------------------------------

@app.post("/api/otp/request")
async def api_request_otp(payload: RequestOtpRequest):
    code = notification_service.generate_otp(payload.destination)
    if payload.type == "email":
        notification_service.send_otp_email(payload.destination, code)
    else:
        logger.info(f"[SMS SIMULATION] Sent OTP {code} to {payload.destination}")
    
    return {
        "success": True,
        "message": f"Code de vérification envoyé à {payload.destination}",
        "simulated": not bool(settings.SMTP_HOST),
        "demo_code": code if not settings.SMTP_HOST else None
    }

@app.post("/api/otp/verify")
async def api_verify_otp(payload: VerifyOtpRequest):
    valid = notification_service.verify_otp(payload.destination, payload.code)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Code de vérification invalide ou expiré."
        )
    return {"success": True, "message": "Identité vérifiée avec succès"}

@app.post("/api/guests/{guest_id}/send-email")
async def api_send_voucher_email(guest_id: str, payload: SendVoucherEmailRequest):
    guest = aruba_client.get_guest_pass(guest_id)
    if not guest:
        raise HTTPException(status_code=404, detail="Accès invité introuvable")
    
    profile_info = profile_manager.get(guest.profile)
    profile_name = profile_info.name if profile_info else guest.profile
    expires_str = guest.expires_at.strftime("%d/%m/%Y à %H:%M")

    success = notification_service.send_voucher_email(
        guest_name=guest.guest_name,
        ssid=guest.ssid,
        password=guest.password,
        expires_at_str=expires_str,
        profile_name=profile_name,
        recipient_email=payload.recipient_email,
        sponsor_name=guest.sponsor_name,
        qr_b64=guest.qr_code_base64
    )
    if not success:
        raise HTTPException(status_code=500, detail="Échec de l'envoi de l'e-mail")
    return {"success": True, "message": f"Billet d'accès envoyé avec succès à {payload.recipient_email}"}


# -----------------------------------------------------------------------------
# LEGAL AUDIT EXPORT ENDPOINT (CSV / JSON)
# -----------------------------------------------------------------------------

@app.get("/api/admin/audit/export")
async def export_audit_log(format: str = "csv"):
    """Export complete legal compliance audit registry (both active and expired records)."""
    all_passes = list(aruba_client._passes.values()) if hasattr(aruba_client, "_passes") else []
    
    if format.lower() == "json":
        return all_passes

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "ID_Acces",
        "Nom_Invite",
        "SSID",
        "Profil",
        "VLAN",
        "Email_Invite",
        "Telephone_Invite",
        "Parrain_Nom",
        "Parrain_Email",
        "IP_Source",
        "User_Agent",
        "Date_Emission",
        "Date_Expiration",
        "Duree_Heures",
        "Charte_Acceptee",
        "OTP_Verifie",
        "Statut_Actif"
    ])
    for p in all_passes:
        writer.writerow([
            p.get("id"),
            p.get("guest_name"),
            p.get("ssid"),
            p.get("profile"),
            p.get("vlan_id"),
            p.get("guest_email", ""),
            p.get("guest_phone", ""),
            p.get("sponsor_name", ""),
            p.get("sponsor_email", ""),
            p.get("client_ip", ""),
            p.get("user_agent", ""),
            p.get("created_at"),
            p.get("expires_at"),
            p.get("duration_hours"),
            "OUI" if p.get("terms_accepted") else "NON",
            "OUI" if p.get("otp_verified") else "NON",
            "ACTIF" if p.get("is_active") else "EXPIRE/REVOKE"
        ])
    
    csv_data = "\ufeff" + output.getvalue()  # Add UTF-8 BOM for Excel compatibility
    filename = f"registre_audit_wifi_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Type": "text/csv; charset=utf-8"
        }
    )


# -----------------------------------------------------------------------------
# DEVOPS: HEALTH CHECK & PROMETHEUS METRICS
# -----------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    """Health check endpoint for Docker, Kubernetes, and uptime probes."""
    db_metrics = db.get_metrics()
    uptime = (datetime.now() - app_start_time).total_seconds()
    return {
        "status": "healthy",
        "uptime_seconds": round(uptime, 1),
        "app_title": settings.APP_TITLE,
        "aruba_mode": db.get_setting("aruba_mode", settings.ARUBA_MODE),
        "database": {
            "status": "connected",
            "file": db_metrics["db_file"],
            "size_bytes": db_metrics["db_size_bytes"],
            "active_passes": db_metrics["active_passes"],
            "total_passes": db_metrics["total_passes"],
            "total_users": db_metrics["total_users"]
        }
    }

@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus exposition metrics endpoint (RFC text standard)."""
    db_metrics = db.get_metrics()
    uptime = (datetime.now() - app_start_time).total_seconds()

    lines = [
        "# HELP wifi_guest_uptime_seconds Application uptime in seconds",
        "# TYPE wifi_guest_uptime_seconds counter",
        f"wifi_guest_uptime_seconds {uptime:.1f}",
        "# HELP wifi_guest_active_passes Number of currently active Wi-Fi guest passes",
        "# TYPE wifi_guest_active_passes gauge",
        f"wifi_guest_active_passes {db_metrics['active_passes']}",
        "# HELP wifi_guest_total_passes Total number of guest passes created",
        "# TYPE wifi_guest_total_passes counter",
        f"wifi_guest_total_passes {db_metrics['total_passes']}",
        "# HELP wifi_guest_revoked_passes Number of expired or revoked passes",
        "# TYPE wifi_guest_revoked_passes gauge",
        f"wifi_guest_revoked_passes {db_metrics['revoked_or_expired_passes']}",
        "# HELP wifi_guest_total_users Number of registered admin/operator users",
        "# TYPE wifi_guest_total_users gauge",
        f"wifi_guest_total_users {db_metrics['total_users']}",
        "# HELP wifi_guest_db_size_bytes Size of SQLite database file in bytes",
        "# TYPE wifi_guest_db_size_bytes gauge",
        f"wifi_guest_db_size_bytes {db_metrics['db_size_bytes']}"
    ]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")


# -----------------------------------------------------------------------------
# USER AUTHENTICATION & RBAC WITH OTP / 2FA (MFA)
# -----------------------------------------------------------------------------

def get_current_user_from_request(request: Request) -> Optional[dict]:
    auth_header = request.headers.get("Authorization")
    token = None
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1].strip()
    elif "access_token" in request.cookies:
        token = request.cookies["access_token"]
    
    if not token:
        return None
    
    payload = auth_service.decode_jwt_token(token)
    if not payload or not payload.get("sub"):
        return None
    return auth_service.get_user_by_id(payload["sub"])

@app.post("/api/auth/login")
async def api_auth_login(payload: LoginPayload, request: Request, response: Response):
    """Step 1 of Login: checks credentials and triggers 2FA/MFA if enabled."""
    client_ip = get_client_ip(request)
    clean_user = payload.username.strip().lower()

    # Master admin password fallback
    if clean_user == "admin" and payload.password == settings.ADMIN_PASSWORD:
        user = auth_service.get_user_by_username("admin")
        if user and user.get("mfa_enabled"):
            return auth_service.login_step1("admin", payload.password, client_ip=client_ip)
        
        token = auth_service.create_jwt_token({
            "sub": str(user["id"]) if user else "1",
            "username": "admin",
            "role": "admin",
            "full_name": user["full_name"] if user else "Administrateur Système"
        })
        response.set_cookie(key="access_token", value=token, httponly=True, samesite="lax", max_age=12 * 3600)
        u_out = auth_service.get_user_out(user["id"]) if user else None
        return {
            "success": True,
            "mfa_required": False,
            "access_token": token,
            "user": u_out.model_dump() if u_out else {
                "id": 1,
                "username": "admin",
                "role": "admin",
                "full_name": "Administrateur Système",
                "is_active": True,
                "mfa_enabled": False,
                "mfa_type": "totp",
                "created_at": datetime.now().isoformat()
            }
        }

    result = auth_service.login_step1(clean_user, payload.password, client_ip=client_ip)
    if not result.get("success"):
        raise HTTPException(status_code=401, detail=result.get("detail", "Identifiants incorrects"))
    if not result.get("mfa_required") and result.get("access_token"):
        response.set_cookie(
            key="access_token",
            value=result["access_token"],
            httponly=True,
            samesite="lax",
            max_age=12 * 3600
        )
    return result

@app.post("/api/auth/mfa-verify")
async def api_auth_mfa_verify(payload: MfaVerifyPayload, request: Request, response: Response):
    """Step 2 of Login: validates MFA OTP (TOTP Authenticator or Email OTP)."""
    client_ip = get_client_ip(request)
    result = auth_service.login_step2_mfa(payload.temp_token, payload.code, client_ip=client_ip)
    if not result.get("success"):
        raise HTTPException(status_code=401, detail=result.get("detail", "Code 2FA invalide"))
    if result.get("access_token"):
        response.set_cookie(
            key="access_token",
            value=result["access_token"],
            httponly=True,
            samesite="lax",
            max_age=12 * 3600
        )
    return result

@app.post("/api/auth/logout")
async def api_auth_logout(response: Response):
    """Log out current user and clear access token cookie."""
    response.delete_cookie(key="access_token")
    return {"success": True, "message": "Déconnexion réussie"}

@app.get("/api/auth/me")
async def api_auth_me(request: Request):
    """Returns currently authenticated user profile."""
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Non authentifié")
    return auth_service.get_user_out(user["id"])

# -----------------------------------------------------------------------------
# USER MANAGEMENT (ADMIN ONLY)
# -----------------------------------------------------------------------------

@app.get("/api/admin/users", response_model=List[UserOut])
async def api_admin_list_users():
    return auth_service.list_users()

@app.post("/api/admin/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def api_admin_create_user(payload: CreateUserSchema, request: Request):
    try:
        current = get_current_user_from_request(request)
        actor = current["username"] if current else "admin"
        return auth_service.create_user(payload, creator=actor)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.put("/api/admin/users/{user_id}", response_model=UserOut)
async def api_admin_update_user(user_id: int, payload: UpdateUserSchema, request: Request):
    current = get_current_user_from_request(request)
    actor = current["username"] if current else "admin"
    try:
        updated = auth_service.update_user(user_id, payload, actor=actor)
        if not updated:
            raise HTTPException(status_code=404, detail="Utilisateur introuvable")
        return updated
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/admin/users/{user_id}")
async def api_admin_delete_user(user_id: int, request: Request):
    current = get_current_user_from_request(request)
    actor = current["username"] if current else "admin"
    try:
        success = auth_service.delete_user(user_id, actor=actor)
        if not success:
            raise HTTPException(status_code=404, detail="Utilisateur introuvable")
        return {"success": True, "message": "Utilisateur supprimé avec succès"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/admin/users/{user_id}/totp-setup")
async def api_admin_totp_setup(user_id: int):
    """Generate TOTP QR Code and secret for authenticator apps."""
    try:
        return auth_service.setup_totp(user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

class EnableTotpPayload(BaseModel):
    code: str

@app.post("/api/admin/users/{user_id}/totp-enable")
async def api_admin_totp_enable(user_id: int, payload: EnableTotpPayload):
    """Confirm first TOTP code and enable MFA."""
    user = auth_service.get_user_by_id(user_id)
    if not user or not user["totp_secret"]:
        raise HTTPException(status_code=404, detail="Configuration TOTP introuvable")
    
    totp = pyotp.TOTP(user["totp_secret"])
    if not totp.verify(payload.code.strip(), valid_window=1):
        raise HTTPException(status_code=400, detail="Code de validation incorrect. Assurez-vous que l'heure de votre appareil est synchronisée.")

    auth_service.update_user(user_id, UpdateUserSchema(mfa_enabled=True, mfa_type="totp"))
    return {"success": True, "message": "Authentification à deux facteurs TOTP activée avec succès !"}

# Backward compatibility login endpoint
class AdminLoginRequest(CreateGuestRequest.__base__):
    password: str

@app.post("/api/admin/login")
async def api_admin_login(payload: AdminLoginRequest, request: Request):
    client_ip = get_client_ip(request)
    # Check master password
    if payload.password == settings.ADMIN_PASSWORD:
        token = auth_service.create_jwt_token({"sub": "1", "username": "admin", "role": "admin"})
        return {"success": True, "message": "Authentification réussie", "access_token": token}

    # Try multi-user database login
    res = auth_service.login_step1("admin", payload.password, client_ip=client_ip)
    if res.get("success"):
        return res
    raise HTTPException(status_code=401, detail="Mot de passe incorrect")


# -----------------------------------------------------------------------------
# DYNAMIC SYSTEM & SMTP SETTINGS MANAGEMENT
# -----------------------------------------------------------------------------

class UpdateSettingsPayload(BaseModel):
    require_otp_verification: Optional[bool] = None
    require_guest_email: Optional[bool] = None
    show_generation_details: Optional[bool] = None
    default_language: Optional[str] = None
    allowed_sponsor_domains: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_tls: Optional[bool] = None
    duration_presets: Optional[List[int]] = None
    wifi_ssid: Optional[str] = None
    wifi_password: Optional[str] = None
    aruba_mode: Optional[str] = None
    aruba_instant_host: Optional[str] = None
    aruba_instant_username: Optional[str] = None
    aruba_instant_password: Optional[str] = None
    aruba_instant_verify_ssl: Optional[bool] = None
    aruba_mpsk_profile: Optional[str] = None

class ToggleOtpPayload(BaseModel):
    enabled: bool

class ToggleRequireEmailPayload(BaseModel):
    enabled: bool

class ToggleGenDetailsPayload(BaseModel):
    enabled: bool

class SetDefaultLangPayload(BaseModel):
    language: str

class TestEmailPayload(BaseModel):
    recipient_email: str

class TestVcPayload(BaseModel):
    mode: Optional[str] = None
    aruba_mode: Optional[str] = None
    host: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    verify_ssl: Optional[bool] = None

class UpdateDurationsPayload(BaseModel):
    duration_presets: List[int]

@app.get("/api/admin/settings")
async def api_admin_get_settings():
    """Retrieve active system, SMTP and Aruba Virtual Controller configuration."""
    cfg = notification_service.get_smtp_config()
    vc_cfg = get_aruba_config()
    return {
        "require_otp_verification": bool(db.get_setting("require_otp_verification", settings.REQUIRE_OTP_VERIFICATION)),
        "require_guest_email": bool(db.get_setting("require_guest_email", getattr(settings, "REQUIRE_GUEST_EMAIL", False))),
        "show_generation_details": db.get_setting("show_generation_details", "true") == "true",
        "default_language": db.get_setting("default_language", getattr(settings, "DEFAULT_LANGUAGE", "fr")),
        "allowed_sponsor_domains": db.get_setting("allowed_sponsor_domains", settings.ALLOWED_SPONSOR_DOMAINS) or "",
        "smtp_host": cfg["host"] or "",
        "smtp_port": cfg["port"],
        "smtp_user": cfg["user"] or "",
        "smtp_from": cfg["from_email"] or "",
        "smtp_tls": cfg["tls"],
        "has_smtp_password": bool(cfg["password"]),
        "duration_presets": db.get_duration_presets(),
        "wifi_ssid": db.get_setting("wifi_ssid", settings.WIFI_SSID) or "Public-Test",
        "wifi_password": db.get_setting("wifi_password", getattr(settings, "WIFI_PASSWORD", "")) or "",
        "aruba_mode": vc_cfg["mode"],
        "aruba_instant_host": vc_cfg["host"],
        "aruba_instant_username": vc_cfg["username"],
        "aruba_instant_verify_ssl": vc_cfg["verify_ssl"],
        "aruba_mpsk_profile": vc_cfg["mpsk_profile"],
        "has_vc_password": bool(vc_cfg["password"])
    }

@app.post("/api/admin/settings")
async def api_admin_update_settings(payload: UpdateSettingsPayload, request: Request):
    """Save or update system, SMTP and Aruba VC settings in SQLite."""
    current = get_current_user_from_request(request)
    actor = current["username"] if current else "admin"

    if payload.require_otp_verification is not None:
        db.set_setting("require_otp_verification", payload.require_otp_verification)
    if payload.require_guest_email is not None:
        db.set_setting("require_guest_email", payload.require_guest_email)
    if payload.show_generation_details is not None:
        db.set_setting("show_generation_details", "true" if payload.show_generation_details else "false")
    if payload.default_language is not None:
        clean_lang = payload.default_language.lower().strip()
        if clean_lang in ("fr", "en", "es", "de", "pt"):
            db.set_setting("default_language", clean_lang)
        else:
            raise HTTPException(status_code=400, detail=f"Langue '{clean_lang}' non supportée. Choix valides: fr, en, es, de, pt.")
    if payload.allowed_sponsor_domains is not None:
        db.set_setting("allowed_sponsor_domains", payload.allowed_sponsor_domains.strip())
    if payload.smtp_host is not None:
        db.set_setting("smtp_host", payload.smtp_host.strip())
    if payload.smtp_port is not None:
        db.set_setting("smtp_port", payload.smtp_port)
    if payload.smtp_user is not None:
        db.set_setting("smtp_user", payload.smtp_user.strip())
    if payload.smtp_password is not None and payload.smtp_password != "":
        db.set_setting("smtp_password", payload.smtp_password)
    if payload.smtp_from is not None:
        db.set_setting("smtp_from", payload.smtp_from.strip())
    if payload.smtp_tls is not None:
        db.set_setting("smtp_tls", payload.smtp_tls)
    if payload.duration_presets is not None:
        try:
            db.set_duration_presets(payload.duration_presets)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if payload.wifi_ssid is not None:
        db.set_setting("wifi_ssid", payload.wifi_ssid.strip())
    if payload.wifi_password is not None:
        db.set_setting("wifi_password", payload.wifi_password.strip())

    # Aruba Virtual Controller settings
    vc_updated = False
    if payload.aruba_mode is not None:
        clean_mode = payload.aruba_mode.strip().lower()
        if clean_mode in ("instant", "mock", "central"):
            db.set_setting("aruba_mode", clean_mode)
            vc_updated = True
    if payload.aruba_instant_host is not None:
        db.set_setting("aruba_instant_host", payload.aruba_instant_host.strip())
        vc_updated = True
    if payload.aruba_instant_username is not None:
        db.set_setting("aruba_instant_username", payload.aruba_instant_username.strip())
        vc_updated = True
    if payload.aruba_instant_password is not None and payload.aruba_instant_password != "":
        db.set_setting("aruba_instant_password", payload.aruba_instant_password)
        vc_updated = True
    if payload.aruba_instant_verify_ssl is not None:
        db.set_setting("aruba_instant_verify_ssl", payload.aruba_instant_verify_ssl)
        vc_updated = True
    if payload.aruba_mpsk_profile is not None:
        db.set_setting("aruba_mpsk_profile", payload.aruba_mpsk_profile.strip())
        vc_updated = True

    if vc_updated:
        reload_active_aruba_client()

    db.log_audit_event("SETTINGS_UPDATED", actor=actor, details="Updated system/SMTP/VC settings")
    return {"success": True, "message": "Paramètres mis à jour avec succès !"}

@app.post("/api/admin/settings/toggle-otp")
async def api_admin_toggle_otp(payload: ToggleOtpPayload, request: Request):
    """Quick 1-click toggle for guest OTP verification requirement."""
    current = get_current_user_from_request(request)
    actor = current["username"] if current else "admin"
    db.set_setting("require_otp_verification", payload.enabled)
    status_txt = "activée (obligatoire)" if payload.enabled else "désactivée (optionnelle)"
    db.log_audit_event("OTP_POLICY_CHANGED", actor=actor, details=f"Vérification OTP {status_txt}")
    return {
        "success": True,
        "require_otp_verification": payload.enabled,
        "message": f"La vérification OTP pour les invités est désormais {status_txt}."
    }

@app.post("/api/admin/settings/toggle-require-email")
async def api_admin_toggle_require_email(payload: ToggleRequireEmailPayload, request: Request):
    """Quick 1-click toggle for guest email requirement in contact section."""
    current = get_current_user_from_request(request)
    actor = current["username"] if current else "admin"
    db.set_setting("require_guest_email", payload.enabled)
    status_txt = "activée (obligatoire)" if payload.enabled else "désactivée (optionnelle)"
    db.log_audit_event("REQUIRE_EMAIL_POLICY_CHANGED", actor=actor, details=f"Saisie email invité {status_txt}")
    return {
        "success": True,
        "require_guest_email": payload.enabled,
        "message": f"La saisie de l'email pour les invités est désormais {status_txt}."
    }

@app.post("/api/admin/settings/toggle-gen-details")
async def api_admin_toggle_gen_details(payload: ToggleGenDetailsPayload, request: Request):
    """Quick 1-click toggle for showing/hiding generation technical details by default."""
    current = get_current_user_from_request(request)
    actor = current["username"] if current else "admin"
    db.set_setting("show_generation_details", "true" if payload.enabled else "false")
    status_txt = "activé (détails affichés)" if payload.enabled else "désactivé (vue épurée)"
    db.log_audit_event("GEN_DETAILS_POLICY_CHANGED", actor=actor, details=f"Détails de génération {status_txt}")
    return {
        "success": True,
        "show_generation_details": payload.enabled,
        "message": f"L'affichage des détails lors de la génération est désormais {status_txt}."
    }

@app.post("/api/admin/settings/default-language")
async def api_admin_set_default_language(payload: SetDefaultLangPayload, request: Request):
    """Quick 1-click update for portal default fallback language."""
    current = get_current_user_from_request(request)
    actor = current["username"] if current else "admin"
    clean_lang = payload.language.lower().strip()
    if clean_lang not in ("fr", "en", "es", "de", "pt"):
        raise HTTPException(status_code=400, detail=f"Langue '{clean_lang}' non supportée. Choix valides: fr, en, es, de, pt.")
    db.set_setting("default_language", clean_lang)
    db.log_audit_event("DEFAULT_LANGUAGE_CHANGED", actor=actor, details=f"Langue par défaut changée pour : {clean_lang.upper()}")
    return {
        "success": True,
        "default_language": clean_lang,
        "message": f"La langue par défaut du portail est désormais : {clean_lang.upper()}."
    }

@app.post("/api/admin/settings/durations")
async def api_admin_update_durations(payload: UpdateDurationsPayload, request: Request):
    """Update the 5 guest access duration presets."""
    current = get_current_user_from_request(request)
    actor = current["username"] if current else "admin"
    try:
        saved = db.set_duration_presets(payload.duration_presets)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    db.log_audit_event("DURATIONS_UPDATED", actor=actor, details=f"Paliers de durée mis à jour : {saved}")
    return {
        "success": True,
        "duration_presets": saved,
        "message": f"Les 5 paliers de durée ont été enregistrés : {', '.join(f'{d}h' for d in saved)}"
    }

@app.post("/api/admin/settings/test-email")
async def api_admin_test_email(payload: TestEmailPayload, request: Request):
    """Test SMTP connection and send a test message."""
    if not payload.recipient_email or "@" not in payload.recipient_email:
        raise HTTPException(status_code=400, detail="Veuillez renseigner une adresse email destinataire valide.")
    success, message = notification_service.test_smtp_connection(payload.recipient_email.strip())
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {"success": True, "message": message}

@app.post("/api/admin/settings/test-vc")
async def api_admin_test_vc(payload: TestVcPayload, request: Request):
    """Test Virtual Controller connectivity and credentials."""
    effective_mode = payload.mode or payload.aruba_mode or db.get_setting("aruba_mode", settings.ARUBA_MODE)
    success, message = test_vc_connection(
        host=payload.host,
        username=payload.username,
        password=payload.password,
        verify_ssl=payload.verify_ssl if payload.verify_ssl is not None else False,
        mode=effective_mode
    )
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {"success": True, "message": message}

# -----------------------------------------------------------------------------
# PASS PROLONGATION & EXTENSION ENDPOINTS
# -----------------------------------------------------------------------------

class ExtendPassPayload(BaseModel):
    additional_hours: float = 2.0

@app.post("/api/guests/{guest_id}/extend")
async def api_extend_guest_pass(guest_id: str, payload: ExtendPassPayload, request: Request):
    """Extend validity duration of an existing pass."""
    if payload.additional_hours <= 0 or payload.additional_hours > 720:
        raise HTTPException(status_code=400, detail="La durée de prolongation doit être comprise entre 1 et 720 heures.")
    
    updated = aruba_client.extend_guest_pass(guest_id, payload.additional_hours)
    if not updated:
        raise HTTPException(status_code=404, detail="Accès invité introuvable ou expiré.")

    return {
        "success": True,
        "guest_id": guest_id,
        "additional_hours": payload.additional_hours,
        "extended_hours": payload.additional_hours,
        "new_expires_at": updated.expires_at.isoformat(),
        "expires_at_formatted": updated.expires_at.strftime("%d/%m/%Y à %H:%M"),
        "remaining_seconds": updated.remaining_seconds,
        "message": f"Accès prolongé de {payload.additional_hours}h avec succès !"
    }

@app.get("/extend/{guest_id}", response_class=HTMLResponse)
async def web_extend_pass_page(request: Request, guest_id: str, hours: Optional[float] = None):
    """Web page allowing 1-click prolongation of Wi-Fi pass."""
    guest = aruba_client.get_guest_pass(guest_id)
    if not guest:
        raise HTTPException(status_code=404, detail="Accès invité introuvable ou expiré.")

    extended = False
    if hours and hours > 0:
        updated = aruba_client.extend_guest_pass(guest_id, hours)
        if updated:
            guest = updated
            extended = True

    branding = db.get_branding()
    return templates.TemplateResponse(
        request=request,
        name="extend.html",
        context={
            "app_title": branding.get("portal_title") or settings.APP_TITLE,
            "branding": branding,
            "guest": guest,
            "extended": extended,
            "added_hours": hours or 2.0
        }
    )

# -----------------------------------------------------------------------------
# LIVE CLIENTS MONITORING & BANNING (DISCONNECT / BLACKLIST)
# -----------------------------------------------------------------------------

class ClientActionPayload(BaseModel):
    mac: Optional[str] = None
    mac_address: Optional[str] = None
    ip: Optional[str] = None
    guest_name: Optional[str] = None
    reason: Optional[str] = ""

    def get_mac(self) -> str:
        return (self.mac or self.mac_address or "").strip()

@app.get("/api/admin/clients/live")
async def api_admin_live_clients(request: Request):
    """Return live Wi-Fi radio clients connected to APs and banned MACs."""
    clients = aruba_client.get_connected_clients()
    banned = db.list_banned_macs()
    return {
        "clients": clients,
        "total_connected": len(clients),
        "banned_macs": banned
    }

@app.post("/api/admin/clients/disconnect")
async def api_admin_disconnect_client(payload: ClientActionPayload, request: Request):
    """Force disconnect client device from AP."""
    current = get_current_user_from_request(request)
    actor = current["username"] if current else "admin"
    target_mac = payload.get_mac()
    if not target_mac:
        raise HTTPException(status_code=400, detail="Adresse MAC requise.")
    success = aruba_client.disconnect_client(target_mac)
    db.log_audit_event("CLIENT_DISCONNECTED", actor=actor, target=target_mac, details=payload.reason or "Déconnexion manuelle")
    return {"success": success, "message": f"Appareil {target_mac} déconnecté avec succès."}

@app.post("/api/admin/clients/ban")
async def api_admin_ban_client(payload: ClientActionPayload, request: Request):
    """Force disconnect and ban MAC address on network."""
    current = get_current_user_from_request(request)
    actor = current["username"] if current else "admin"
    target_mac = payload.get_mac()
    if not target_mac:
        raise HTTPException(status_code=400, detail="Adresse MAC requise.")
    success = aruba_client.blacklist_client(target_mac, reason=payload.reason or "Banni par l'administrateur", actor=actor)
    db.ban_mac(target_mac, reason=payload.reason or "Banni par l'administrateur", actor=actor, ip=payload.ip, guest_name=payload.guest_name)
    return {"success": success, "message": f"Appareil {target_mac} banni avec succès du réseau."}

@app.delete("/api/admin/clients/ban/{mac}")
async def api_admin_unban_client(mac: str, request: Request):
    """Remove MAC address from blacklist."""
    current = get_current_user_from_request(request)
    actor = current["username"] if current else "admin"
    success = aruba_client.unblacklist_client(mac, actor=actor)
    return {"success": success, "message": f"Appareil {mac} débanni avec succès."}

# -----------------------------------------------------------------------------
# BRANDING & WHITE-LABELING MANAGEMENT
# -----------------------------------------------------------------------------

class BrandingPayload(BaseModel):
    company_name: Optional[str] = None
    portal_title: Optional[str] = None
    portal_subtitle: Optional[str] = None
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None
    accent_color: Optional[str] = None
    charter_text: Optional[str] = None
    custom_charter: Optional[str] = None
    footer_text: Optional[str] = None

@app.get("/api/admin/branding")
async def api_admin_get_branding():
    """Retrieve dynamic branding settings."""
    return db.get_branding()

@app.post("/api/admin/branding")
async def api_admin_update_branding(payload: BrandingPayload, request: Request):
    """Update dynamic branding settings."""
    current = get_current_user_from_request(request)
    actor = current["username"] if current else "admin"
    db.set_branding(payload.model_dump(exclude_unset=True))
    db.log_audit_event("BRANDING_UPDATED", actor=actor, details="Mise à jour de l'identité visuelle")
    return {"success": True, "branding": db.get_branding(), "message": "Identité visuelle mise à jour avec succès !"}




