import os
os.environ["ARUBA_MODE"] = "mock"

import pytest
import pyotp
from datetime import datetime, timedelta
from fastapi.testclient import TestClient

from app.config import settings
settings.ARUBA_MODE = "mock"

from app.main import app, aruba_client
from app.aruba_client import MockArubaClient
from app.models import ProfileEnum
from app.db import db
from app.qr_generator import build_wifi_qr_string, generate_qr_code_bytes, generate_qr_code_base64

client = TestClient(app)

@pytest.fixture(autouse=True)
def reset_settings_fixture():
    orig_settings = db.get_all_settings()
    db.set_setting("require_otp_verification", False)
    db.set_setting("require_guest_email", False)
    db.set_setting("allowed_sponsor_domains", "")
    db.set_setting("smtp_host", "")
    db.set_setting("duration_presets", [1, 2, 4, 8, 24])
    db.set_setting("wifi_ssid", "Public-Test")
    db.set_setting("wifi_password", "")
    yield
    for k, v in orig_settings.items():
        db.set_setting(k, v)

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

    # Test AJAX JSON submission for interactive progress bar
    ajax_resp = client.post(
        "/create",
        data={
            "guest_name": "Progress User",
            "duration_hours": "2",
            "profile": "standard",
            "terms_accepted": "on"
        },
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
    )
    assert ajax_resp.status_code == 200
    ajax_data = ajax_resp.json()
    assert ajax_data["success"] is True
    assert "/guest/gst_" in ajax_data["redirect_url"]
    assert ajax_data["guest_id"].startswith("gst_")

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

    # 10. Update user password using 'password' (frontend payload format)
    update_resp = client.put(f"/api/admin/users/{user_id}", json={
        "password": "BrandNewPassword456!",
        "full_name": "Test Opérateur Modifié",
        "mfa_enabled": False
    })
    assert update_resp.status_code == 200
    assert update_resp.json()["full_name"] == "Test Opérateur Modifié"
    assert update_resp.json()["mfa_enabled"] is False

    # Old password must now be rejected
    old_login = client.post("/api/auth/login", json={
        "username": test_uname,
        "password": "SecurePassword123!"
    })
    assert old_login.status_code == 401

    # New password must be accepted
    new_login = client.post("/api/auth/login", json={
        "username": test_uname,
        "password": "BrandNewPassword456!"
    })
    assert new_login.status_code == 200
    assert new_login.json()["success"] is True

    # 10b. Also verify 'new_password' format
    update_new_pwd = client.put(f"/api/admin/users/{user_id}", json={
        "new_password": "EvenNewerPassword789!"
    })
    assert update_new_pwd.status_code == 200
    login_even_newer = client.post("/api/auth/login", json={
        "username": test_uname,
        "password": "EvenNewerPassword789!"
    })
    assert login_even_newer.status_code == 200

    # 11. Delete test user
    del_resp = client.delete(f"/api/admin/users/{user_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["success"] is True

def test_admin_settings_api():
    from unittest.mock import patch, MagicMock

    # 1. Get initial settings
    resp = client.get("/api/admin/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "require_otp_verification" in data
    assert "allowed_sponsor_domains" in data
    assert "smtp_port" in data

    # 2. Toggle OTP policy to True
    toggle_resp1 = client.post("/api/admin/settings/toggle-otp", json={"enabled": True})
    assert toggle_resp1.status_code == 200
    assert toggle_resp1.json()["require_otp_verification"] is True

    # Verify via get
    resp2 = client.get("/api/admin/settings")
    assert resp2.json()["require_otp_verification"] is True

    # 3. Toggle OTP policy back to False
    toggle_resp2 = client.post("/api/admin/settings/toggle-otp", json={"enabled": False})
    assert toggle_resp2.status_code == 200
    assert toggle_resp2.json()["require_otp_verification"] is False

    # 4. Update SMTP and sponsor settings
    update_payload = {
        "smtp_host": "smtp.test-company.local",
        "smtp_port": 587,
        "smtp_user": "wifi-notifier@test-company.local",
        "smtp_password": "supersecretpassword",
        "smtp_from": "Portail Wi-Fi <wifi-notifier@test-company.local>",
        "smtp_tls": True,
        "allowed_sponsor_domains": "test-company.local, partner.org"
    }
    set_resp = client.post("/api/admin/settings", json=update_payload)
    assert set_resp.status_code == 200
    assert set_resp.json()["success"] is True

    # Verify updated settings
    resp3 = client.get("/api/admin/settings")
    s3 = resp3.json()
    assert s3["smtp_host"] == "smtp.test-company.local"
    assert s3["smtp_port"] == 587
    assert s3["smtp_user"] == "wifi-notifier@test-company.local"
    assert s3["has_smtp_password"] is True
    assert s3["allowed_sponsor_domains"] == "test-company.local, partner.org"

    # 5. Test email validation error
    bad_email_resp = client.post("/api/admin/settings/test-email", json={"recipient_email": "invalid-format"})
    assert bad_email_resp.status_code == 400

    # 6. Test email with mocked SMTP server
    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

        test_mail_resp = client.post("/api/admin/settings/test-email", json={"recipient_email": "guest@test-company.local"})
        assert test_mail_resp.status_code == 200
        assert test_mail_resp.json()["success"] is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("wifi-notifier@test-company.local", "supersecretpassword")
        mock_server.send_message.assert_called_once()
        mock_server.quit.assert_called_once()

    # 7. Test duration presets configuration
    # Check default 5 presets
    settings_data = client.get("/api/admin/settings").json()
    assert "duration_presets" in settings_data
    assert len(settings_data["duration_presets"]) == 5

    # Invalid payload: only 3 items
    err_resp1 = client.post("/api/admin/settings/durations", json={"duration_presets": [1, 2, 4]})
    assert err_resp1.status_code == 400

    # Invalid payload: negative or out-of-range hours
    err_resp2 = client.post("/api/admin/settings/durations", json={"duration_presets": [0, 2, 4, 8, 24]})
    assert err_resp2.status_code == 400

    # Valid custom 5 presets: [1, 3, 6, 12, 48]
    custom_resp = client.post("/api/admin/settings/durations", json={"duration_presets": [48, 1, 12, 3, 6]})
    assert custom_resp.status_code == 200
    saved_presets = custom_resp.json()["duration_presets"]
    assert saved_presets == [1, 3, 6, 12, 48]  # Sorted

    # Verify home page renders custom duration pills
    home_resp = client.get("/")
    assert home_resp.status_code == 200
    assert "3h" in home_resp.text
    assert "6h" in home_resp.text
    assert "12h" in home_resp.text
    assert "48h" in home_resp.text

    # Reset back to standard [1, 2, 4, 8, 24]
    reset_resp = client.post("/api/admin/settings/durations", json={"duration_presets": [1, 2, 4, 8, 24]})
    assert reset_resp.status_code == 200
    assert reset_resp.json()["duration_presets"] == [1, 2, 4, 8, 24]

    # 8. Test toggle generation details display policy
    toggle_gen1 = client.post("/api/admin/settings/toggle-gen-details", json={"enabled": False})
    assert toggle_gen1.status_code == 200
    assert toggle_gen1.json()["show_generation_details"] is False

    settings_gen1 = client.get("/api/admin/settings").json()
    assert settings_gen1["show_generation_details"] is False

    toggle_gen2 = client.post("/api/admin/settings/toggle-gen-details", json={"enabled": True})
    assert toggle_gen2.status_code == 200
    assert toggle_gen2.json()["show_generation_details"] is True

    # 9. Test AJAX /create JSON response
    form_create = {
        "guest_name": "Ajax Guest Test",
        "duration_hours": "2",
        "profile": "standard",
        "terms_accepted": "on"
    }
    ajax_resp = client.post("/create", data=form_create, headers={"Accept": "application/json"})
    assert ajax_resp.status_code == 200
    ajax_data = ajax_resp.json()
    assert ajax_data["success"] is True
    assert "guest_id" in ajax_data
    assert "redirect_url" in ajax_data

def test_pass_extension():
    # 1. Create a guest pass
    create_resp = client.post("/api/guests", json={
        "guest_name": "Test Extension Guest",
        "duration_hours": 1,
        "profile": "standard"
    })
    assert create_resp.status_code == 201
    guest_data = create_resp.json()
    guest_id = guest_data["id"]
    original_expiry = datetime.fromisoformat(guest_data["expires_at"])

    # 2. View extension page
    extend_page_resp = client.get(f"/extend/{guest_id}")
    assert extend_page_resp.status_code == 200
    assert "Prolonger" in extend_page_resp.text
    assert "Test Extension Guest" in extend_page_resp.text

    # 3. Extend pass by 3 hours
    extend_api_resp = client.post(f"/api/guests/{guest_id}/extend", json={"additional_hours": 3})
    assert extend_api_resp.status_code == 200
    res_json = extend_api_resp.json()
    assert res_json["success"] is True
    assert res_json["extended_hours"] == 3
    new_expiry = datetime.fromisoformat(res_json["new_expires_at"])
    assert new_expiry > original_expiry
    diff_hours = (new_expiry - original_expiry).total_seconds() / 3600
    assert round(diff_hours, 1) == 3.0

    # 4. Error cases
    bad_id_resp = client.post("/api/guests/nonexistent-id-999/extend", json={"additional_hours": 2})
    assert bad_id_resp.status_code == 404

    bad_hours_resp = client.post(f"/api/guests/{guest_id}/extend", json={"additional_hours": 0})
    assert bad_hours_resp.status_code == 400

def test_live_clients_and_mac_ban():
    # Login as admin to get auth headers / cookies
    login_resp = client.post("/api/auth/login", json={"username": "admin", "password": settings.ADMIN_PASSWORD})
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Get live clients list
    live_resp = client.get("/api/admin/clients/live", headers=headers)
    assert live_resp.status_code == 200
    data = live_resp.json()
    assert "clients" in data
    assert len(data["clients"]) >= 1
    test_client = data["clients"][0]
    target_mac = test_client["mac"]

    # 2. Disconnect client
    dc_resp = client.post("/api/admin/clients/disconnect", json={"mac": target_mac}, headers=headers)
    assert dc_resp.status_code == 200
    assert dc_resp.json()["success"] is True

    # 3. Ban client MAC
    ban_resp = client.post("/api/admin/clients/ban", json={
        "mac": target_mac,
        "ip": test_client.get("ip"),
        "guest_name": test_client.get("guest_name"),
        "reason": "Test Blacklist"
    }, headers=headers)
    assert ban_resp.status_code == 200
    assert ban_resp.json()["success"] is True

    # Check that MAC is recorded as banned in DB
    assert db.is_mac_banned(target_mac) is True
    banned_list = db.list_banned_macs()
    assert any(b["mac"] == target_mac for b in banned_list)

    # 4. Unban client MAC
    unban_resp = client.delete(f"/api/admin/clients/ban/{target_mac}", headers=headers)
    assert unban_resp.status_code == 200
    assert unban_resp.json()["success"] is True
    assert db.is_mac_banned(target_mac) is False

def test_branding_customization():
    login_resp = client.post("/api/auth/login", json={"username": "admin", "password": settings.ADMIN_PASSWORD})
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Get default branding
    b_resp = client.get("/api/admin/branding", headers=headers)
    assert b_resp.status_code == 200
    assert "company_name" in b_resp.json()

    # 2. Update branding
    new_branding = {
        "company_name": "Acme Grand Hotel & Spa",
        "portal_title": "Bienvenue au Wi-Fi Acme",
        "portal_subtitle": "Connectez-vous pour profiter d'un débit ultra-rapide",
        "logo_url": "https://example.com/acme-logo.png",
        "primary_color": "#0ea5e9",
        "charter_text": "Charte personnalisée de l'hôtel Acme."
    }
    update_resp = client.post("/api/admin/branding", json=new_branding, headers=headers)
    assert update_resp.status_code == 200
    assert update_resp.json()["success"] is True

    # 3. Verify home page renders updated branding
    home_resp = client.get("/")
    assert home_resp.status_code == 200
    assert "Acme Grand Hotel &amp; Spa" in home_resp.text or "Acme Grand Hotel & Spa" in home_resp.text
    assert "Bienvenue au Wi-Fi Acme" in home_resp.text
    assert "https://example.com/acme-logo.png" in home_resp.text

    # Reset branding
    default_branding = {
        "company_name": "WIFI Guest",
        "portal_title": "Portail Wi-Fi Invité",
        "portal_subtitle": "Générez un accès temporaire sécurisé",
        "logo_url": "",
        "primary_color": "#171717",
        "charter_text": "Charte légale d'utilisation du réseau Wi-Fi."
    }
    client.post("/api/admin/branding", json=default_branding, headers=headers)

def test_expiry_alert_detection():
    # 1. Create pass via API
    create_resp = client.post("/api/guests", json={
        "guest_name": "Expiring Soon Guest",
        "duration_hours": 1,
        "profile": "standard",
        "guest_email": "expiring@example.com"
    })
    assert create_resp.status_code == 201
    guest_id = create_resp.json()["id"]

    # 2. Manually backdate expires_at so remaining time is 10 minutes
    future_10min = datetime.now() + timedelta(minutes=10)
    import sqlite3
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE guest_passes SET expires_at = ?, expiry_alert_sent = 0 WHERE id = ?", (future_10min.isoformat(), guest_id))
        conn.commit()

    needing_alert = db.get_passes_needing_expiry_alert(threshold_minutes=15)
    matching = [p for p in needing_alert if p["id"] == guest_id]
    assert len(matching) == 1
    assert matching[0]["guest_email"] == "expiring@example.com"

    # 3. Mark as sent
    db.mark_expiry_alert_sent(guest_id)
    needing_alert_after = db.get_passes_needing_expiry_alert(threshold_minutes=15)
    matching_after = [p for p in needing_alert_after if p["id"] == guest_id]
    assert len(matching_after) == 0

def test_multilingual_support():
    from app.main import detect_client_language

    # 1. Verify UI contains language dropdown and Portuguese support
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="langDropdown"' in resp.text
    assert 'setLanguage(' in resp.text
    assert 'data-i18n=' in resp.text
    assert "setLanguage('pt')" in resp.text
    assert "Português" in resp.text
    assert "detectClientDeviceLanguage" in resp.text
    assert "resetToAutoLanguage" in resp.text

    # 2. Test detect_client_language helper with various Accept-Language headers
    assert detect_client_language("pt-BR,pt;q=0.9,en;q=0.8", "fr") == "pt"
    assert detect_client_language("pt-PT,pt;q=0.9", "fr") == "pt"
    assert detect_client_language("en-US,en;q=0.9", "fr") == "en"
    assert detect_client_language("es-ES,es;q=0.8", "fr") == "es"
    assert detect_client_language("de-DE,de;q=0.8", "fr") == "de"
    assert detect_client_language("fr-FR,fr;q=0.9", "en") == "fr"
    # Unsupported language falls back to default
    assert detect_client_language("it-IT,it;q=0.9", "pt") == "pt"
    assert detect_client_language(None, "de") == "de"

    # 3. Test admin default language API
    settings_resp = client.get("/api/admin/settings")
    assert settings_resp.status_code == 200
    assert "default_language" in settings_resp.json()

    # Set default language to pt
    set_pt_resp = client.post("/api/admin/settings/default-language", json={"language": "pt"})
    assert set_pt_resp.status_code == 200
    assert set_pt_resp.json()["default_language"] == "pt"

    # Verify updated setting
    assert client.get("/api/admin/settings").json()["default_language"] == "pt"

    # Test invalid language rejected
    bad_lang_resp = client.post("/api/admin/settings/default-language", json={"language": "invalid_lang"})
    assert bad_lang_resp.status_code == 400

    # Reset back to fr
    reset_resp = client.post("/api/admin/settings/default-language", json={"language": "fr"})
    assert reset_resp.status_code == 200
    assert reset_resp.json()["default_language"] == "fr"

def test_guest_email_requirement():
    # 1. Initial state: require_guest_email is False
    settings_resp = client.get("/api/admin/settings")
    assert settings_resp.status_code == 200
    assert settings_resp.json()["require_guest_email"] is False

    # Guest can generate pass without email
    resp_anon = client.post("/create", data={
        "guest_name": "Anonymous Visitor",
        "duration_hours": 1,
        "profile": "standard",
        "terms_accepted": "on"
    }, follow_redirects=False)
    assert resp_anon.status_code in (200, 303)

    # 2. Toggle require_guest_email to True
    toggle_resp = client.post("/api/admin/settings/toggle-require-email", json={"enabled": True})
    assert toggle_resp.status_code == 200
    assert toggle_resp.json()["success"] is True
    assert toggle_resp.json()["require_guest_email"] is True

    # Verify updated setting via GET
    assert client.get("/api/admin/settings").json()["require_guest_email"] is True

    # 3. Check UI reflects mandatory status and required HTML attribute
    home_resp = client.get("/")
    assert home_resp.status_code == 200
    assert 'data-i18n="mandatory"' in home_resp.text
    assert 'id="guest_email"' in home_resp.text
    assert 'required' in home_resp.text

    # 4. Attempt to create guest without email via /api/guests -> should fail with 400
    api_fail_resp = client.post("/api/guests", json={
        "guest_name": "No Email Visitor",
        "duration_hours": 2,
        "profile": "standard",
        "terms_accepted": True
    })
    assert api_fail_resp.status_code == 400
    assert "email est obligatoire" in api_fail_resp.json()["detail"]

    # 5. Attempt to create guest without email via /create form -> should fail with 400
    form_fail_resp = client.post("/create", data={
        "guest_name": "No Email Form",
        "duration_hours": 1,
        "profile": "standard",
        "terms_accepted": "on",
        "guest_email": ""
    })
    assert form_fail_resp.status_code == 400
    assert "email est obligatoire" in form_fail_resp.json()["detail"]

    # 6. Create guest with valid email -> succeeds
    api_ok_resp = client.post("/api/guests", json={
        "guest_name": "Valid Email Visitor",
        "duration_hours": 2,
        "profile": "standard",
        "terms_accepted": True,
        "guest_email": "guest@testcorp.com"
    })
    assert api_ok_resp.status_code == 201
    assert api_ok_resp.json()["guest_email"] == "guest@testcorp.com"

    form_ok_resp = client.post("/create", data={
        "guest_name": "Valid Email Form",
        "duration_hours": 1,
        "profile": "standard",
        "terms_accepted": "on",
        "guest_email": "guest2@testcorp.com"
    }, follow_redirects=False)
    assert form_ok_resp.status_code in (200, 303)

    # 7. Toggle back to False
    toggle_off_resp = client.post("/api/admin/settings/toggle-require-email", json={"enabled": False})
    assert toggle_off_resp.status_code == 200
    assert toggle_off_resp.json()["require_guest_email"] is False
    assert client.get("/api/admin/settings").json()["require_guest_email"] is False

def test_aruba_vc_settings_and_test_connection():
    from unittest.mock import patch, MagicMock

    # 1. Verify GET /api/admin/settings contains VC configuration fields
    settings_resp = client.get("/api/admin/settings")
    assert settings_resp.status_code == 200
    data = settings_resp.json()
    assert "aruba_mode" in data
    assert "aruba_instant_host" in data
    assert "aruba_instant_username" in data
    assert "aruba_instant_verify_ssl" in data
    assert "aruba_mpsk_profile" in data
    assert "has_vc_password" in data

    # 2. Update VC settings via POST /api/admin/settings
    update_payload = {
        "aruba_mode": "instant",
        "aruba_instant_host": "192.168.10.100",
        "aruba_instant_username": "aruba_admin",
        "aruba_instant_password": "SuperSecretArubaPassword!",
        "aruba_instant_verify_ssl": False,
        "aruba_mpsk_profile": "Corp-Guests"
    }
    set_resp = client.post("/api/admin/settings", json=update_payload)
    assert set_resp.status_code == 200
    assert set_resp.json()["success"] is True

    # 3. Check GET returns updated values and password is masked
    resp_updated = client.get("/api/admin/settings")
    assert resp_updated.status_code == 200
    u_data = resp_updated.json()
    assert u_data["aruba_mode"] == "instant"
    assert u_data["aruba_instant_host"] == "192.168.10.100"
    assert u_data["aruba_instant_username"] == "aruba_admin"
    assert u_data["aruba_instant_verify_ssl"] is False
    assert u_data["aruba_mpsk_profile"] == "Corp-Guests"
    assert u_data["has_vc_password"] is True
    assert "aruba_instant_password" not in u_data

    # 4. Update without password -> preserves existing password
    update_payload2 = {
        "aruba_instant_host": "192.168.10.101"
    }
    set_resp2 = client.post("/api/admin/settings", json=update_payload2)
    assert set_resp2.status_code == 200
    resp_updated2 = client.get("/api/admin/settings")
    assert resp_updated2.json()["aruba_instant_host"] == "192.168.10.101"
    assert resp_updated2.json()["has_vc_password"] is True

    # 5. Test test-vc endpoint in mock mode
    mock_test = client.post("/api/admin/settings/test-vc", json={"mode": "mock"})
    assert mock_test.status_code == 200
    assert mock_test.json()["success"] is True
    assert "Simulation" in mock_test.json()["message"]

    # 6. Test test-vc endpoint in central mode
    central_test = client.post("/api/admin/settings/test-vc", json={"mode": "central"})
    assert central_test.status_code == 200
    assert central_test.json()["success"] is True
    assert "Central" in central_test.json()["message"]

    # 7. Test test-vc endpoint in instant mode with simulated SSH success
    with patch("socket.create_connection") as mock_conn, \
         patch("paramiko.SSHClient") as mock_ssh_cls:
        mock_conn.return_value.__enter__.return_value = MagicMock()
        mock_ssh = MagicMock()
        mock_ssh_cls.return_value = mock_ssh
        mock_chan = MagicMock()
        mock_chan.recv.return_value = b"Instant-AP# "
        mock_ssh.invoke_shell.return_value = mock_chan

        instant_test = client.post("/api/admin/settings/test-vc", json={
            "mode": "instant",
            "host": "192.168.10.101",
            "username": "aruba_admin",
            "password": "SuperSecretArubaPassword!"
        })
        assert instant_test.status_code == 200
        assert instant_test.json()["success"] is True
        assert "Virtual Controller" in instant_test.json()["message"]

    # 8. Check home page renders VC info card
    home_resp = client.get("/")
    assert home_resp.status_code == 200
    assert "192.168.10.101" in home_resp.text
    assert "Corp-Guests" in home_resp.text

    # 9. Clean up and restore mock mode
    reset_resp = client.post("/api/admin/settings", json={"aruba_mode": "mock"})
    assert reset_resp.status_code == 200

def test_wifi_ssid_and_password_settings():
    """Verify configuring SSID and fixed vs dynamic MPSK keys via web API."""
    # 1. Read default settings
    res = client.get("/api/admin/settings")
    assert res.status_code == 200
    data = res.json()
    assert "wifi_ssid" in data
    assert "wifi_password" in data

    # 2. Update SSID and set a fixed static Wi-Fi password
    update_res = client.post("/api/admin/settings", json={
        "wifi_ssid": "VIP-Conference-WiFi",
        "wifi_password": "MySuperSecretKey2026!"
    })
    assert update_res.status_code == 200
    assert update_res.json()["success"] is True

    # Check updated settings
    res2 = client.get("/api/admin/settings")
    data2 = res2.json()
    assert data2["wifi_ssid"] == "VIP-Conference-WiFi"
    assert data2["wifi_password"] == "MySuperSecretKey2026!"

    # 3. Create a guest pass with fixed password
    create_res = client.post("/create", data={
        "guest_name": "VIP Guest 1",
        "duration_hours": 2,
        "profile": "standard",
        "terms_accepted": "true"
    }, headers={"X-Requested-With": "XMLHttpRequest"})
    assert create_res.status_code == 200
    guest_id = create_res.json()["guest_id"]
    pass_data = db.get_pass(guest_id)
    assert pass_data is not None
    assert pass_data["ssid"] == "VIP-Conference-WiFi"
    assert pass_data["password"] == "MySuperSecretKey2026!"

    # 4. Check home page renders new SSID
    home_resp = client.get("/")
    assert home_resp.status_code == 200
    assert "VIP-Conference-WiFi" in home_resp.text

    # 5. Clear static password (reverting to dynamic MPSK individual random passwords)
    clear_res = client.post("/api/admin/settings", json={
        "wifi_ssid": "VIP-Conference-WiFi",
        "wifi_password": ""
    })
    assert clear_res.status_code == 200

    create_res2 = client.post("/create", data={
        "guest_name": "VIP Guest 2",
        "duration_hours": 2,
        "profile": "standard",
        "terms_accepted": "true"
    }, headers={"X-Requested-With": "XMLHttpRequest"})
    assert create_res2.status_code == 200
    guest_id2 = create_res2.json()["guest_id"]
    pass_data2 = db.get_pass(guest_id2)
    assert pass_data2 is not None
    assert pass_data2["ssid"] == "VIP-Conference-WiFi"
    # Individual random 10-char password generated
    assert pass_data2["password"] != "MySuperSecretKey2026!"
    assert len(pass_data2["password"]) == 10








