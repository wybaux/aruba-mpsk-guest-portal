# Wi-Fi Invités Homelab & Entreprise (Aruba Instant MPSK)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Tests: Pytest](https://img.shields.io/badge/tests-pytest-green.svg)](tests/test_app.py)
[![Code Style: Clean](https://img.shields.io/badge/design-minimalist%20white-black.svg)]()

Solution complète, sécurisée et épurée de gestion des accès Wi-Fi invités avec provisionnement dynamique de clés uniques **MPSK (Multiple Pre-Shared Key)** et limitation matérielle de débit par utilisateur sur bornes **Aruba Instant AP (IAP)** ou **Aruba Central**.

---

## 🌟 Fonctionnalités Principales

* 🔑 **Clé Wi-Fi unique par invité (MPSK)** : Chaque visiteur reçoit sa propre clé WPA2/WPA3 personnelle générée à la volée. Fini le mot de passe Wi-Fi unique partagé ou affiché sur un post-it.
* 📱 **Connexion par QR Code instantanée** : Flashable nativement par l'appareil photo iOS & Android au format universel ZXing (`WIFI:T:WPA;S:...;P:...;;`).
* ⚡ **Limitation matérielle du débit (Hardware Shaper ASIC)** : Rôles Aruba dédiés appliquant un bridage strict et équitable par utilisateur (`bandwidth-limit peruser`), géré directement par le contrôleur de la borne.
* 🔒 **Politique des Profils & Autorisations** :
  * **Profil par défaut** : Présélectionné et libre d'accès pour tous les invités sans code.
  * **Profils protégés** : Accès restreint par mot de passe spécifique (ex: invités VIP, équipes de tournage, streaming) ou réservé aux administrateurs.
  * **Gestion Admin complète** : Création, modification et suppression des profils et débits synchronisés en direct sur la borne Aruba via SSH.
* ✉️ **Vérification d'Identité par Code OTP (Email / SMS)** :
  * Validation facultative ou obligatoire de l'identité du visiteur via un code de sécurité éphémère à 6 chiffres.
  * Envoi direct du pass et du QR Code par e-mail au visiteur.
* 🏢 **Parrainage Interne d'Entreprise (Corporate Sponsorship)** :
  * Déclaration du collaborateur hôte (nom et email pro) avec validation du nom de domaine d'entreprise (`@societe.com`).
  * Envoi automatique d'une copie du billet d'accès au collaborateur parrain.
* 📋 **Conformité Légale & Traçabilité (Registre d'Audit)** :
  * **Traçabilité obligatoire** : Enregistrement de l'adresse IP source, horodatage, User-Agent, email/mobile, parrain et clé émise.
  * **Charte d'utilisation intégrée** : Validation obligatoire avant génération du pass (respect RGPD, décret sur la conservation des données).
  * **Export Légal en un clic (CSV)** : Téléchargement du registre d'audit conforme avec encodage UTF-8 Excel.
* ⏱️ **Révocation automatique** : Tâche de fond périodique révoquant les clés expirées sur l'AP à la minute près.
* 🎨 **Design Épuré Blanc Minimaliste** : Interface moderne, typographie *Plus Jakarta Sans* / *JetBrains Mono*, responsive, avec vue « Billet d'embarquement » optimisée pour smartphone, impression et expédition email.

---

## 📁 Structure du Projet

```text
├── .github/
│   ├── workflows/
│   │   └── ci.yml               # Pipeline CI GitHub Actions (Tests automatisés Python 3.10-3.13)
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.md        # Modèle de rapport de bug
│   │   └── feature_request.md   # Modèle de demande de fonctionnalité
│   └── PULL_REQUEST_TEMPLATE.md # Modèle de Pull Request
├── app/
│   ├── aruba_client.py          # Pilotes Aruba (CLI SSH Instant AP, Aruba Central REST API, Mock)
│   ├── config.py                # Configuration & variables d'environnement (Pydantic Settings)
│   ├── main.py                  # Application FastAPI & routes API/Web
│   ├── models.py                # Schémas de données Pydantic
│   ├── notification_service.py  # Service de validation OTP et notifications email (Sponsorship & Vouchers)
│   ├── profile_manager.py       # Gestionnaire persistant des profils & synchronisation AP
│   ├── qr_generator.py          # Générateur de QR codes Wi-Fi
│   ├── static/
│   │   ├── css/style.css        # Feuille de style épurée & media queries print
│   │   └── js/app.js            # Logique dynamique du portail et administration
│   └── templates/
│       ├── base.html            # Layout HTML de base
│       ├── index.html           # Portail de génération de pass invité
│       ├── voucher.html         # Billet d'accès invité avec QR code
│       └── admin.html           # Tableau de bord administrateur & gestion des profils
├── tests/
│   └── test_app.py              # Suite complète de tests unitaires et d'intégration
├── ARUBA_IAP_CONFIGURATION.md   # Guide complet de paramétrage de la borne Aruba Instant
├── aruba_iap_setup.cli          # Script CLI prêt à l'emploi pour borne Aruba (conf t)
├── Dockerfile                   # Image conteneur de production
├── docker-compose.yml           # Déploiement en un clic
├── requirements.txt             # Dépendances Python
├── profiles.json                # Fichier de profils par défaut
├── .env.example                 # Modèle de variables d'environnement (anonymisé)
├── .gitignore                   # Exclusions strictes (sécurité, secrets, logs)
├── CONTRIBUTING.md              # Guide du contributeur
└── LICENSE                      # Licence open source MIT
```

---

## 📁 Fichiers de Configuration Aruba AP

Pour configurer votre infrastructure réseau et votre borne Aruba Instant :

* 📖 **[ARUBA_IAP_CONFIGURATION.md](./ARUBA_IAP_CONFIGURATION.md)** : Guide complet étape par étape (CLI SSH, WebUI, règles de filtrage, commandes de vérification).
* ⚙️ **[aruba_iap_setup.cli](./aruba_iap_setup.cli)** : Script CLI prêt à être copié-collé dans votre terminal AP (`conf t`).

---

## 🚀 Démarrage Rapide

### 1. Cloner et installer les dépendances

```bash
git clone https://github.com/votre-compte/wifi-guest.git
cd wifi-guest

# Créer un environnement virtuel Python
python -m venv .venv

# Activer l'environnement :
# Sur Windows (PowerShell) :
.\.venv\Scripts\Activate.ps1
# Sur Linux / macOS :
source .venv/bin/activate

# Installer les dépendances
pip install -r requirements.txt
```

### 2. Configurer les variables d'environnement

Copiez `.env.example` vers `.env` et ajustez vos paramètres :

```bash
cp .env.example .env
```

```dotenv
# Configuration Application
APP_TITLE=Wi-Fi Invités Homelab
APP_HOST=0.0.0.0
APP_PORT=8000
SECRET_KEY=change_this_secret_key_in_production
ADMIN_PASSWORD=votre_mot_de_passe_admin

# Réseau Wi-Fi Invité
WIFI_SSID=Societe-Guest

# Mode Aruba (instant = borne physique, mock = simulation locale hors-ligne)
ARUBA_MODE=instant
ARUBA_INSTANT_HOST=https://192.168.1.1:4343
ARUBA_INSTANT_USERNAME=admin
ARUBA_INSTANT_PASSWORD=votre_mot_de_passe_ap
ARUBA_INSTANT_VERIFY_SSL=false
ARUBA_MPSK_PROFILE=MPSK_GUEST
```

### 3. Lancer l'application

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Accédez au portail :
* **Portail Invités** : `http://localhost:8000/`
* **Console d'Administration** : `http://localhost:8000/admin`
* **Documentation Interactive OpenAPI** : `http://localhost:8000/docs`

---

## 🐳 Déploiement avec Docker

Pour exécuter le portail en conteneur autonome :

```bash
docker compose up -d
```

---

## 🧪 Tests Automatisés

Pour lancer la suite de tests avec simulation complète (sans nécessiter de borne Aruba connectée) :

```bash
python -m pytest tests/test_app.py -v
```

---

## 🤝 Contribution & Signalement de bugs

Les contributions sont les bienvenues ! Consultez le fichier **[CONTRIBUTING.md](./CONTRIBUTING.md)** pour connaître la marche à suivre, soumettre une idée ou proposer une pull request.

---

## 📄 Licence

Ce projet est distribué sous licence MIT. Consultez le fichier **[LICENSE](./LICENSE)** pour plus d'informations.
