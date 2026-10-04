import logging
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
    AVAILABLE_PROFILES
)
from app.profile_manager import profile_manager, AccessProfileInfo
from app.aruba_client import get_aruba_client
from app.scheduler import CleanupScheduler
from app.qr_generator import generate_qr_code_bytes

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
    version="1.0.0",
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
            "aruba_mode": settings.ARUBA_MODE
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
    note: str = Form(None)
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

    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "Inconnu")

    guest_access = aruba_client.create_guest_pass(
        guest_name=guest_name.strip(),
        duration_hours=duration_hours,
        profile=profile_clean,
        note=note.strip() if note else None,
        client_ip=client_ip,
        user_agent=user_agent,
        terms_accepted=True
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
    
    return templates.TemplateResponse(
        request=request,
        name="view.html",
        context={
            "app_title": settings.APP_TITLE,
            "guest": guest,
            "profile_info": profile_info
        }
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
    acc_pwd = access_password.strip() if (access_password and access_password.strip() and req_pwd) else None

    prof = AccessProfileInfo(
        id=id.strip(),
        name=name.strip(),
        description=description.strip(),
        vlan_id=vlan_id,
        bandwidth_limit_mbps=bw,
        is_default=is_def,
        requires_password=req_pwd,
        access_password=acc_pwd
    )
    profile_manager.save_profile(prof, sync_to_ap=True)
    return RedirectResponse(url="/#profiles", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/profiles/{profile_id}/delete")
async def admin_delete_profile_form(profile_id: str):
    profile_manager.delete_profile(profile_id)
    return RedirectResponse(url="/#profiles", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/guest/{guest_id}/revoke")
async def revoke_guest_form(guest_id: str):
    aruba_client.revoke_guest_pass(guest_id)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/guest/{guest_id}/qr.png")
async def download_qr_image(guest_id: str):
    guest = aruba_client.get_guest_pass(guest_id)
    if not guest:
        raise HTTPException(status_code=404, detail="Accès invité introuvable")
    
    qr_bytes = generate_qr_code_bytes(guest.wifi_string)
    filename = f"wifi-qr-{guest.guest_name.lower().replace(' ', '_')}.png"
    
    return Response(
        content=qr_bytes,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{filename}"'}
    )


# -----------------------------------------------------------------------------
# REST API ENDPOINTS
# -----------------------------------------------------------------------------

@app.get("/api/profiles", response_model=List[dict])
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
                terms_accepted=p.terms_accepted
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
    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "Inconnu")

    guest_access = aruba_client.create_guest_pass(
        guest_name=req.guest_name,
        duration_hours=req.duration_hours,
        profile=req.profile,
        note=req.note,
        client_ip=client_ip,
        user_agent=user_agent,
        terms_accepted=req.terms_accepted
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


class AdminLoginRequest(CreateGuestRequest.__base__):
    password: str

@app.post("/api/admin/login")
async def api_admin_login(payload: AdminLoginRequest):
    if payload.password == settings.ADMIN_PASSWORD:
        return {"success": True, "message": "Authentification réussie"}
    raise HTTPException(status_code=401, detail="Mot de passe incorrect")

