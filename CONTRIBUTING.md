# Guide de Contribution

Merci de votre intérêt pour contribuer au projet **Wi-Fi Invités (Aruba Instant MPSK)** ! Ce guide a pour but de vous aider à participer efficacement au développement, à l'amélioration de la documentation ou au signalement de dysfonctionnements.

---

## 🔒 Sécurité et Confidentialité (Important)

Ce projet manipule des identifiants d'accès réseau (mots de passe de bornes Wi-Fi, clés PSK, jetons d'API).

> [!CAUTION]
> **Ne soumettez JAMAIS de secrets dans vos commits, issues ou pull requests.**
> Assurez-vous que votre fichier `.env` ou toute clé SSH / jeton d'accès n'est jamais poussé sur GitHub.
> Si vous découvrez une faille de sécurité sensible, merci de ne pas ouvrir une issue publique mais de contacter directement les mainteneurs.

---

## 🛠️ Configuration de l'Environnement de Développement

### 1. Prérequis
- **Python 3.10** ou supérieur (testé sur 3.10, 3.11, 3.12, 3.13 et 3.14).
- **Git** installé sur votre machine.

### 2. Cloner le dépôt et initialiser le venv
```bash
git clone https://github.com/votre-compte/wifi-guest.git
cd wifi-guest

# Créer un environnement virtuel
python -m venv .venv

# Activer l'environnement virtuel
# Sur Linux / macOS :
source .venv/bin/activate
# Sur Windows PowerShell :
.\.venv\Scripts\Activate.ps1

# Installer les dépendances
pip install -r requirements.txt
pip install pytest pytest-cov
```

### 3. Fichier d'environnement de test
Pour développer sans avoir besoin d'une borne physique Aruba connectée, utilisez le mode **`mock`** :

```bash
cp .env.example .env
```

Dans votre `.env`, configurez :
```dotenv
ARUBA_MODE=mock
ADMIN_PASSWORD=admin123
```

---

## 🧪 Exécution des Tests

Avant de soumettre une contribution, assurez-vous que l'ensemble des tests automatisés passent avec succès :

```bash
# Lancer les tests unitaires et d'intégration
python -m pytest tests/test_app.py -v

# Vérifier la couverture de code (optionnel)
python -m pytest --cov=app tests/
```

---

## 🌿 Workflow Git & Branches

1. **Forkez** le dépôt sur votre compte GitHub.
2. Créez une branche dédiée à votre fonctionnalité ou correction :
   ```bash
   git checkout -b feature/nom-de-votre-fonctionnalite
   # ou
   git checkout -b fix/nom-du-bug
   ```
3. Effectuez des commits atomiques avec des messages clairs et descriptifs en respectant les conventions [Conventional Commits](https://www.conventionalcommits.org/) :
   - `feat: ajout du support du protocole WPA3-Enterprise`
   - `fix: correction de la validation du format d'adresse IP`
   - `docs: mise à jour du guide d'installation Aruba Instant`
4. Poussez votre branche sur votre fork :
   ```bash
   git push origin feature/nom-de-votre-fonctionnalite
   ```
5. Ouvrez une **Pull Request** sur la branche `main` du projet d'origine en complétant le modèle de PR.

---

## 📐 Standards de Code

- Respecter les conventions **PEP 8**.
- Utiliser le typage Python (`typing`, modèles Pydantic) pour toute nouvelle fonction ou structure de données.
- Veiller à ce que l'interface utilisateur reste accessible, responsive et épurée (palette de couleurs blanche/minimaliste, iconographie Lucide).
- Tout nouveau endpoint d'API ou modification métier doit être accompagné d'un test dans `tests/test_app.py`.

---

Merci pour vos contributions ! 🎉
