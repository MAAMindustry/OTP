# ONE TOUCH MILLION — v3.0 Backend

Backend FastAPI + WebSockets temps-réel + Système d'authentification complet. Déployable sur **Render**.

## Structure
```
one-touch-million/
├── app/
│   ├── __init__.py
│   └── main.py          ← moteur de jeu complet
├── static/
│   └── index.html       ← frontend servi par FastAPI
├── requirements.txt
├── render.yaml
└── README.md
```

---

## Nouveautés v3.0

- **Inscription / Connexion** — Système de comptes avec email + mot de passe hashé (SHA-256 + salt)
- **Mot de passe oublié** — Envoi d'email de réinitialisation via SMTP (Gmail, etc.)
- **Vérification d'âge +21** — Bloqué à l'entrée si l'utilisateur refuse
- **Politique de confidentialité** — 3 cases à cocher obligatoires avant de jouer
- **Clic ultra-rapide** — `ontouchstart` sur mobile (élimine le délai 300ms), WebSocket direct, timer à 60fps avec `performance.now()`
- **Paiements réels NotchPay** — Flux complet : initiation → callback → webhook → transfert gagnant

---

## Déploiement sur Render

### Étape 1 — Pousser sur GitHub
```bash
git init
git add .
git commit -m "v3.0 auth + age check + privacy policy"
git branch -M main
git remote add origin https://github.com/TON_USERNAME/one-touch-million.git
git push -u origin main
```

### Étape 2 — Créer le service sur Render
1. Va sur render.com → New → Web Service
2. Connecte ton repo GitHub
3. Render détecte automatiquement `render.yaml`
4. Configure les variables d'environnement (voir ci-dessous)
5. Clique "Create Web Service"
6. Ton URL : https://one-touch-million.onrender.com

### Variables d'environnement à configurer sur Render
```
NOTCHPAY_PUBLIC_KEY   → ta clé publique NotchPay (live)
NOTCHPAY_PRIVATE_KEY  → ta clé privée NotchPay (live)
NOTCHPAY_HASH_KEY     → ta clé de signature webhook NotchPay
SITE_URL              → https://one-touch-million.onrender.com
SMTP_USER             → ton email Gmail (ex: monapp@gmail.com)
SMTP_PASS             → mot de passe d'application Gmail (pas ton vrai mdp)
```

> **Gmail :** Active la validation en 2 étapes puis génère un "Mot de passe d'application" sur myaccount.google.com/apppasswords

---

## API Endpoints

### Auth
```
POST /api/auth/register      → Créer un compte  {"name","phone","email","password"}
POST /api/auth/login         → Connexion         {"email","password"}
POST /api/auth/forgot-password → Reset email    {"email"}
POST /api/auth/reset-password  → Nouveau mdp    {"token","password"}
```

### Jeu
```
GET  /                       → Frontend HTML
GET  /api/state              → État actuel du jeu
POST /api/join               → Rejoindre une partie {"account_id":"..."}
POST /api/payment/init       → Lancer le paiement   {"player_id":"..."}
GET  /api/payment/callback   → Retour NotchPay après paiement
POST /api/payment/webhook    → Webhook NotchPay (notifications auto)
POST /api/click              → Clic (fallback HTTP) {"player_id":"..."}
GET  /api/leaderboard        → Top 50 gagnants
GET  /health                 → Health check Render
WS   /ws/{player_id}         → WebSocket joueur (clic ultra-rapide)
WS   /ws/spectate            → WebSocket spectateur
```

---

## Paramètres de jeu (app/main.py)
```
GROUP_SIZE     = 100_000   joueurs par groupe
MAX_PLAYERS    = 1_000_000 maximum total
WINNERS_COUNT  = 50        gagnants par round
ROUND_DURATION = 30        durée du round (secondes)
COUNTDOWN      = 5         compte à rebours
MISE_FCFA      = 500       mise d'entrée en XAF
```

---

## Flux utilisateur complet

```
[Arrivée sur le site]
       ↓
[Vérification d'âge +21]  ← BLOQUANT si refus
       ↓
[Politique de confidentialité + CGU]  ← 3 cases obligatoires
       ↓
[Connexion OU Inscription]
       ↓
[Rejoindre une partie]  → assignation groupe
       ↓
[Paiement 500 FCFA via NotchPay]  → Orange Money / MTN MoMo
       ↓
[Attente du signal — countdown]
       ↓
[TOUCHER POUR GAGNER — clic ultra-rapide]
       ↓
[Résultat + transfert automatique des gains]
```

---

ATTENTION : Sur le plan Free de Render, le service s'endort après 15 min
d'inactivité. Premier chargement lent (~30s). Plan Starter = $7/mois sans sleep.
