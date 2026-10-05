# Enterprise & Homelab Guest Wi-Fi (Aruba Instant & Central MPSK)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Tests: Pytest](https://img.shields.io/badge/tests-pytest-green.svg)](tests/test_app.py)
[![Design: Minimalist White](https://img.shields.io/badge/design-minimalist%20white-black.svg)]()

A complete, secure, and modern guest Wi-Fi portal featuring dynamic **Multiple Pre-Shared Key (MPSK)** provisioning, hardware per-user bandwidth rate-limiting, and instant QR code onboarding on **Aruba Instant APs (IAP)** and **Aruba Central**.

---

## 🌟 Key Features

* 🔑 **Unique Wi-Fi Passphrase per Guest (MPSK)**: Each visitor receives their own unique, temporary WPA2/WPA3 credential generated on the fly. No shared static passwords or handwritten post-it notes.
* 📱 **Instant Camera QR Code Connection**: Scannable out-of-the-box by native iOS & Android cameras using universal ZXing standard (`WIFI:T:WPA;S:...;P:...;;`).
* ⚡ **Hardware Rate-Limiting (Aruba ASIC Shaper)**: Dedicated Aruba user roles enforce fair, strict per-client upload/download limits (`bandwidth-limit peruser`), processed directly by the AP hardware.
* 🌐 **Automatic Device Language Adaptation & Multilingual Support**:
  * Automatically detects client device language (`navigator.languages` & `Accept-Language` header).
  * 5 languages fully supported: **English 🇬🇧, French 🇫🇷, Portuguese 🇵🇹, Spanish 🇪🇸, and German 🇩🇪**.
  * Configurable default fallback language in Admin Settings with one-click quick switch.
* ⏱️ **Live Generation Feedback & Details Toggle**:
  * Real-time progress bar with live elapsed stopwatch badge.
  * Collapsible 4-step provisioning details with visitor preference remembered in `localStorage`.
  * Global default display toggle (Detailed vs. Clean Minimalist) manageable in Admin Settings.
* 🔒 **Access Profiles & Protected Tiers**:
  * **Default tier**: Zero friction, instant access for typical guests.
  * **VIP / High-speed tiers**: Protected by custom access codes (e.g., streaming teams, conference speakers, VIP guests).
  * **Dynamic AP role binding**: Synchronizes MPSK profiles and VLAN assignment (e.g. VLAN 190) via SSH.
* ✉️ **OTP Identity Verification (Email / SMS)**:
  * Optional or mandatory guest email validation via 6-digit one-time passcodes.
  * Automatic delivery of passes and printable boarding vouchers to the visitor's inbox.
* 🏢 **Corporate Host Sponsorship**:
  * Internal employee sponsorship with company email domain enforcement (`@company.com`).
  * Automatic carbon-copy of the guest pass sent to the employee sponsor.
* 🎨 **White-Label & Custom Branding**:
  * Customize company name, portal title, subtitle, logo URL, primary accent color, and Wi-Fi terms directly from the Admin panel.
* 🛡️ **Role-Based Access Control (RBAC) & 2FA**:
  * Granular roles: **Admin** (full rights), **Operator** (pass management), and **Auditor** (read-only compliance).
  * Two-Factor Authentication via TOTP (compatible with Google Authenticator, Microsoft Authenticator, 1Password).
* 📡 **Real-Time Connected Clients & 1-Click MAC Ban**:
  * Live monitoring of connected clients (AP name, RSSI dBm, RX/TX traffic, duration).
  * Instant hardware kick / disconnect and permanent MAC hardware blacklisting.
* 📋 **Legal Compliance & Audit Trail**:
  * Mandatory traceability recording client IP, exact timestamps, MAC address, sponsor, and issued key.
  * Integrated terms of use acceptance modal compliant with data retention laws and GDPR.
  * One-click compliant Excel UTF-8 CSV export.
* ⏱️ **Automated Pass Expiry & Background Cleanup**:
  * Background worker revoking expired credentials on Aruba hardware every minute.
  * Automatic 15-minute expiration warning emails with one-click renewal links.
* 🤍 **Pure-White Minimalist Design**:
  * Clean UI engineered with *Plus Jakarta Sans*, *JetBrains Mono*, Tailwind CSS, and Lucide icons.
  * Responsive layout optimized for smartphones, desktop kiosks, and thermal/A4 printing.

---

## 📁 Project Structure

```text
├── .github/
│   ├── workflows/
│   │   └── ci.yml               # Automated CI test pipeline (Python 3.10-3.14)
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.md        # Bug report template
│   │   └── feature_request.md   # Feature request template
│   └── PULL_REQUEST_TEMPLATE.md # Pull request template
├── app/
│   ├── aruba_client.py          # Aruba drivers (CLI SSH Instant AP, Central API, Mock)
│   ├── auth_service.py          # RBAC authentication & TOTP 2FA engine
│   ├── config.py                # Environment configuration (Pydantic Settings)
│   ├── db.py                    # SQLite persistence, audit trails, and settings storage
│   ├── main.py                  # FastAPI application & REST/Web routes
│   ├── models.py                # Pydantic data schemas
│   ├── notification_service.py  # SMTP notification & OTP email delivery
│   ├── profile_manager.py       # Access profiles & AP bandwidth mapping
│   ├── qr_generator.py          # ZXing Wi-Fi QR code generator
│   └── templates/
│       ├── base.html            # Base layout with I18N (EN/FR/PT/ES/DE)
│       └── index.html           # Guest portal & unified admin console
├── tests/
│   └── test_app.py              # Full unit and integration test suite
├── ARUBA_IAP_CONFIGURATION.md   # Complete step-by-step Aruba Instant AP setup guide
├── aruba_iap_setup.cli          # Ready-to-use CLI script for Aruba terminal (conf t)
├── Dockerfile                   # Production container definition
├── docker-compose.yml           # One-click Docker deployment
├── requirements.txt             # Python dependencies
├── profiles.json                # Default access profile specifications
├── .env.example                 # Sanitized environment template
├── CONTRIBUTING.md              # Contributor guidelines
└── LICENSE                      # MIT Open Source License
```

---

## ⚙️ Aruba Access Point Configuration

For network configuration and Aruba Instant AP parameters:

* 📖 **[ARUBA_IAP_CONFIGURATION.md](./ARUBA_IAP_CONFIGURATION.md)**: Detailed step-by-step guide (CLI, WebUI, firewall ACLs, VLANs, and verification commands).
* ⚙️ **[aruba_iap_setup.cli](./aruba_iap_setup.cli)**: Ready-to-paste CLI configuration script (`conf t`).

---

## 🚀 Quick Start

### 1. Clone repository & set up Python environment

```bash
git clone https://github.com/wybaux/aruba-mpsk-guest-portal.git
cd aruba-mpsk-guest-portal

# Create virtual environment
python -m venv .venv

# Activate environment:
# On Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# On Linux / macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and update your settings:

```bash
cp .env.example .env
```

Key configuration parameters in `.env`:
```dotenv
# Application
APP_TITLE=Wi-Fi Guest Portal
APP_HOST=0.0.0.0
APP_PORT=8000
SECRET_KEY=change_this_secret_key_in_production
ADMIN_PASSWORD=your_admin_password

# Wi-Fi SSID
WIFI_SSID=Public-Test

# Localization
DEFAULT_LANGUAGE=en  # Options: en, fr, pt, es, de

# Aruba Mode (instant = physical AP via SSH, mock = offline local simulation)
ARUBA_MODE=instant
ARUBA_INSTANT_HOST=https://10.10.30.4:4343
ARUBA_INSTANT_USERNAME=admin
ARUBA_INSTANT_PASSWORD=your_ap_password
ARUBA_INSTANT_VERIFY_SSL=false
ARUBA_MPSK_PROFILE=MPSK_GUEST
```

### 3. Run the application

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open your browser:
* **Guest Portal**: `http://localhost:8000/`
* **Admin Dashboard**: `http://localhost:8000/#users`
* **Interactive OpenAPI Specs**: `http://localhost:8000/docs`

---

## 🐳 Docker Deployment

To launch the portal using Docker:

```bash
docker compose up -d
```

---

## 🧪 Running Tests

Execute the complete test suite (includes full mock Aruba environment, RBAC, localization, and rate shaping tests):

```bash
python -m pytest tests/test_app.py -v
```

---

## 🤝 Contributing

Contributions are welcome! Please check **[CONTRIBUTING.md](./CONTRIBUTING.md)** for development guidelines and code standards.

---

## 📄 License

This project is licensed under the MIT License. See **[LICENSE](./LICENSE)** for details.
