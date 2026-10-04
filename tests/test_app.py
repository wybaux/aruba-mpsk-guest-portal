import os
import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient

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


