import logging
import csv
import io
from datetime import datetime
from contextlib import asynccontextmanager
from typing import List, Optional
from pydantic import BaseModel

from fastapi import FastAPI, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
from app.aruba_client import get_aruba_client
from app.scheduler import CleanupScheduler
from app.qr_generator import generate_qr_code_bytes
from app.notification_service import notification_service

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("wifi_guest.main")

# Initialize Aruba Client
aruba_client = get_aruba_client()
cleanup_scheduler = CleanupScheduler(
    aruba_client=aruba_client,
    interval_seconds=settings.CLEANUP_CHECK_INTERVAL
)

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
    description="Générateur intelligent de configuration Wi-Fi Invités Homelab avec intégration API Aruba",
    version="1.1.0",
    lifespan=lifespan
)

# Set up templates
templates = Jinja2Templates(directory="app/templates")


# -----------------------------------------------------------------------------
# WEB FRONTEND ROUTES
# -----------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home_dashboard(request: Request):
    # Cleanup expired passes first
    aruba_client.clean_expired_passes()
    active_passes = aruba_client.list_active_passes()
    profiles = profile_manager.get_all()
    default_profile = profile_manager.get_default()
    
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_title": settings.APP_TITLE,
            "ssid": settings.WIFI_SSID,
            "active_passes": active_passes,
            "profiles": profiles,
            "default_profile": default_profile,
            "aruba_mode": settings.ARUBA_MODE,
            "require_otp": settings.REQUIRE_OTP_VERIFICATION,
            "allowed_sponsor_domains": settings.ALLOWED_SPONSOR_DOMAINS
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

    # OTP Verification check
    dest = (guest_email.strip() if guest_email else None) or (guest_phone.strip() if guest_phone else None)
    otp_verified = False

    if settings.REQUIRE_OTP_VERIFICATION:
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
    
    return templates.TemplateResponse(
        request=request,
        name="view.html",
        context={
            "app_title": settings.APP_TITLE,
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
    profile_manager.delete_profile(profile_id)
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

    # OTP Verification check
    dest = req.guest_email or req.guest_phone
    otp_verified = False

    if settings.REQUIRE_OTP_VERIFICATION:
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


class AdminLoginRequest(CreateGuestRequest.__base__):
    password: str

@app.post("/api/admin/login")
async def api_admin_login(payload: AdminLoginRequest):
    if payload.password == settings.ADMIN_PASSWORD:
        return {"success": True, "message": "Authentification réussie"}
    raise HTTPException(status_code=401, detail="Mot de passe incorrect")
