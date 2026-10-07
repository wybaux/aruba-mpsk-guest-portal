# Contributing Guidelines

Thank you for your interest in contributing to **Enterprise & Homelab Guest Wi-Fi (Aruba Instant MPSK)**! This guide will help you participate effectively in development, documentation improvements, and bug reporting.

---

## 🔒 Security & Privacy (Important)

This project handles network credentials (Wi-Fi access passphrases, PSK keys, API tokens).

> [!CAUTION]
> **NEVER submit secrets in commits, issues, or pull requests.**
> Ensure your `.env` file, private SSH keys, and access tokens are never pushed to GitHub.
> If you discover a sensitive security vulnerability, please do NOT open a public issue; reach out to the project maintainers directly.

---

## 🛠️ Development Environment Setup

### 1. Prerequisites
- **Python 3.10** or higher (tested on 3.10, 3.11, 3.12, 3.13, and 3.14).
- **Git** installed on your system.

### 2. Clone the repository and initialize virtual environment
```bash
git clone https://github.com/wybaux/aruba-mpsk-guest-portal.git
cd aruba-mpsk-guest-portal

# Create virtual environment
python -m venv .venv

# Activate virtual environment:
# On Linux / macOS:
source .venv/bin/activate
# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
pip install pytest pytest-cov
```

### 3. Test environment configuration
To develop without needing a physical Aruba Access Point connected, use the **`mock`** mode:

```bash
cp .env.example .env
```

In your `.env`, set:
```dotenv
ARUBA_MODE=mock
ADMIN_PASSWORD=admin123
```

---

## 🧪 Running Tests

Before submitting any contribution, make sure the entire test suite passes:

```bash
# Run unit and integration tests
python -m pytest tests/test_app.py -v

# Check test coverage (optional)
python -m pytest --cov=app tests/
```

---

## 🌿 Git Workflow & Branches

1. **Fork** the repository on GitHub.
2. Create a dedicated branch for your feature or bug fix:
   ```bash
   git checkout -b feature/your-feature-name
   # or
   git checkout -b fix/bug-description
   ```
3. Make atomic commits with clear, descriptive messages following [Conventional Commits](https://www.conventionalcommits.org/):
   - `feat: add Portuguese language support and device detection`
   - `fix: correct IP address format validation`
   - `docs: update Aruba Instant setup guide`
4. Push your branch to your fork:
   ```bash
   git push origin feature/your-feature-name
   ```
5. Open a **Pull Request** targeting the `main` branch with the provided PR template.

---

## 📐 Code Standards

- Adhere to **PEP 8** style guidelines.
- Use Python type annotations (`typing`, Pydantic models) for all new functions and data structures.
- Keep UI components clean, accessible, and responsive (minimalist pure-white aesthetic, Lucide icons, Tailwind CSS).
- Any new API endpoint or business logic change must include accompanying tests in `tests/test_app.py`.

---

Thank you for contributing! 🎉
