## 📝 Description des modifications

Expliquez brièvement les changements introduits par cette Pull Request. Précisez la motivation et le contexte métier ou technique.

Fixes #(numéro de l'issue)

---

## 🔍 Type de changement

Cochez les options applicables :
- [ ] 🐛 Correction de bug (changement rétrocompatible qui résout un incident)
- [ ] ✨ Nouvelle fonctionnalité (changement rétrocompatible ajoutant une capacité)
- [ ] 💥 Changement cassant (modification qui nécessite une adaptation des configurations existantes)
- [ ] 📚 Mise à jour de la documentation ou des fichiers de configuration Aruba
- [ ] ⚡ Optimisation des performances / Refactorisation
- [ ] 🧪 Ajout ou amélioration de tests automatisés

---

## ✅ Liste de contrôle (Checklist)

Avant de soumettre votre PR, vérifiez les points suivants :
- [ ] Mon code respecte les directives de style de code (PEP 8, typage).
- [ ] J'ai vérifié qu'**aucun secret, mot de passe ou adresse IP privée** n'est présent dans le code ou les commits.
- [ ] J'ai exécuté les tests en local avec succès (`pytest tests/test_app.py`).
- [ ] J'ai ajouté des tests pour couvrir mes modifications si nécessaire.
- [ ] La documentation (`README.md`, guides AP) a été mise à jour en conséquence.
- [ ] Mon code fonctionne en mode `ARUBA_MODE=mock` ainsi qu'en mode réel si applicable.
