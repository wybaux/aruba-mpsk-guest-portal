import os
os.environ["ARUBA_MODE"] = "mock"

import pytest
import pyotp
from datetime import datetime, timedelta
from fastapi.testclient import TestClient

from app.config import settings
settings.ARUBA_MODE = "mock"

from app.main import app
from app.aruba_client import MockArubaClient
from app.models import ProfileEnum
from app.qr_generator import build_wifi_qr_string, generate_qr_code_bytes, generate_qr_code_base64

client = TestClient(app)

def test_qr_generator():
    wifi_str = build_wifi_qr_string("TestSSID", "SecretPass123", "WPA")
    assert "S:TestSSID;" in wifi_str
    assert "P:SecretPass123;" in wifi_str
    
    qr_bytes = generate_qr_code_bytes(wifi_str)
    assert len(qr_bytes) > 0
    assert qr_bytes.startswith(b"\x89PNG")
    
    qr_b64 = generate_qr_code_base64(wifi_str)
    assert qr_b64.startswith("data:image/png;base64,")

def test_mock_aruba_client(tmp_path):
    db_file = tmp_path / "test_passes.json"
    mock_client = MockArubaClient(db_path=str(db_file))
    
    # Create pass
    guest = mock_client.create_guest_pass(
        guest_name="Alice",
        duration_hours=1.0,
        profile=ProfileEnum.STANDARD,
        note="Test Note"
    )
    
    assert guest.guest_name == "Alice"
    assert guest.is_active is True
    assert guest.remaining_seconds > 0
    
    # Get pass
    fetched = mock_client.get_guest_pass(guest.id)
    assert fetched is not None
    assert fetched.guest_name == "Alice"
    
    # List active
    active = mock_client.list_active_passes()
    assert len(active) == 1
    
    # Revoke
    revoked = mock_client.revoke_guest_pass(guest.id)
    assert revoked is True
    assert len(mock_client.list_active_passes()) == 0

def test_api_endpoints():
    # 1. Get profiles
    resp = client.get("/api/profiles")
    assert resp.status_code == 200
    profiles = resp.json()
    assert len(profiles) >= 3
    
    # 2. Create guest with protected profile without password -> 403 Forbidden
    payload_no_pwd = {
        "guest_name": "Bob Marley",
        "duration_hours": 2,
        "profile": "vip",
        "note": "Concert guest"
    }
    resp_fail = client.post("/api/guests", json=payload_no_pwd)
    assert resp_fail.status_code == 403

    # 3. Verify profile unlock endpoint
    resp_verif_fail = client.post("/api/profiles/vip/verify", json={"password": "wrong"})
    assert resp_verif_fail.status_code == 401

    resp_verif_ok = client.post("/api/profiles/vip/verify", json={"password": "admin123"})
    assert resp_verif_ok.status_code == 200
    assert resp_verif_ok.json()["valid"] is True

    # 4. Create guest with authorization password -> 201 Created
    payload_with_pwd = {
        "guest_name": "Bob Marley",
        "duration_hours": 2,
        "profile": "vip",
        "profile_password": "admin123",
        "note": "Concert guest"
    }
    resp = client.post("/api/guests", json=payload_with_pwd)
    assert resp.status_code == 201
    data = resp.json()
    guest_id = data["id"]
    assert data["guest_name"] == "Bob Marley"
    assert data["vlan_id"] == 190
    
    # 3. List guests
    resp = client.get("/api/guests")
    assert resp.status_code == 200
    summaries = resp.json()
    assert any(g["id"] == guest_id for g in summaries)
    
    # 4. Get QR PNG
    resp = client.get(f"/guest/{guest_id}/qr.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    
    # 5. Revoke guest
    resp = client.delete(f"/api/guests/{guest_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

def test_web_frontend_routes():
    # Home dashboard HTML
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Wi-Fi" in resp.text
    
    # Submit form
    form_data = {
        "guest_name": "Charlie",
        "duration_hours": "5",
        "profile": "restricted",
        "terms_accepted": "on",
        "note": "Web test"
    }
    resp = client.post("/create", data=form_data, follow_redirects=False)
    assert resp.status_code == 303
    redirect_url = resp.headers["location"]
    
    # Follow redirect to view voucher
    resp = client.get(redirect_url)
    assert resp.status_code == 200
    assert "Charlie" in resp.text
    assert "Charte" in resp.text
    assert "VLAN 190" in resp.text

def test_admin_login():
    from app.config import settings
    # Test valid login
    resp = client.post("/api/admin/login", json={"password": settings.ADMIN_PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # Test invalid login
    resp = client.post("/api/admin/login", json={"password": "wrong_password_123"})
    assert resp.status_code == 401

def test_profile_management():
    # 1. Create a new custom profile with password protection via API
    custom_profile = {
        "id": "gaming_vip",
        "name": "Gaming VIP",
        "description": "Latence ultra-faible",
        "vlan_id": 190,
        "bandwidth_limit_mbps": 100,
        "is_default": False,
        "requires_password": True,
        "access_password": "gaming_secret"
    }
    resp = client.post("/api/profiles", json=custom_profile)
    assert resp.status_code == 201
    assert resp.json()["id"] == "gaming_vip"
    assert resp.json()["bandwidth_limit_mbps"] == 100
    assert resp.json()["requires_password"] is True
    assert resp.json()["access_password"] == "gaming_secret"

    # Verify custom password unlocks it
    resp_pw = client.post("/api/profiles/gaming_vip/verify", json={"password": "gaming_secret"})
    assert resp_pw.status_code == 200

    # 2. Check profile appears in list
    resp = client.get("/api/profiles")
    assert resp.status_code == 200
    profiles = {p["id"]: p for p in resp.json()}
    assert "gaming_vip" in profiles
    assert profiles["gaming_vip"]["role_name"] == "Guest-GamingVip"
    assert profiles["gaming_vip"]["requires_password"] is True
    assert profiles["gaming_vip"]["has_password"] is True

    # 3. Test HTML form creation/update
    form_data = {
        "id": "gaming_vip",
        "name": "Gaming VIP Updated",
        "description": "Latence minimale et débit prioritaire",
        "vlan_id": 190,
        "bandwidth_limit_mbps": "120",
        "requires_password": "on",
        "access_password": "updated_secret"
    }
    resp = client.post("/admin/profiles", data=form_data, follow_redirects=False)
    assert resp.status_code == 303

    # Check updated profile
    resp = client.get("/api/profiles")
    profiles = {p["id"]: p for p in resp.json()}
    assert profiles["gaming_vip"]["name"] == "Gaming VIP Updated"
    assert profiles["gaming_vip"]["bandwidth_limit_mbps"] == 120

    # 4. Delete the custom profile
    resp = client.delete("/api/profiles/gaming_vip")
    assert resp.status_code == 200

    # Verify deletion
    resp = client.get("/api/profiles")
    profiles = {p["id"]: p for p in resp.json()}
    assert "gaming_vip" not in profiles

def test_otp_and_sponsorship():
    # 1. Request OTP
    dest = "visiteur@example.com"
    resp = client.post("/api/otp/request", json={"destination": dest, "type": "email"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    demo_code = data.get("demo_code")
    assert demo_code is not None
    assert len(demo_code) == 6

    # 2. Verify invalid OTP
    resp_invalid = client.post("/api/otp/verify", json={"destination": dest, "code": "000000"})
    assert resp_invalid.status_code == 400

    # 3. Verify valid OTP
    resp_valid = client.post("/api/otp/verify", json={"destination": dest, "code": demo_code})
    assert resp_valid.status_code == 200
    assert resp_valid.json()["success"] is True

    # 4. Create sponsored guest pass via API
    payload = {
        "guest_name": "Jean Dupont",
        "duration_hours": 4,
        "profile": "standard",
        "guest_email": dest,
        "sponsor_name": "Alice Martin",
        "sponsor_email": "alice.martin@entreprise.com",
        "send_email_voucher": True,
        "terms_accepted": True
    }
    resp_guest = client.post("/api/guests", json=payload)
    assert resp_guest.status_code == 201
    guest_data = resp_guest.json()
    assert guest_data["guest_name"] == "Jean Dupont"
    assert guest_data["sponsor_name"] == "Alice Martin"
    assert guest_data["guest_email"] == dest

    # 5. Send voucher email via API
    resp_email = client.post(f"/api/guests/{guest_data['id']}/send-email", json={"recipient_email": "test@dest.com"})
    assert resp_email.status_code == 200
    assert resp_email.json()["success"] is True

    # 6. Test web send voucher email form route
    resp_web_email = client.post(f"/guest/{guest_data['id']}/send-email", data={"email": "autre@dest.com"}, follow_redirects=False)
    assert resp_web_email.status_code == 303
    assert "email_sent=true" in resp_web_email.headers["location"]

def test_legal_audit_export():
    # 1. Export as CSV
    resp_csv = client.get("/api/admin/audit/export?format=csv")
    assert resp_csv.status_code == 200
    assert "text/csv" in resp_csv.headers["content-type"]
    assert "Content-Disposition" in resp_csv.headers
    assert "registre_audit_wifi" in resp_csv.headers["Content-Disposition"]
    assert "Nom_Invite" in resp_csv.text
    assert "Parrain_Nom" in resp_csv.text
    assert "Charte_Acceptee" in resp_csv.text

    # 2. Export as JSON
    resp_json = client.get("/api/admin/audit/export?format=json")
    assert resp_json.status_code == 200
    assert isinstance(resp_json.json(), list)

def test_health_and_metrics():
    # 1. /health
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "uptime_seconds" in data
    assert "database" in data
    assert data["database"]["status"] == "connected"
    assert "active_passes" in data["database"]
    assert "total_users" in data["database"]

    # 2. /metrics
    resp_metrics = client.get("/metrics")
    assert resp_metrics.status_code == 200
    assert "text/plain" in resp_metrics.headers["content-type"]
    text = resp_metrics.text
    assert "wifi_guest_uptime_seconds" in text
    assert "wifi_guest_active_passes" in text
    assert "wifi_guest_total_passes" in text
    assert "wifi_guest_total_users" in text
    assert "wifi_guest_db_size_bytes" in text

import uuid

def test_rbac_user_management_and_mfa():
    test_uname = f"op_{uuid.uuid4().hex[:6]}"
    # 1. Create a new operator user
    user_payload = {
        "username": test_uname,
        "full_name": "Test Opérateur",
        "email": f"{test_uname}@example.com",
        "role": "operator",
        "password": "SecurePassword123!",
        "is_active": True
    }
    resp = client.post("/api/admin/users", json=user_payload)
    assert resp.status_code == 201
    created_user = resp.json()
    user_id = created_user["id"]
    assert created_user["username"] == test_uname
    assert created_user["role"] == "operator"
    assert created_user["mfa_enabled"] is False

    # 2. List users and verify existence
    resp_users = client.get("/api/admin/users")
    assert resp_users.status_code == 200
    usernames = [u["username"] for u in resp_users.json()]
    assert test_uname in usernames

    # 3. Test Step 1 Login with direct password (before 2FA)
    login_step1 = client.post("/api/auth/login", json={
        "username": test_uname,
        "password": "SecurePassword123!"
    })
    assert login_step1.status_code == 200
    l_data = login_step1.json()
    assert l_data["success"] is True
    assert l_data["mfa_required"] is False
    token = l_data["access_token"]
    assert token is not None

    # 4. Verify /api/auth/me with Bearer token
    me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["username"] == test_uname

    # 5. Setup TOTP 2FA for this user
    setup_resp = client.post(f"/api/admin/users/{user_id}/totp-setup")
    assert setup_resp.status_code == 200
    s_data = setup_resp.json()
    assert "secret" in s_data
    assert "qr_code_base64" in s_data
    assert "manual_code" in s_data
    secret = s_data["secret"]

    # 6. Enable TOTP using valid generated code
    totp = pyotp.TOTP(secret)
    valid_code = totp.now()
    enable_resp = client.post(f"/api/admin/users/{user_id}/totp-enable", json={"code": valid_code})
    assert enable_resp.status_code == 200
    assert enable_resp.json()["success"] is True

    # 7. Now Step 1 login must challenge for MFA!
    mfa_login_resp = client.post("/api/auth/login", json={
        "username": test_uname,
        "password": "SecurePassword123!"
    })
    assert mfa_login_resp.status_code == 200
    mfa_step1 = mfa_login_resp.json()
    assert mfa_step1["mfa_required"] is True
    temp_token = mfa_step1["temp_token"]
    assert temp_token is not None

    # 8. Step 2 MFA verification with invalid code -> 401
    invalid_verify = client.post("/api/auth/mfa-verify", json={
        "temp_token": temp_token,
        "code": "000000"
    })
    assert invalid_verify.status_code == 401

    # 9. Step 2 MFA verification with valid TOTP code -> 200 and access_token
    valid_verify = client.post("/api/auth/mfa-verify", json={
        "temp_token": temp_token,
        "code": totp.now()
    })
    assert valid_verify.status_code == 200
    v_data = valid_verify.json()
    assert v_data["success"] is True
    assert "access_token" in v_data
    assert v_data["user"]["username"] == test_uname

    # 10. Update user (change full_name and deactivate MFA)
    update_resp = client.put(f"/api/admin/users/{user_id}", json={
        "full_name": "Test Opérateur Modifié",
        "mfa_enabled": False
    })
    assert update_resp.status_code == 200
    assert update_resp.json()["full_name"] == "Test Opérateur Modifié"
    assert update_resp.json()["mfa_enabled"] is False

    # 11. Delete test user
    del_resp = client.delete(f"/api/admin/users/{user_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["success"] is True



