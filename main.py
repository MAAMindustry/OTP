"""
ONE TOUCH MILLION — Backend FastAPI + NotchPay
Déployable sur Render — Version corrigée avec persistance JSON
"""

import asyncio
import hashlib
import hmac
import json
import os
import random
import secrets
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional
import logging
import smtplib
from email.mime.text import MIMEText

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
import uvicorn

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("OTM")

# ─── NOTCHPAY CONFIG ──────────────────────────────────────────────────────────
NOTCHPAY_PUBLIC_KEY  = os.environ.get("NOTCHPAY_PUBLIC_KEY",  "pk_test.xxx")
NOTCHPAY_PRIVATE_KEY = os.environ.get("NOTCHPAY_PRIVATE_KEY", "sk_test.xxx")
NOTCHPAY_HASH_KEY    = os.environ.get("NOTCHPAY_HASH_KEY",    "hsk_test.xxx")
NOTCHPAY_API         = "https://api.notchpay.co"
MISE_MIN             = 100
MISE_MAX             = 1000

SITE_URL = os.environ.get("SITE_URL", "https://one-touch-million.onrender.com")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

# ─── PERSISTANCE JSON ─────────────────────────────────────────────────────────
# FIX CRITIQUE : Render efface la RAM au redémarrage → on sauvegarde sur disque
DATA_FILE = os.environ.get("DATA_FILE", "/tmp/otm_accounts.json")

def load_accounts_from_disk() -> dict:
    """Charge les comptes depuis le fichier JSON au démarrage"""
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        accounts = {}
        for email, data in raw.items():
            acc = Account(**data)
            accounts[email] = acc
        log.info(f"[PERSISTANCE] {len(accounts)} comptes chargés depuis {DATA_FILE}")
        return accounts
    except Exception as e:
        log.error(f"[PERSISTANCE] Erreur chargement: {e}")
        return {}

def save_accounts_to_disk(accounts: dict):
    """Sauvegarde les comptes sur disque après chaque inscription/modification"""
    try:
        raw = {email: asdict(acc) for email, acc in accounts.items()}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"[PERSISTANCE] Erreur sauvegarde: {e}")

# ─── CONFIG JEU ───────────────────────────────────────────────────────────────
GROUP_SIZE     = 100_000
MAX_PLAYERS    = 1_000_000
WINNERS_COUNT  = 50
ROUND_DURATION = 30
COUNTDOWN      = 5

PRIZES = [
    5_000_000, 2_000_000, 1_000_000, 500_000, 300_000,
    200_000, 150_000, 100_000, 80_000, 60_000,
    50_000, 45_000, 40_000, 35_000, 30_000,
    28_000, 26_000, 24_000, 22_000, 20_000,
    18_000, 17_000, 16_000, 15_000, 14_000,
    13_000, 12_000, 11_000, 10_000, 9_500,
    9_000, 8_500, 8_000, 7_500, 7_000,
    6_500, 6_000, 5_500, 5_000, 4_500,
    4_000, 3_500, 3_000, 2_500, 2_000,
    1_500, 1_200, 1_000, 800, 500
]

# ─── HELPERS AUTH ─────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        return hashlib.sha256(f"{salt}{password}".encode()).hexdigest() == h
    except Exception:
        return False

def send_reset_email(email: str, token: str, name: str):
    if not SMTP_USER:
        log.warning("SMTP non configuré — email non envoyé")
        return
    try:
        reset_url = f"{SITE_URL}/?reset_token={token}"
        body = f"""Bonjour {name},\n\nRéinitialisez votre mot de passe :\n{reset_url}\n\nLien valide 30 minutes.\n\n— ONE TOUCH MILLION"""
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = "Réinitialisation mot de passe — ONE TOUCH MILLION"
        msg["From"] = SMTP_USER
        msg["To"] = email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.starttls()
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(SMTP_USER, [email], msg.as_string())
        log.info(f"Email reset envoyé à {email}")
    except Exception as e:
        log.error(f"Erreur email: {e}")

# ─── MODÈLES ──────────────────────────────────────────────────────────────────
@dataclass
class Account:
    id: str
    name: str
    phone: str
    email: str
    password_hash: str
    created_at: float = field(default_factory=time.time)
    reset_token: str = ""
    reset_expires: float = 0.0
    # FIX : Solde persistant sur le compte
    wallet: int = 0
    total_gains: int = 0
    total_deposits: int = 0

@dataclass
class Player:
    id: str
    name: str
    group: int
    phone: str = ""
    email: str = ""
    mise: int = 500
    paid: bool = False
    wallet: int = 0
    joined_at: float = field(default_factory=time.time)
    clicked: bool = False
    click_time: Optional[float] = None
    rank: Optional[int] = None
    prize: Optional[int] = None

@dataclass
class Winner:
    rank: int
    name: str
    time: float
    prize: int
    is_bot: bool = False

@dataclass
class GameState:
    phase: str = "idle"
    round: int = 1
    round_start: float = 0.0
    countdown_start: float = 0.0
    winners: list = field(default_factory=list)
    total_players: int = 0
    groups: list = field(default_factory=lambda: [0] * 10)

# ─── NOTCHPAY CLIENT ──────────────────────────────────────────────────────────
class NotchPayClient:
    def __init__(self):
        self.headers = {
            "Authorization": NOTCHPAY_PUBLIC_KEY,
            "Content-Type": "application/json",
        }
        self.private_headers = {
            "Authorization": NOTCHPAY_PRIVATE_KEY,
            "Content-Type": "application/json",
        }

    async def init_payment(self, player: "Player", reference: str, amount: int, callback_url: str) -> dict:
        """
        FIX: callback_url est maintenant dynamique (mise vs dépôt)
        FIX: 'phone' doit inclure l'indicatif pays pour NotchPay Cameroun
        """
        phone = player.phone or ""
        # NotchPay attend le format international : +237XXXXXXXXX
        if phone and not phone.startswith("+"):
            phone = "+237" + phone.lstrip("0")

        payload = {
            "amount": amount,
            "currency": "XAF",
            "customer": {
                "name": player.name,
                "email": player.email if player.email and "@" in player.email else f"user_{player.id[:8]}@otm.game",
                "phone": phone,
            },
            "description": "ONE TOUCH MILLION",
            "reference": reference,
            "callback": callback_url,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{NOTCHPAY_API}/payments",
                headers=self.headers,
                json=payload,
            )
            data = r.json()
            log.info(f"NotchPay init_payment [{r.status_code}]: {data}")
            if r.status_code not in (200, 201):
                # FIX : Message d'erreur plus précis pour le débogage
                msg = data.get("message") or data.get("error") or str(data)
                raise ValueError(f"NotchPay [{r.status_code}]: {msg}")
            return data

    async def verify_payment(self, reference: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{NOTCHPAY_API}/payments/{reference}",
                headers=self.headers,
            )
            return r.json()

    async def send_transfer(self, account: "Account", amount: int, reference: str) -> dict:
        phone = account.phone or ""
        if phone and not phone.startswith("+"):
            phone = "+237" + phone.lstrip("0")

        # FIX : Détection canal MTN/Orange améliorée
        local = account.phone.lstrip("+237").lstrip("0") if account.phone else ""
        if local.startswith(("65", "66", "69")):
            channel = "cm.orange"
        else:
            channel = "cm.mtn"

        payload = {
            "amount": amount,
            "currency": "XAF",
            "beneficiary": {
                "name": account.name,
                "phone": phone,
                "email": account.email,
            },
            "description": f"Gain ONE TOUCH MILLION",
            "reference": reference,
            "channel": channel,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{NOTCHPAY_API}/transfers",
                headers=self.private_headers,
                json=payload,
            )
            data = r.json()
            log.info(f"NotchPay transfer [{r.status_code}]: {data}")
            return data

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        """FIX : Utilisation correcte de hmac.new"""
        expected = hmac.new(
            NOTCHPAY_HASH_KEY.encode(),
            payload,
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

notchpay = NotchPayClient()

# ─── CONNEXIONS WEBSOCKET ─────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, WebSocket] = {}
        self.spectators: list[WebSocket] = []

    async def connect_player(self, player_id: str, ws: WebSocket):
        await ws.accept()
        self.connections[player_id] = ws

    async def connect_spectator(self, ws: WebSocket):
        await ws.accept()
        self.spectators.append(ws)

    def disconnect(self, player_id: str):
        self.connections.pop(player_id, None)

    def disconnect_spectator(self, ws: WebSocket):
        if ws in self.spectators:
            self.spectators.remove(ws)

    async def send(self, player_id: str, data: dict):
        ws = self.connections.get(player_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                self.connections.pop(player_id, None)

    async def broadcast(self, data: dict, exclude: str = None):
        dead = []
        for pid, ws in self.connections.items():
            if pid == exclude:
                continue
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(pid)
        for pid in dead:
            self.connections.pop(pid, None)
        for ws in list(self.spectators):
            try:
                await ws.send_json(data)
            except Exception:
                self.spectators.remove(ws)

    async def broadcast_all(self, data: dict):
        await self.broadcast(data)

# ─── MOTEUR DE JEU ────────────────────────────────────────────────────────────
class GameEngine:
    def __init__(self, manager: ConnectionManager):
        self.mgr = manager
        self.state = GameState()
        self.players: dict[str, Player] = {}
        # FIX CRITIQUE : Charger les comptes depuis le disque au démarrage
        self.accounts: dict[str, Account] = load_accounts_from_disk()
        self.accounts_by_id: dict[str, Account] = {
            acc.id: acc for acc in self.accounts.values()
        }
        self.pending_payments: dict[str, dict] = {}  # ref -> {player_id, account_id, type, amount}
        self.bot_clicks: list[float] = []
        self.task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        log.info(f"GameEngine démarré — {len(self.accounts)} comptes en mémoire")

    # ── INSCRIPTION ───────────────────────────────────────────────────────────
    async def create_account(self, name: str, phone: str, email: str, password: str) -> Account:
        if email in self.accounts:
            raise ValueError("Un compte existe déjà avec cet email")
        acc = Account(
            id=str(uuid.uuid4()),
            name=name,
            phone=phone,
            email=email,
            password_hash=hash_password(password),
        )
        self.accounts[email] = acc
        self.accounts_by_id[acc.id] = acc
        # FIX : Sauvegarder immédiatement sur disque
        save_accounts_to_disk(self.accounts)
        log.info(f"Nouveau compte créé: {name} ({email})")
        return acc

    # ── CONNEXION ─────────────────────────────────────────────────────────────
    async def login(self, email: str, password: str) -> Account:
        acc = self.accounts.get(email)
        if not acc:
            raise ValueError("Email ou mot de passe incorrect")
        if not verify_password(password, acc.password_hash):
            raise ValueError("Email ou mot de passe incorrect")
        return acc

    # ── RESET MOT DE PASSE ────────────────────────────────────────────────────
    async def request_password_reset(self, email: str) -> str:
        acc = self.accounts.get(email)
        if not acc:
            return "ok"
        token = secrets.token_urlsafe(32)
        acc.reset_token = token
        acc.reset_expires = time.time() + 1800
        save_accounts_to_disk(self.accounts)
        asyncio.create_task(asyncio.to_thread(send_reset_email, email, token, acc.name))
        return "ok"

    async def reset_password(self, token: str, new_password: str) -> bool:
        for acc in self.accounts.values():
            if acc.reset_token == token and time.time() < acc.reset_expires:
                acc.password_hash = hash_password(new_password)
                acc.reset_token = ""
                acc.reset_expires = 0.0
                save_accounts_to_disk(self.accounts)
                return True
        return False

    # ── DÉPÔT (recharge de portefeuille) ─────────────────────────────────────
    async def initiate_deposit(self, account_id: str, amount: int) -> dict:
        """FIX : Nouvel endpoint dépôt — séparé du paiement de mise"""
        acc = self.accounts_by_id.get(account_id)
        if not acc:
            raise ValueError("Compte introuvable — veuillez vous reconnecter")

        ref = f"dep_{account_id[:8]}_{int(time.time())}"
        self.pending_payments[ref] = {
            "type": "deposit",
            "account_id": account_id,
            "amount": amount,
        }

        # Créer un pseudo-player pour l'API NotchPay
        fake_player = Player(
            id=acc.id,
            name=acc.name,
            group=0,
            phone=acc.phone,
            email=acc.email,
        )
        callback = f"{SITE_URL}/api/deposit/callback"
        data = await notchpay.init_payment(fake_player, ref, amount, callback)
        auth_url = (
            data.get("authorization_url")
            or (data.get("transaction") or {}).get("authorization_url")
            or (data.get("payment") or {}).get("authorization_url")
        )
        return {"authorization_url": auth_url, "reference": ref}

    async def confirm_deposit(self, reference: str) -> bool:
        """FIX : Vérifier et créditer le dépôt sur le compte"""
        info = self.pending_payments.get(reference)
        if not info or info.get("type") != "deposit":
            return False

        account_id = info["account_id"]
        acc = self.accounts_by_id.get(account_id)
        if not acc:
            return False

        try:
            data = await notchpay.verify_payment(reference)
            tx = data.get("transaction") or data.get("payment") or {}
            status = tx.get("status", "")
            amount = int(tx.get("amount") or info.get("amount", 0))
            log.info(f"Vérif dépôt {reference}: status={status}, amount={amount}")

            if status == "complete":
                acc.wallet += amount
                acc.total_deposits += amount
                del self.pending_payments[reference]
                save_accounts_to_disk(self.accounts)
                # Notifier le joueur en temps réel si connecté
                player_id = self._find_player_by_account(account_id)
                if player_id:
                    await self.mgr.send(player_id, {
                        "type": "deposit_confirmed",
                        "amount": amount,
                        "new_balance": acc.wallet,
                        "message": f"Depot de {amount:,} FCFA confirme !",
                    })
                log.info(f"Depot confirme: {acc.name} +{amount} FCFA → solde={acc.wallet}")
                return True
        except Exception as e:
            log.error(f"Erreur confirmation depot: {e}")
        return False

    def _find_player_by_account(self, account_id: str) -> Optional[str]:
        for pid, p in self.players.items():
            if p.id == account_id or (hasattr(p, 'account_id') and p.account_id == account_id):
                return pid
        return None

    # ── REJOINDRE UNE PARTIE ──────────────────────────────────────────────────
    async def join_game(self, account_id: str, mise: int = 500) -> tuple[str, int]:
        async with self._lock:
            # FIX : Message d'erreur plus explicite
            acc = self.accounts_by_id.get(account_id)
            if not acc:
                raise ValueError(
                    "Compte introuvable. Le serveur a peut-etre redémarré — "
                    "veuillez vous déconnecter et vous reconnecter."
                )
            if self.state.total_players >= MAX_PLAYERS:
                raise ValueError("Serveur complet")

            pid = str(uuid.uuid4())
            grp = min(self.state.total_players // GROUP_SIZE, 9)
            player = Player(
                id=pid, name=acc.name, group=grp,
                phone=acc.phone, email=acc.email, mise=mise
            )
            self.players[pid] = player

            bots_to_add = random.randint(120_000, 180_000)
            bots_to_add = min(bots_to_add, MAX_PLAYERS - self.state.total_players - 1)
            for i in range(bots_to_add):
                g = min((self.state.total_players + i + 1) // GROUP_SIZE, 9)
                self.state.groups[g] += 1
            self.state.total_players += bots_to_add + 1
            self.state.groups[grp] += 1

            log.info(f"Joueur rejoint: {acc.name} → groupe {grp+1}")
            return pid, grp

    # ── INITIER PAIEMENT MISE ─────────────────────────────────────────────────
    async def initiate_payment(self, player_id: str) -> dict:
        player = self.players.get(player_id)
        if not player:
            raise ValueError("Session de jeu introuvable — veuillez rejoindre la partie")
        if player.paid:
            return {"already_paid": True}

        ref = f"otm_{player_id[:8]}_{int(time.time())}"
        self.pending_payments[ref] = {
            "type": "mise",
            "player_id": player_id,
            "amount": player.mise,
        }

        callback = f"{SITE_URL}/api/payment/callback"
        data = await notchpay.init_payment(player, ref, player.mise, callback)
        auth_url = (
            data.get("authorization_url")
            or (data.get("transaction") or {}).get("authorization_url")
            or (data.get("payment") or {}).get("authorization_url")
        )
        return {"authorization_url": auth_url, "reference": ref}

    # ── CONFIRMER PAIEMENT MISE ───────────────────────────────────────────────
    async def confirm_payment(self, reference: str) -> bool:
        info = self.pending_payments.get(reference)
        if not info:
            return False

        # Rediriger si c'est un dépôt
        if info.get("type") == "deposit":
            return await self.confirm_deposit(reference)

        player_id = info.get("player_id")
        if not player_id:
            return False
        player = self.players.get(player_id)
        if not player:
            return False

        try:
            data = await notchpay.verify_payment(reference)
            tx = data.get("transaction") or data.get("payment") or {}
            status = tx.get("status", "")
            log.info(f"Vérif paiement mise {reference}: {status}")
            if status == "complete":
                player.paid = True
                del self.pending_payments[reference]
                await self.mgr.send(player_id, {
                    "type": "payment_confirmed",
                    "message": "Paiement confirme ! Vous pouvez jouer.",
                })
                if self.task is None or self.task.done():
                    self.task = asyncio.create_task(self._game_loop())
                return True
        except Exception as e:
            log.error(f"Erreur vérification mise: {e}")
        return False

    # ── CLIC JOUEUR ──────────────────────────────────────────────────────────
    async def player_click(self, pid: str) -> dict:
        player = self.players.get(pid)
        if not player:
            raise ValueError("Joueur inconnu")
        if not player.paid:
            return {"ok": False, "reason": "not_paid"}
        if self.state.phase != "active":
            return {"ok": False, "reason": "round_not_active"}
        if player.clicked:
            return {"ok": False, "reason": "already_clicked"}

        elapsed = time.time() - self.state.round_start
        player.clicked = True
        player.click_time = elapsed

        if len(self.state.winners) < WINNERS_COUNT:
            rank = len(self.state.winners) + 1
            prize = PRIZES[rank - 1]
            player.rank = rank
            player.prize = prize
            player.wallet += prize
            winner = Winner(rank=rank, name=player.name + " ", time=elapsed, prize=prize)
            self.state.winners.append(asdict(winner))

            await self.mgr.broadcast_all({
                "type": "winner_added",
                "winner": asdict(winner),
                "total": len(self.state.winners)
            })

            # Créditer le gain sur le compte permanent
            acc = self.accounts.get(player.email)
            if acc:
                acc.wallet += prize
                acc.total_gains += prize
                save_accounts_to_disk(self.accounts)

            if player.phone:
                asyncio.create_task(self._pay_winner_account(player, prize))

            return {"ok": True, "rank": rank, "prize": prize, "time": elapsed}
        else:
            return {"ok": False, "reason": "too_late"}

    async def _pay_winner_account(self, player: Player, amount: int):
        acc = self.accounts.get(player.email)
        if not acc:
            return
        ref = f"win_{player.id[:8]}_{int(time.time())}"
        try:
            result = await notchpay.send_transfer(acc, amount, ref)
            log.info(f"Transfert gagnant {player.name}: {result}")
            await self.mgr.send(player.id, {
                "type": "prize_sent",
                "amount": amount,
                "message": f"Gain de {amount:,} FCFA en cours sur {player.phone} !",
            })
        except Exception as e:
            log.error(f"Erreur transfert {player.name}: {e}")
            await self.mgr.send(player.id, {
                "type": "prize_error",
                "message": "Erreur envoi. Contactez le support.",
            })

    # ── BOUCLE DE JEU ────────────────────────────────────────────────────────
    async def _game_loop(self):
        while True:
            try:
                await self._run_countdown()
                await self._run_round()
                await self._end_round()
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Erreur game loop: {e}", exc_info=True)
                await asyncio.sleep(2)

    async def _run_countdown(self):
        self.state.phase = "countdown"
        self.state.countdown_start = time.time()
        self.bot_clicks = sorted(
            random.uniform(0, ROUND_DURATION)
            for _ in range(WINNERS_COUNT * 4)
        )
        for i in range(COUNTDOWN, 0, -1):
            await self.mgr.broadcast_all({"type": "countdown", "seconds": i, "round": self.state.round})
            await asyncio.sleep(1)

    async def _run_round(self):
        self.state.phase = "active"
        self.state.round_start = time.time()
        self.state.winners = []
        for p in self.players.values():
            p.clicked = False
            p.click_time = None
            p.rank = None
            p.prize = None

        await self.mgr.broadcast_all({"type": "round_start", "round": self.state.round, "duration": ROUND_DURATION})

        end_time = self.state.round_start + ROUND_DURATION
        bot_idx = 0

        while time.time() < end_time and self.state.phase == "active":
            elapsed = time.time() - self.state.round_start
            remaining = max(0, ROUND_DURATION - elapsed)

            while bot_idx < len(self.bot_clicks) and self.bot_clicks[bot_idx] <= elapsed:
                if len(self.state.winners) < WINNERS_COUNT:
                    rank = len(self.state.winners) + 1
                    prize = PRIZES[rank - 1]
                    bot_name = f"Joueur{random.randint(10000, 99999)}"
                    winner = Winner(rank=rank, name=bot_name, time=self.bot_clicks[bot_idx], prize=prize, is_bot=True)
                    self.state.winners.append(asdict(winner))
                    await self.mgr.broadcast_all({
                        "type": "winner_added",
                        "winner": asdict(winner),
                        "total": len(self.state.winners)
                    })
                bot_idx += 1

            await self.mgr.broadcast_all({
                "type": "tick",
                "remaining": round(remaining, 1),
                "winners_count": len(self.state.winners)
            })

            if len(self.state.winners) >= WINNERS_COUNT:
                break
            await asyncio.sleep(0.05)

    async def _end_round(self):
        self.state.phase = "ended"
        await self.mgr.broadcast_all({
            "type": "round_end",
            "round": self.state.round,
            "winners": self.state.winners,
            "total_winners": len(self.state.winners)
        })
        self.state.round += 1
        log.info(f"Round {self.state.round - 1} termine. {len(self.state.winners)} gagnants.")

    def snapshot(self) -> dict:
        return {
            "phase": self.state.phase,
            "round": self.state.round,
            "total_players": self.state.total_players,
            "groups": self.state.groups,
            "winners": self.state.winners,
            "winners_count": len(self.state.winners),
            "mise_min": MISE_MIN,
            "mise_max": MISE_MAX,
        }

# ─── APP ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="ONE TOUCH MILLION", version="4.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

manager = ConnectionManager()
engine = GameEngine(manager)

# ─── AUTH ENDPOINTS ───────────────────────────────────────────────────────────
@app.post("/api/auth/register")
async def auth_register(body: dict):
    name     = (body.get("name") or "").strip()
    phone    = (body.get("phone") or "").strip()
    email    = (body.get("email") or "").strip().lower()
    password = (body.get("password") or "").strip()

    if not name or len(name) > 20:
        raise HTTPException(400, "Pseudo invalide (1-20 caracteres)")
    if not phone:
        raise HTTPException(400, "Numero de telephone requis")
    if not email or "@" not in email:
        raise HTTPException(400, "Email invalide")
    if len(password) < 6:
        raise HTTPException(400, "Mot de passe trop court (min 6)")

    try:
        acc = await engine.create_account(name, phone, email, password)
        return {
            "account_id": acc.id,
            "name": acc.name,
            "email": acc.email,
            "wallet": acc.wallet,
        }
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/auth/login")
async def auth_login(body: dict):
    email    = (body.get("email") or "").strip().lower()
    password = (body.get("password") or "").strip()
    if not email or not password:
        raise HTTPException(400, "Email et mot de passe requis")
    try:
        acc = await engine.login(email, password)
        return {
            "account_id": acc.id,
            "name": acc.name,
            "email": acc.email,
            "wallet": acc.wallet,          # FIX : Retourner le solde réel
            "total_gains": acc.total_gains,
        }
    except ValueError as e:
        raise HTTPException(401, str(e))


@app.post("/api/auth/forgot-password")
async def forgot_password(body: dict):
    email = (body.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "Email requis")
    await engine.request_password_reset(email)
    return {"message": "Si cet email existe, un lien a été envoyé."}


@app.post("/api/auth/reset-password")
async def reset_password(body: dict):
    token    = (body.get("token") or "").strip()
    password = (body.get("password") or "").strip()
    if not token or len(password) < 6:
        raise HTTPException(400, "Token ou mot de passe invalide")
    ok = await engine.reset_password(token, password)
    if not ok:
        raise HTTPException(400, "Lien expire ou invalide")
    return {"message": "Mot de passe modifie avec succes"}


# ─── WALLET ENDPOINT ──────────────────────────────────────────────────────────
@app.get("/api/wallet/{account_id}")
async def get_wallet(account_id: str):
    """FIX NOUVEAU : Obtenir le solde réel du compte"""
    acc = engine.accounts_by_id.get(account_id)
    if not acc:
        raise HTTPException(404, "Compte introuvable")
    return {
        "wallet": acc.wallet,
        "total_gains": acc.total_gains,
        "total_deposits": acc.total_deposits,
    }


# ─── DÉPÔT ENDPOINTS ──────────────────────────────────────────────────────────
@app.post("/api/deposit/init")
async def deposit_init(body: dict):
    """FIX NOUVEAU : Initier un dépôt de portefeuille"""
    account_id = body.get("account_id")
    amount = int(body.get("amount") or 0)
    if not account_id:
        raise HTTPException(400, "account_id manquant")
    if amount < 100 or amount > 10000:
        raise HTTPException(400, "Montant invalide (100-10000 FCFA)")
    try:
        result = await engine.initiate_deposit(account_id, amount)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/deposit/callback")
async def deposit_callback(reference: str = None, trxref: str = None):
    """FIX NOUVEAU : Callback après dépôt réussi"""
    ref = reference or trxref
    if ref:
        confirmed = await engine.confirm_deposit(ref)
        if confirmed:
            return RedirectResponse(url="/?deposit=success")
    return RedirectResponse(url="/?deposit=failed")


# ─── JEU ENDPOINTS ────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    idx = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    return HTMLResponse("<h1>ONE TOUCH MILLION</h1>")


@app.get("/api/state")
async def get_state():
    return engine.snapshot()


@app.post("/api/join")
async def join_game(body: dict):
    account_id = body.get("account_id")
    mise = int(body.get("mise") or 500)
    if mise < MISE_MIN or mise > MISE_MAX:
        raise HTTPException(400, f"Mise entre {MISE_MIN} et {MISE_MAX} FCFA")
    if not account_id:
        raise HTTPException(400, "account_id manquant")
    try:
        pid, grp = await engine.join_game(account_id, mise)
        return {
            "player_id": pid,
            "group": grp,
            "group_label": f"G-{grp+1}",
            "mise_min": MISE_MIN,
            "mise_max": MISE_MAX,
            "state": engine.snapshot()
        }
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/payment/init")
async def payment_init(body: dict):
    pid = body.get("player_id")
    if not pid:
        raise HTTPException(400, "player_id manquant")
    try:
        result = await engine.initiate_payment(pid)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/payment/callback")
async def payment_callback(reference: str = None, trxref: str = None):
    ref = reference or trxref
    if ref:
        confirmed = await engine.confirm_payment(ref)
        if confirmed:
            return RedirectResponse(url="/?payment=success")
    return RedirectResponse(url="/?payment=failed")


@app.post("/api/payment/webhook")
async def payment_webhook(request: Request):
    body = await request.body()
    sig  = request.headers.get("x-notch-signature", "")

    # FIX : Vérification de signature avec le résultat utilisé
    if sig and not notchpay.verify_webhook(body, sig):
        log.warning("Webhook signature invalide — ignoré")
        return {"received": False}

    try:
        data  = json.loads(body)
        event = data.get("event", "")
        log.info(f"Webhook NotchPay: {event}")

        ref = ""
        if event in ("payment.complete", "transaction.complete"):
            ref = (data.get("data") or data.get("transaction") or {}).get("reference", "")
            if ref:
                await engine.confirm_payment(ref)
        elif event == "transfer.complete":
            log.info(f"Transfert complété: {data}")
    except Exception as e:
        log.error(f"Erreur webhook: {e}")

    return {"received": True}


@app.post("/api/click")
async def click(body: dict):
    pid = body.get("player_id")
    if not pid:
        raise HTTPException(400, "player_id manquant")
    if not engine.players.get(pid):
        raise HTTPException(404, "Joueur inconnu")
    return await engine.player_click(pid)


@app.get("/api/leaderboard")
async def leaderboard():
    return {"winners": engine.state.winners, "round": engine.state.round}


@app.post("/api/demo/click")
async def demo_click(body: dict):
    elapsed = round(random.uniform(0.05, 2.5), 4)
    rank    = random.randint(1, 50)
    prizes  = [5000000,2000000,1000000,500000,300000,200000,150000,100000,
               80000,60000,50000,45000,40000,35000,30000,28000,26000,24000,
               22000,20000,18000,17000,16000,15000,14000,13000,12000,11000,
               10000,9500,9000,8500,8000,7500,7000,6500,6000,5500,5000,4500,
               4000,3500,3000,2500,2000,1500,1200,1000,800,500]
    won = random.random() > 0.4
    if won:
        return {"ok": True, "rank": rank, "prize": prizes[rank-1], "time": elapsed, "demo": True}
    return {"ok": False, "reason": "too_late", "demo": True}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "players": engine.state.total_players,
        "phase": engine.state.phase,
        "accounts": len(engine.accounts),
    }


# ─── WEBSOCKET ────────────────────────────────────────────────────────────────
@app.websocket("/ws/{player_id}")
async def ws_player(ws: WebSocket, player_id: str):
    player = engine.players.get(player_id)
    if not player:
        await ws.close(code=4001, reason="Joueur non trouvé")
        return

    await manager.connect_player(player_id, ws)
    await ws.send_json({
        "type": "connected",
        "state": engine.snapshot(),
        "paid": player.paid,
    })

    try:
        while True:
            data   = await ws.receive_json()
            action = data.get("action")
            if action == "click":
                result = await engine.player_click(player_id)
                await ws.send_json({"type": "click_result", **result})
            elif action == "ping":
                await ws.send_json({"type": "pong", "ts": time.time()})
    except WebSocketDisconnect:
        manager.disconnect(player_id)


@app.websocket("/ws/spectate")
async def ws_spectate(ws: WebSocket):
    await manager.connect_spectator(ws)
    await ws.send_json({"type": "connected", "state": engine.snapshot()})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_spectator(ws)


# ─── ENTRYPOINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
