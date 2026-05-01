"""
ONE TOUCH MILLION — Backend FastAPI + NotchPay (PRODUCTION)
Déployable sur Render
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
NOTCHPAY_PUBLIC_KEY  = os.environ.get("NOTCHPAY_PUBLIC_KEY",  "pk_test.S6YkLb5VsV2oZaV6NWJjMxXLRGtvYsSFb9TU07w4CWCTObZtF6TbXJIYTKjiULPSew9iGkGDQicpGQjWYa7ySLXxoj9ejDdNR3Yo9De5DQp6ZIE1KJg0GXaAHfvIy")
NOTCHPAY_PRIVATE_KEY = os.environ.get("NOTCHPAY_PRIVATE_KEY", "sk_test.Rxpi9c8hMQ3jVTKRUdEfWzeew63YFnJQUDZNCYxIb9uJ1ta6qVSxHbZLt5cWBb4FMvn52fYzzhNlDQAdKn21CKH7W6nClDXENUiB8qEd4QtdiCs16y6tIdwW0mzuS")
NOTCHPAY_HASH_KEY    = os.environ.get("NOTCHPAY_HASH_KEY",    "hsk_test.2heaJByGADdVDdH4niK811B6QN8ST9buAWGDe1jIIlZQzK97if3fJFb")
NOTCHPAY_API         = "https://api.notchpay.co"

# Montants jeu
MISE_MIN  = 100    # XAF
MISE_MAX  = 1000   # XAF
DEPOT_MIN = 100    # XAF
DEPOT_MAX = 10000  # XAF

SITE_URL = os.environ.get("SITE_URL", "https://one-touch-million.onrender.com")

# Email (reset mot de passe)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

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
    1_500, 1_200, 1_000, 800, 500,
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
        log.warning("SMTP non configuré — email de reset non envoyé")
        return
    try:
        reset_url = f"{SITE_URL}/?reset_token={token}"
        body = f"""Bonjour {name},

Vous avez demandé la réinitialisation de votre mot de passe ONE TOUCH MILLION.

Cliquez ici pour créer un nouveau mot de passe :
{reset_url}

Ce lien expire dans 30 minutes.

Si vous n'avez pas fait cette demande, ignorez cet email.

— L'équipe ONE TOUCH MILLION
"""
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
        log.error(f"Erreur envoi email reset: {e}")

# ─── MODÈLES ──────────────────────────────────────────────────────────────────
@dataclass
class Account:
    id: str
    name: str
    phone: str
    email: str
    password_hash: str
    balance: int = 0           # solde en FCFA
    total_gains: int = 0       # cumul des gains reçus
    created_at: float = field(default_factory=time.time)
    reset_token: str = ""
    reset_expires: float = 0.0


@dataclass
class Player:
    id: str
    account_id: str
    name: str
    group: int
    phone: str = ""
    email: str = ""
    mise: int = 500
    paid: bool = False
    joined_at: float = field(default_factory=time.time)
    clicked: bool = False
    click_time: Optional[float] = None
    rank: Optional[int] = None
    prize: Optional[int] = None


@dataclass
class DepotSession:
    """Session de dépôt de solde (recharge compte)"""
    id: str
    account_id: str
    amount: int
    reference: str
    status: str = "pending"   # pending | confirmed | failed
    created_at: float = field(default_factory=time.time)


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
    winners: list = field(default_factory=list)
    total_players: int = 0
    groups: list = field(default_factory=lambda: [0] * 10)


# ─── NOTCHPAY CLIENT ──────────────────────────────────────────────────────────
class NotchPayClient:

    # Headers publics (paiements entrants)
    @property
    def pub_headers(self):
        return {
            "Authorization": NOTCHPAY_PUBLIC_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # Headers privés (transferts sortants)
    @property
    def priv_headers(self):
        return {
            "Authorization": NOTCHPAY_PRIVATE_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Détecter l'opérateur mobile ─────────────────────────────────────────
    @staticmethod
    def detect_channel(phone: str) -> str:
        """
        Retourne le canal NotchPay selon le préfixe du numéro camerounais.
        Orange: 069x, 065x, 066x, 067x, 068x
        MTN:    067x (partagé), 070x, 071x, 072x, 073x, 074x, 075x, 076x, 077x, 078x, 079x, 050x
        """
        normalized = phone.replace(" ", "").replace("+237", "").replace("237", "")
        orange_prefixes = ("069", "065", "066", "068")
        mtn_prefixes    = ("070", "071", "072", "073", "074", "075", "076", "077", "078", "079", "050", "067")
        if normalized.startswith(orange_prefixes):
            return "cm.orange"
        if normalized.startswith(mtn_prefixes):
            return "cm.mtn"
        # Par défaut MTN
        return "cm.mtn"

    # ── Initialiser un paiement (mise ou dépôt) ──────────────────────────────
    async def init_payment(
        self,
        name: str,
        email: str,
        phone: str,
        amount: int,
        reference: str,
        description: str,
        callback_url: str,
    ) -> dict:
        payload = {
            "amount": amount,
            "currency": "XAF",
            "customer": {
                "name": name,
                "email": email or f"user_{reference[:8]}@otm.game",
                "phone": phone or "",
            },
            "description": description,
            "reference": reference,
            "callback": callback_url,
        }
        log.info(f"NotchPay init_payment → ref={reference} amount={amount} XAF")
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{NOTCHPAY_API}/payments",
                headers=self.pub_headers,
                json=payload,
            )
            data = r.json()
            log.info(f"NotchPay init_payment ← {r.status_code}: {data}")
            if r.status_code not in (200, 201):
                msg = data.get("message") or data.get("error") or "Erreur NotchPay"
                raise ValueError(msg)
            # L'URL peut se trouver à différents niveaux selon la version de l'API
            auth_url = (
                data.get("authorization_url")
                or data.get("transaction", {}).get("authorization_url")
                or data.get("payment", {}).get("authorization_url")
                or data.get("data", {}).get("authorization_url")
            )
            return {"authorization_url": auth_url, "reference": reference, "raw": data}

    # ── Vérifier un paiement ─────────────────────────────────────────────────
    async def verify_payment(self, reference: str) -> dict:
        log.info(f"NotchPay verify_payment → ref={reference}")
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{NOTCHPAY_API}/payments/{reference}",
                headers=self.pub_headers,
            )
            data = r.json()
            log.info(f"NotchPay verify_payment ← {r.status_code}: {data}")
            return data

    # ── Extraire le statut d'une réponse de vérification ───────────────────
    @staticmethod
    def extract_status(data: dict) -> str:
        """Retourne le statut normalisé: 'complete' | 'pending' | 'failed' | autre"""
        tx = (
            data.get("transaction")
            or data.get("payment")
            or data.get("data")
            or {}
        )
        return (tx.get("status") or data.get("status") or "unknown").lower()

    # ── Envoyer un transfert (gain joueur) ───────────────────────────────────
    async def send_transfer(
        self,
        name: str,
        phone: str,
        email: str,
        amount: int,
        reference: str,
        description: str,
    ) -> dict:
        channel = self.detect_channel(phone)
        payload = {
            "amount": amount,
            "currency": "XAF",
            "beneficiary": {
                "name": name,
                "phone": phone,
                "email": email or f"user_{reference[:8]}@otm.game",
            },
            "description": description,
            "reference": reference,
            "channel": channel,
        }
        log.info(f"NotchPay transfer → {name} ({phone}) {amount} XAF via {channel}")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{NOTCHPAY_API}/transfers",
                headers=self.priv_headers,
                json=payload,
            )
            data = r.json()
            log.info(f"NotchPay transfer ← {r.status_code}: {data}")
            return data

    # ── Vérifier la signature webhook ────────────────────────────────────────
    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        expected = hmac.new(
            NOTCHPAY_HASH_KEY.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


notchpay = NotchPayClient()


# ─── GESTIONNAIRE WEBSOCKET ───────────────────────────────────────────────────
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
        self.accounts: dict[str, Account] = {}          # email -> Account
        self.accounts_by_id: dict[str, Account] = {}    # id -> Account

        # Paiements de mise: reference -> player_id
        self.pending_mise_payments: dict[str, str] = {}
        # Dépôts de solde: reference -> DepotSession
        self.depot_sessions: dict[str, DepotSession] = {}

        self.bot_clicks: list[float] = []
        self.task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ═══════════════════════════════════════════════════════════════════════════
    # AUTH
    # ═══════════════════════════════════════════════════════════════════════════
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
        log.info(f"Nouveau compte: {name} ({email})")
        return acc

    async def login(self, email: str, password: str) -> Account:
        acc = self.accounts.get(email)
        if not acc or not verify_password(password, acc.password_hash):
            raise ValueError("Email ou mot de passe incorrect")
        return acc

    async def request_password_reset(self, email: str):
        acc = self.accounts.get(email)
        if not acc:
            return  # Ne pas révéler si l'email existe
        token = secrets.token_urlsafe(32)
        acc.reset_token = token
        acc.reset_expires = time.time() + 1800
        asyncio.create_task(asyncio.to_thread(send_reset_email, email, token, acc.name))

    async def reset_password(self, token: str, new_password: str) -> bool:
        for acc in self.accounts.values():
            if acc.reset_token == token and time.time() < acc.reset_expires:
                acc.password_hash = hash_password(new_password)
                acc.reset_token = ""
                acc.reset_expires = 0.0
                return True
        return False

    # ═══════════════════════════════════════════════════════════════════════════
    # DÉPÔT DE SOLDE (recharge compte)
    # ═══════════════════════════════════════════════════════════════════════════
    async def initiate_depot(self, account_id: str, amount: int) -> dict:
        """Initialise un dépôt de solde via NotchPay"""
        if amount < DEPOT_MIN or amount > DEPOT_MAX:
            raise ValueError(f"Montant invalide ({DEPOT_MIN}–{DEPOT_MAX} FCFA)")
        acc = self.accounts_by_id.get(account_id)
        if not acc:
            raise ValueError("Compte introuvable")

        reference = f"otm_depot_{account_id[:8]}_{int(time.time()*1000)}"
        session = DepotSession(
            id=str(uuid.uuid4()),
            account_id=account_id,
            amount=amount,
            reference=reference,
        )
        self.depot_sessions[reference] = session

        try:
            result = await notchpay.init_payment(
                name=acc.name,
                email=acc.email,
                phone=acc.phone,
                amount=amount,
                reference=reference,
                description=f"Recharge compte ONE TOUCH MILLION — {acc.name}",
                callback_url=f"{SITE_URL}/api/depot/callback",
            )
            return {
                "authorization_url": result["authorization_url"],
                "reference": reference,
                "amount": amount,
            }
        except Exception as e:
            del self.depot_sessions[reference]
            raise ValueError(str(e))

    async def confirm_depot(self, reference: str) -> bool:
        """Confirme un dépôt après vérification NotchPay"""
        session = self.depot_sessions.get(reference)
        if not session or session.status != "pending":
            return False
        try:
            data = await notchpay.verify_payment(reference)
            status = notchpay.extract_status(data)
            log.info(f"Confirm dépôt {reference}: status={status}")
            if status == "complete":
                session.status = "confirmed"
                acc = self.accounts_by_id.get(session.account_id)
                if acc:
                    acc.balance += session.amount
                    log.info(f"Solde {acc.name} → +{session.amount} FCFA = {acc.balance} FCFA")
                    # Notifier le joueur via WS si connecté
                    for pid, player in self.players.items():
                        if player.account_id == session.account_id:
                            await self.mgr.send(pid, {
                                "type": "depot_confirmed",
                                "amount": session.amount,
                                "new_balance": acc.balance,
                                "message": f"✓ Dépôt de {session.amount:,} FCFA confirmé !",
                            })
                return True
            elif status in ("failed", "cancelled", "expired"):
                session.status = "failed"
        except Exception as e:
            log.error(f"Erreur confirm dépôt: {e}")
        return False

    # ═══════════════════════════════════════════════════════════════════════════
    # MISE & PAIEMENT JEU
    # ═══════════════════════════════════════════════════════════════════════════
    async def join_game(self, account_id: str, mise: int = 500) -> tuple[str, int]:
        async with self._lock:
            acc = self.accounts_by_id.get(account_id)
            if not acc:
                raise ValueError("Compte introuvable")
            if mise < MISE_MIN or mise > MISE_MAX:
                raise ValueError(f"Mise invalide ({MISE_MIN}–{MISE_MAX} FCFA)")
            if self.state.total_players >= MAX_PLAYERS:
                raise ValueError("Serveur complet")

            pid = str(uuid.uuid4())
            grp = min(self.state.total_players // GROUP_SIZE, 9)
            player = Player(
                id=pid,
                account_id=account_id,
                name=acc.name,
                group=grp,
                phone=acc.phone,
                email=acc.email,
                mise=mise,
            )
            self.players[pid] = player

            # Bots simulés
            bots = random.randint(120_000, 180_000)
            bots = min(bots, MAX_PLAYERS - self.state.total_players - 1)
            for i in range(bots):
                g = min((self.state.total_players + i + 1) // GROUP_SIZE, 9)
                self.state.groups[g] += 1
            self.state.total_players += bots + 1
            self.state.groups[grp] += 1

            log.info(f"Joueur rejoint: {acc.name} → groupe {grp+1}, mise={mise} FCFA")
            return pid, grp

    async def initiate_payment(self, player_id: str) -> dict:
        """Initialise le paiement de la mise via NotchPay"""
        player = self.players.get(player_id)
        if not player:
            raise ValueError("Session de jeu introuvable")
        if player.paid:
            return {"already_paid": True}

        reference = f"otm_mise_{player_id[:8]}_{int(time.time()*1000)}"
        self.pending_mise_payments[reference] = player_id

        try:
            result = await notchpay.init_payment(
                name=player.name,
                email=player.email,
                phone=player.phone,
                amount=player.mise,
                reference=reference,
                description=f"Mise ONE TOUCH MILLION — {player.name} — Groupe G-{player.group+1}",
                callback_url=f"{SITE_URL}/api/payment/callback",
            )
            return {
                "authorization_url": result["authorization_url"],
                "reference": reference,
            }
        except Exception as e:
            self.pending_mise_payments.pop(reference, None)
            log.error(f"Erreur init paiement mise: {e}")
            raise ValueError(str(e))

    async def confirm_payment(self, reference: str) -> bool:
        """Confirme le paiement d'une mise"""
        player_id = self.pending_mise_payments.get(reference)
        if not player_id:
            return False
        player = self.players.get(player_id)
        if not player:
            return False
        try:
            data = await notchpay.verify_payment(reference)
            status = notchpay.extract_status(data)
            log.info(f"Confirm mise {reference}: status={status}")
            if status == "complete":
                player.paid = True
                self.pending_mise_payments.pop(reference, None)
                await self.mgr.send(player_id, {
                    "type": "payment_confirmed",
                    "message": "Paiement confirmé ! Vous pouvez jouer.",
                })
                # Lancer le jeu si pas déjà en cours
                if self.task is None or self.task.done():
                    self.task = asyncio.create_task(self._game_loop())
                return True
            elif status in ("failed", "cancelled", "expired"):
                await self.mgr.send(player_id, {
                    "type": "payment_failed",
                    "message": "Paiement échoué. Veuillez réessayer.",
                })
                self.pending_mise_payments.pop(reference, None)
        except Exception as e:
            log.error(f"Erreur confirm paiement: {e}")
        return False

    # ═══════════════════════════════════════════════════════════════════════════
    # WEBHOOK NOTCHPAY (commun dépôt + mise)
    # ═══════════════════════════════════════════════════════════════════════════
    async def handle_webhook(self, event: str, reference: str, status: str):
        """Traite les événements NotchPay reçus via webhook"""
        log.info(f"Webhook event={event} ref={reference} status={status}")
        if status not in ("complete", "successful"):
            return

        # C'est un dépôt ?
        if reference in self.depot_sessions:
            await self.confirm_depot(reference)
            return

        # C'est une mise ?
        if reference in self.pending_mise_payments:
            await self.confirm_payment(reference)
            return

        log.warning(f"Référence inconnue dans webhook: {reference}")

    # ═══════════════════════════════════════════════════════════════════════════
    # CLIC JOUEUR
    # ═══════════════════════════════════════════════════════════════════════════
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

            # Créditer le gain sur le solde du compte
            acc = self.accounts_by_id.get(player.account_id)
            if acc:
                acc.balance += prize
                acc.total_gains += prize

            winner = Winner(rank=rank, name=player.name + " ★", time=elapsed, prize=prize)
            self.state.winners.append(asdict(winner))

            await self.mgr.broadcast_all({
                "type": "winner_added",
                "winner": asdict(winner),
                "total": len(self.state.winners),
            })

            # Envoyer le vrai paiement Mobile Money
            if player.phone:
                asyncio.create_task(self._pay_winner(player, prize))

            log.info(f"Gagnant #{rank}: {player.name} en {elapsed:.4f}s → {prize} FCFA")
            return {"ok": True, "rank": rank, "prize": prize, "time": elapsed}
        else:
            return {"ok": False, "reason": "too_late"}

    async def _pay_winner(self, player: Player, amount: int):
        ref = f"otm_win_{player.id[:8]}_{int(time.time()*1000)}"
        try:
            result = await notchpay.send_transfer(
                name=player.name,
                phone=player.phone,
                email=player.email,
                amount=amount,
                reference=ref,
                description=f"Gain ONE TOUCH MILLION rang #{player.rank}",
            )
            log.info(f"Transfert gagnant {player.name}: {result}")
            # Vérifier si le transfert a réussi
            tx_status = (result.get("transfer") or result.get("data") or {}).get("status", "")
            if tx_status in ("sent", "complete", "processing"):
                await self.mgr.send(player.id, {
                    "type": "prize_sent",
                    "amount": amount,
                    "message": f"🎉 Votre gain de {amount:,} FCFA est en cours d'envoi sur {player.phone} !",
                })
            else:
                raise ValueError(f"Statut transfert: {tx_status}")
        except Exception as e:
            log.error(f"Erreur transfert gagnant {player.name}: {e}")
            await self.mgr.send(player.id, {
                "type": "prize_error",
                "message": f"Erreur lors de l'envoi du gain. Contactez support@onetouchmillion.cm — Réf: {ref}",
            })

    # ═══════════════════════════════════════════════════════════════════════════
    # BOUCLE DE JEU
    # ═══════════════════════════════════════════════════════════════════════════
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
        self.bot_clicks = sorted(
            random.uniform(0, ROUND_DURATION)
            for _ in range(WINNERS_COUNT * 4)
        )
        for i in range(COUNTDOWN, 0, -1):
            await self.mgr.broadcast_all({
                "type": "countdown",
                "seconds": i,
                "round": self.state.round,
            })
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

        await self.mgr.broadcast_all({
            "type": "round_start",
            "round": self.state.round,
            "duration": ROUND_DURATION,
        })

        end_time = self.state.round_start + ROUND_DURATION
        bot_idx = 0
        while time.time() < end_time and self.state.phase == "active":
            elapsed = time.time() - self.state.round_start
            remaining = max(0, ROUND_DURATION - elapsed)
            while bot_idx < len(self.bot_clicks) and self.bot_clicks[bot_idx] <= elapsed:
                if len(self.state.winners) < WINNERS_COUNT:
                    rank = len(self.state.winners) + 1
                    prize = PRIZES[rank - 1]
                    bot_name = f"Joueur#{random.randint(10000, 99999)}"
                    winner = Winner(rank=rank, name=bot_name, time=self.bot_clicks[bot_idx], prize=prize, is_bot=True)
                    self.state.winners.append(asdict(winner))
                    await self.mgr.broadcast_all({
                        "type": "winner_added",
                        "winner": asdict(winner),
                        "total": len(self.state.winners),
                    })
                bot_idx += 1
            await self.mgr.broadcast_all({
                "type": "tick",
                "remaining": round(remaining, 1),
                "winners_count": len(self.state.winners),
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
            "total_winners": len(self.state.winners),
        })
        log.info(f"Round {self.state.round} terminé. {len(self.state.winners)} gagnants.")
        self.state.round += 1

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


# ─── APP FASTAPI ───────────────────────────────────────────────────────────────
app = FastAPI(title="ONE TOUCH MILLION", version="4.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

manager = ConnectionManager()
engine = GameEngine(manager)


# ─── ROUTES STATIQUES ─────────────────────────────────────────────────────────
@app.get("/")
async def root():
    idx = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    return HTMLResponse("<h1>ONE TOUCH MILLION</h1><p>Placez index.html dans /static/</p>")


# ─── AUTH ─────────────────────────────────────────────────────────────────────
@app.post("/api/auth/register")
async def auth_register(body: dict):
    name     = (body.get("name") or "").strip()
    phone    = (body.get("phone") or "").strip()
    email    = (body.get("email") or "").strip().lower()
    password = (body.get("password") or "").strip()
    if not name or len(name) > 20:
        raise HTTPException(400, "Pseudo invalide (1–20 caractères)")
    if not phone:
        raise HTTPException(400, "Numéro Mobile Money requis")
    if not email or "@" not in email:
        raise HTTPException(400, "Email invalide")
    if len(password) < 6:
        raise HTTPException(400, "Mot de passe trop court (min 6 caractères)")
    try:
        acc = await engine.create_account(name, phone, email, password)
        return {"account_id": acc.id, "name": acc.name, "email": acc.email}
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
            "balance": acc.balance,
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
        raise HTTPException(400, "Lien expiré ou invalide")
    return {"message": "Mot de passe modifié avec succès"}


# ─── SOLDE COMPTE ─────────────────────────────────────────────────────────────
@app.get("/api/account/{account_id}/balance")
async def get_balance(account_id: str):
    acc = engine.accounts_by_id.get(account_id)
    if not acc:
        raise HTTPException(404, "Compte introuvable")
    return {"balance": acc.balance, "total_gains": acc.total_gains}


# ─── DÉPÔT DE SOLDE ───────────────────────────────────────────────────────────
@app.post("/api/depot/init")
async def depot_init(body: dict):
    """Initie un dépôt de solde via NotchPay"""
    account_id = body.get("account_id")
    amount = int(body.get("amount") or 0)
    if not account_id:
        raise HTTPException(400, "account_id manquant")
    try:
        result = await engine.initiate_depot(account_id, amount)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/depot/callback")
async def depot_callback(reference: str = None, trxref: str = None, status: str = None):
    """Callback NotchPay après un dépôt"""
    ref = reference or trxref
    log.info(f"Depot callback: ref={ref} status={status}")
    if ref:
        confirmed = await engine.confirm_depot(ref)
        if confirmed:
            return RedirectResponse(url="/?depot=success")
    return RedirectResponse(url="/?depot=failed")


@app.post("/api/depot/verify")
async def depot_verify(body: dict):
    """Vérification manuelle d'un dépôt (polling frontend)"""
    reference = body.get("reference")
    if not reference:
        raise HTTPException(400, "reference manquante")
    confirmed = await engine.confirm_depot(reference)
    session = engine.depot_sessions.get(reference)
    if session:
        acc = engine.accounts_by_id.get(session.account_id)
        return {
            "confirmed": confirmed,
            "status": session.status,
            "balance": acc.balance if acc else 0,
        }
    return {"confirmed": False, "status": "unknown"}


# ─── PAIEMENT MISE ────────────────────────────────────────────────────────────
@app.get("/api/state")
async def get_state():
    return engine.snapshot()


@app.post("/api/join")
async def join_game(body: dict):
    account_id = body.get("account_id")
    mise = int(body.get("mise") or 500)
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
            "state": engine.snapshot(),
        }
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/payment/init")
async def payment_init(body: dict):
    """Initie le paiement de la mise"""
    pid = body.get("player_id")
    if not pid:
        raise HTTPException(400, "player_id manquant")
    try:
        result = await engine.initiate_payment(pid)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/payment/callback")
async def payment_callback(reference: str = None, trxref: str = None, status: str = None):
    """Callback NotchPay après paiement mise"""
    ref = reference or trxref
    log.info(f"Payment callback: ref={ref} status={status}")
    if ref:
        confirmed = await engine.confirm_payment(ref)
        if confirmed:
            return RedirectResponse(url="/?payment=success")
    return RedirectResponse(url="/?payment=failed")


@app.post("/api/payment/verify")
async def payment_verify(body: dict):
    """Vérification manuelle du paiement (polling)"""
    reference = body.get("reference")
    if not reference:
        raise HTTPException(400, "reference manquante")
    confirmed = await engine.confirm_payment(reference)
    return {"confirmed": confirmed}


# ─── WEBHOOK NOTCHPAY (UNIFIÉ) ────────────────────────────────────────────────
@app.post("/api/payment/webhook")
async def payment_webhook(request: Request):
    """
    Webhook NotchPay — reçoit les événements paiement ET transfert.
    Configurez cette URL dans votre dashboard NotchPay :
    https://your-app.onrender.com/api/payment/webhook
    """
    body = await request.body()
    sig = request.headers.get("x-notch-signature", "")

    # Vérification signature (optionnel en test, obligatoire en prod)
    # if sig and not notchpay.verify_webhook(body, sig):
    #     raise HTTPException(401, "Signature invalide")

    try:
        data = json.loads(body)
        log.info(f"Webhook reçu: {json.dumps(data, indent=2)[:500]}")

        event = data.get("event", "")
        # Extraire la référence et le statut selon la structure de la réponse
        tx = (
            data.get("data")
            or data.get("transaction")
            or data.get("payment")
            or data.get("transfer")
            or {}
        )
        reference = tx.get("reference") or data.get("reference", "")
        status = (tx.get("status") or data.get("status") or "").lower()

        await engine.handle_webhook(event, reference, status)
    except json.JSONDecodeError:
        log.error("Webhook: payload JSON invalide")
    except Exception as e:
        log.error(f"Erreur webhook: {e}", exc_info=True)

    return {"received": True}


# ─── CLIC ─────────────────────────────────────────────────────────────────────
@app.post("/api/click")
async def click(body: dict):
    pid = body.get("player_id")
    if not pid:
        raise HTTPException(400, "player_id manquant")
    if not engine.players.get(pid):
        raise HTTPException(404, "Joueur inconnu")
    return await engine.player_click(pid)


# ─── DÉMO ─────────────────────────────────────────────────────────────────────
@app.post("/api/demo/click")
async def demo_click(body: dict):
    elapsed = round(random.uniform(0.05, 2.5), 4)
    rank    = random.randint(1, 50)
    won     = random.random() > 0.4
    if won:
        return {
            "ok": True,
            "rank": rank,
            "prize": PRIZES[rank - 1],
            "time": elapsed,
            "demo": True,
        }
    return {"ok": False, "reason": "too_late", "demo": True}


# ─── DIVERS ───────────────────────────────────────────────────────────────────
@app.get("/api/leaderboard")
async def leaderboard():
    return {"winners": engine.state.winners, "round": engine.state.round}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "players": engine.state.total_players,
        "phase": engine.state.phase,
        "round": engine.state.round,
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
    acc = engine.accounts_by_id.get(player.account_id)
    await ws.send_json({
        "type": "connected",
        "state": engine.snapshot(),
        "paid": player.paid,
        "balance": acc.balance if acc else 0,
    })
    try:
        while True:
            data = await ws.receive_json()
            action = data.get("action")
            if action == "click":
                result = await engine.player_click(player_id)
                await ws.send_json({"type": "click_result", **result})
            elif action == "ping":
                await ws.send_json({"type": "pong", "ts": time.time()})
            elif action == "get_balance":
                if acc:
                    await ws.send_json({"type": "balance", "balance": acc.balance, "total_gains": acc.total_gains})
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
