"""
ONE TOUCH MILLION — Backend FastAPI
Paiements fictifs (déduction wallet), historique des parties, profil compte
"""

import asyncio
import hashlib
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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
import uvicorn

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("OTM")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
SITE_URL   = os.environ.get("SITE_URL", "https://one-touch-million.onrender.com")
SMTP_HOST  = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER  = os.environ.get("SMTP_USER", "")
SMTP_PASS  = os.environ.get("SMTP_PASS", "")
DATA_FILE  = os.environ.get("DATA_FILE", "/tmp/otm_accounts.json")

GROUP_SIZE     = 100_000
MAX_PLAYERS    = 1_000_000
WINNERS_COUNT  = 50
ROUND_DURATION = 30
COUNTDOWN      = 5
MISE_AMOUNT    = 500

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

# ─── AUTH HELPERS ─────────────────────────────────────────────────────────────
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
        log.warning("SMTP non configuré")
        return
    try:
        reset_url = f"{SITE_URL}/?reset_token={token}"
        body = f"Bonjour {name},\n\nRéinitialisez votre mot de passe :\n{reset_url}\n\nLien valide 30 minutes.\n\n— ONE TOUCH MILLION"
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = "Réinitialisation mot de passe — ONE TOUCH MILLION"
        msg["From"] = SMTP_USER
        msg["To"] = email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.starttls()
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(SMTP_USER, [email], msg.as_string())
    except Exception as e:
        log.error(f"Erreur email: {e}")

# ─── MODÈLES ──────────────────────────────────────────────────────────────────
@dataclass
class HistoryEntry:
    round: int
    timestamp: float
    mise: int
    rank: Optional[int]
    prize: int
    result: str  # "win" | "loss" | "too_late"

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
    wallet: int = 0
    total_gains: int = 0
    total_deposits: int = 0
    total_spent: int = 0
    games_played: int = 0
    games_won: int = 0
    history: list = field(default_factory=list)  # list[HistoryEntry as dict]

@dataclass
class Player:
    id: str           # == account_id
    name: str
    group: int
    phone: str = ""
    email: str = ""
    mise: int = MISE_AMOUNT
    paid: bool = False
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
    winners: list = field(default_factory=list)
    total_players: int = 0
    groups: list = field(default_factory=lambda: [0] * 10)

# ─── PERSISTANCE ──────────────────────────────────────────────────────────────
def load_accounts_from_disk() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        accounts = {}
        for email, data in raw.items():
            # Compatibilité champs manquants
            data.setdefault("total_spent", 0)
            data.setdefault("games_played", 0)
            data.setdefault("games_won", 0)
            data.setdefault("history", [])
            data.setdefault("total_deposits", 0)
            acc = Account(**data)
            accounts[email] = acc
        log.info(f"[DB] {len(accounts)} comptes chargés")
        return accounts
    except Exception as e:
        log.error(f"[DB] Erreur chargement: {e}")
        return {}

def save_accounts_to_disk(accounts: dict):
    try:
        raw = {email: asdict(acc) for email, acc in accounts.items()}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"[DB] Erreur sauvegarde: {e}")

# ─── WEBSOCKET MANAGER ────────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, WebSocket] = {}

    async def connect(self, player_id: str, ws: WebSocket):
        await ws.accept()
        self.connections[player_id] = ws

    def disconnect(self, player_id: str):
        self.connections.pop(player_id, None)

    async def send(self, player_id: str, data: dict):
        ws = self.connections.get(player_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                self.connections.pop(player_id, None)

    async def broadcast(self, data: dict):
        dead = []
        for pid, ws in list(self.connections.items()):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(pid)
        for pid in dead:
            self.connections.pop(pid, None)

# ─── MOTEUR DE JEU ────────────────────────────────────────────────────────────
class GameEngine:
    def __init__(self, manager: ConnectionManager):
        self.mgr = manager
        self.state = GameState()
        self.players: dict[str, Player] = {}
        self.accounts: dict[str, Account] = load_accounts_from_disk()
        self.accounts_by_id: dict[str, Account] = {
            acc.id: acc for acc in self.accounts.values()
        }
        self.bot_clicks: list[float] = []
        self.task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        log.info(f"GameEngine démarré — {len(self.accounts)} comptes")

    # ── AUTH ──────────────────────────────────────────────────────────────────
    async def create_account(self, name: str, phone: str, email: str, password: str) -> Account:
        if email in self.accounts:
            raise ValueError("Un compte existe déjà avec cet email")
        acc = Account(
            id=str(uuid.uuid4()),
            name=name, phone=phone, email=email,
            password_hash=hash_password(password),
        )
        self.accounts[email] = acc
        self.accounts_by_id[acc.id] = acc
        save_accounts_to_disk(self.accounts)
        return acc

    async def login(self, email: str, password: str) -> Account:
        acc = self.accounts.get(email)
        if not acc or not verify_password(password, acc.password_hash):
            raise ValueError("Email ou mot de passe incorrect")
        return acc

    async def request_password_reset(self, email: str):
        acc = self.accounts.get(email)
        if not acc:
            return
        token = secrets.token_urlsafe(32)
        acc.reset_token = token
        acc.reset_expires = time.time() + 1800
        save_accounts_to_disk(self.accounts)
        asyncio.create_task(asyncio.to_thread(send_reset_email, email, token, acc.name))

    async def reset_password(self, token: str, new_password: str) -> bool:
        for acc in self.accounts.values():
            if acc.reset_token == token and time.time() < acc.reset_expires:
                acc.password_hash = hash_password(new_password)
                acc.reset_token = ""
                acc.reset_expires = 0.0
                save_accounts_to_disk(self.accounts)
                return True
        return False

    # ── DÉPÔT FICTIF ──────────────────────────────────────────────────────────
    async def fake_deposit(self, account_id: str, amount: int) -> dict:
        acc = self.accounts_by_id.get(account_id)
        if not acc:
            raise ValueError("Compte introuvable")
        if amount < 100 or amount > 100_000:
            raise ValueError("Montant invalide (100 – 100 000 FCFA)")
        # Simulation délai "traitement"
        await asyncio.sleep(1.5)
        acc.wallet += amount
        acc.total_deposits += amount
        save_accounts_to_disk(self.accounts)
        # Notifier via WS si joueur connecté
        player_id = self._find_player_by_account(account_id)
        if player_id:
            await self.mgr.send(player_id, {
                "type": "deposit_confirmed",
                "amount": amount,
                "new_balance": acc.wallet,
                "message": f"Dépôt fictif de {amount:,} FCFA confirmé !",
            })
        log.info(f"[DÉPÔT FICTIF] {acc.name} +{amount} FCFA → solde={acc.wallet}")
        return {"success": True, "new_balance": acc.wallet}

    def _find_player_by_account(self, account_id: str) -> Optional[str]:
        for pid, p in self.players.items():
            if p.id == account_id:
                return pid
        return None

    # ── REJOINDRE + PAYER MISE ─────────────────────────────────────────────────
    async def join_and_pay(self, account_id: str) -> tuple[str, int]:
        async with self._lock:
            acc = self.accounts_by_id.get(account_id)
            if not acc:
                raise ValueError("Compte introuvable — reconnectez-vous")
            if acc.wallet < MISE_AMOUNT:
                raise ValueError(f"Solde insuffisant. Vous avez {acc.wallet} FCFA, mise requise : {MISE_AMOUNT} FCFA. Rechargez votre compte.")

            # Déduire la mise immédiatement
            acc.wallet -= MISE_AMOUNT
            acc.total_spent += MISE_AMOUNT
            acc.games_played += 1
            save_accounts_to_disk(self.accounts)

            if self.state.total_players >= MAX_PLAYERS:
                raise ValueError("Serveur complet")

            # Créer/remplacer le joueur (1 session par compte)
            old_pid = self._find_player_by_account(account_id)
            if old_pid:
                del self.players[old_pid]

            pid = str(uuid.uuid4())
            grp = min(self.state.total_players // GROUP_SIZE, 9)
            player = Player(id=account_id, name=acc.name, group=grp,
                            phone=acc.phone, email=acc.email,
                            mise=MISE_AMOUNT, paid=True)
            self.players[pid] = player

            bots = random.randint(80_000, 150_000)
            bots = min(bots, MAX_PLAYERS - self.state.total_players - 1)
            for i in range(bots):
                g = min((self.state.total_players + i + 1) // GROUP_SIZE, 9)
                self.state.groups[g] = self.state.groups[g] + 1
            self.state.total_players += bots + 1
            self.state.groups[grp] += 1

            log.info(f"[JOIN] {acc.name} → groupe G-{grp+1} | solde restant: {acc.wallet} FCFA")

            # Lancer le jeu si pas en cours
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self._game_loop())

            return pid, grp, acc.wallet

    # ── CLIC JOUEUR ───────────────────────────────────────────────────────────
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
            rank  = len(self.state.winners) + 1
            prize = PRIZES[rank - 1]
            player.rank  = rank
            player.prize = prize

            winner = Winner(rank=rank, name=player.name + " ", time=elapsed, prize=prize)
            self.state.winners.append(asdict(winner))

            await self.mgr.broadcast({
                "type": "winner_added",
                "winner": asdict(winner),
                "total": len(self.state.winners)
            })

            # Créditer le gain sur le compte
            acc = self.accounts.get(player.email)
            if acc:
                acc.wallet += prize
                acc.total_gains += prize
                acc.games_won += 1
                entry = asdict(HistoryEntry(
                    round=self.state.round, timestamp=time.time(),
                    mise=MISE_AMOUNT, rank=rank, prize=prize, result="win"
                ))
                acc.history.insert(0, entry)
                if len(acc.history) > 100:
                    acc.history = acc.history[:100]
                save_accounts_to_disk(self.accounts)

                # Notifier le gain
                await self.mgr.send(pid, {
                    "type": "prize_sent",
                    "amount": prize,
                    "new_balance": acc.wallet,
                    "message": f"🏆 Gain de {prize:,} FCFA crédité sur votre compte !",
                })

            return {"ok": True, "rank": rank, "prize": prize, "time": elapsed}
        else:
            # Enregistrer la défaite "trop tard"
            acc = self.accounts.get(player.email)
            if acc:
                entry = asdict(HistoryEntry(
                    round=self.state.round, timestamp=time.time(),
                    mise=MISE_AMOUNT, rank=None, prize=0, result="too_late"
                ))
                acc.history.insert(0, entry)
                if len(acc.history) > 100:
                    acc.history = acc.history[:100]
                save_accounts_to_disk(self.accounts)
            return {"ok": False, "reason": "too_late"}

    # ── BOUCLE DE JEU ─────────────────────────────────────────────────────────
    async def _game_loop(self):
        while True:
            try:
                await self._run_countdown()
                await self._run_round()
                await self._end_round()
                await asyncio.sleep(8)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Erreur game loop: {e}", exc_info=True)
                await asyncio.sleep(2)

    async def _run_countdown(self):
        self.state.phase = "countdown"
        self.bot_clicks = sorted(
            random.uniform(0.5, ROUND_DURATION - 2)
            for _ in range(WINNERS_COUNT * 6)
        )
        for i in range(COUNTDOWN, 0, -1):
            await self.mgr.broadcast({
                "type": "countdown",
                "seconds": i,
                "round": self.state.round
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

        await self.mgr.broadcast({
            "type": "round_start",
            "round": self.state.round,
            "duration": ROUND_DURATION
        })

        end_time = self.state.round_start + ROUND_DURATION
        bot_idx  = 0

        while time.time() < end_time and self.state.phase == "active":
            elapsed   = time.time() - self.state.round_start
            remaining = max(0.0, ROUND_DURATION - elapsed)

            while bot_idx < len(self.bot_clicks) and self.bot_clicks[bot_idx] <= elapsed:
                if len(self.state.winners) < WINNERS_COUNT:
                    rank  = len(self.state.winners) + 1
                    prize = PRIZES[rank - 1]
                    bname = f"Joueur{random.randint(10000,99999)}"
                    w = Winner(rank=rank, name=bname, time=self.bot_clicks[bot_idx], prize=prize, is_bot=True)
                    self.state.winners.append(asdict(w))
                    await self.mgr.broadcast({
                        "type": "winner_added",
                        "winner": asdict(w),
                        "total": len(self.state.winners)
                    })
                bot_idx += 1

            await self.mgr.broadcast({
                "type": "tick",
                "remaining": round(remaining, 2),
                "winners_count": len(self.state.winners)
            })

            if len(self.state.winners) >= WINNERS_COUNT:
                break
            await asyncio.sleep(0.05)

    async def _end_round(self):
        self.state.phase = "ended"

        # Enregistrer "loss" pour les joueurs qui n'ont pas cliqué
        for pid, player in list(self.players.items()):
            if player.paid and not player.clicked:
                acc = self.accounts.get(player.email)
                if acc:
                    entry = asdict(HistoryEntry(
                        round=self.state.round, timestamp=time.time(),
                        mise=MISE_AMOUNT, rank=None, prize=0, result="loss"
                    ))
                    acc.history.insert(0, entry)
                    if len(acc.history) > 100:
                        acc.history = acc.history[:100]
                save_accounts_to_disk(self.accounts)

        await self.mgr.broadcast({
            "type": "round_end",
            "round": self.state.round,
            "winners": self.state.winners,
            "total_winners": len(self.state.winners)
        })

        # Réinitialiser les joueurs pour le prochain round
        self.players.clear()
        self.state.round += 1
        log.info(f"Round {self.state.round-1} terminé. {len(self.state.winners)} gagnants.")

    def snapshot(self) -> dict:
        return {
            "phase": self.state.phase,
            "round": self.state.round,
            "total_players": self.state.total_players,
            "groups": self.state.groups,
            "winners": self.state.winners,
            "winners_count": len(self.state.winners),
        }

# ─── APP FASTAPI ──────────────────────────────────────────────────────────────
app = FastAPI(title="ONE TOUCH MILLION", version="6.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_static = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static):
    app.mount("/static", StaticFiles(directory=_static), name="static")

manager = ConnectionManager()
engine  = GameEngine(manager)

# ─── AUTH ROUTES ──────────────────────────────────────────────────────────────
@app.post("/api/auth/register")
async def auth_register(body: dict):
    name  = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    email = (body.get("email") or "").strip().lower()
    pwd   = (body.get("password") or "").strip()
    if not name or len(name) > 20: raise HTTPException(400, "Pseudo invalide (1-20 car.)")
    if not phone: raise HTTPException(400, "Numéro Mobile Money requis")
    if not email or "@" not in email: raise HTTPException(400, "Email invalide")
    if len(pwd) < 6: raise HTTPException(400, "Mot de passe trop court (min 6)")
    try:
        acc = await engine.create_account(name, phone, email, pwd)
        return {"account_id": acc.id, "name": acc.name, "email": acc.email,
                "phone": acc.phone, "wallet": acc.wallet, "total_gains": 0}
    except ValueError as e:
        raise HTTPException(409, str(e))

@app.post("/api/auth/login")
async def auth_login(body: dict):
    email = (body.get("email") or "").strip().lower()
    pwd   = (body.get("password") or "").strip()
    if not email or not pwd: raise HTTPException(400, "Email et mot de passe requis")
    try:
        acc = await engine.login(email, pwd)
        return {"account_id": acc.id, "name": acc.name, "email": acc.email,
                "phone": acc.phone, "wallet": acc.wallet, "total_gains": acc.total_gains}
    except ValueError as e:
        raise HTTPException(401, str(e))

@app.post("/api/auth/forgot-password")
async def forgot_password(body: dict):
    email = (body.get("email") or "").strip().lower()
    if not email: raise HTTPException(400, "Email requis")
    await engine.request_password_reset(email)
    return {"message": "Si cet email existe, un lien a été envoyé."}

@app.post("/api/auth/reset-password")
async def reset_password(body: dict):
    token = (body.get("token") or "").strip()
    pwd   = (body.get("password") or "").strip()
    if not token or len(pwd) < 6: raise HTTPException(400, "Token ou mot de passe invalide")
    ok = await engine.reset_password(token, pwd)
    if not ok: raise HTTPException(400, "Lien expiré ou invalide")
    return {"message": "Mot de passe modifié"}

# ─── WALLET ───────────────────────────────────────────────────────────────────
@app.get("/api/wallet/{account_id}")
async def get_wallet(account_id: str):
    acc = engine.accounts_by_id.get(account_id)
    if not acc: raise HTTPException(404, "Compte introuvable")
    return {
        "wallet": acc.wallet,
        "total_gains": acc.total_gains,
        "total_deposits": acc.total_deposits,
        "total_spent": acc.total_spent,
        "games_played": acc.games_played,
        "games_won": acc.games_won,
    }

# ─── DÉPÔT FICTIF ─────────────────────────────────────────────────────────────
@app.post("/api/deposit/fake")
async def deposit_fake(body: dict):
    account_id = body.get("account_id")
    amount     = int(body.get("amount") or 0)
    if not account_id: raise HTTPException(400, "account_id manquant")
    if amount < 100 or amount > 100_000: raise HTTPException(400, "Montant invalide (100–100 000 FCFA)")
    try:
        result = await engine.fake_deposit(account_id, amount)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))

# ─── JEU ──────────────────────────────────────────────────────────────────────
@app.get("/api/state")
async def get_state():
    return engine.snapshot()

@app.post("/api/join")
async def join_game(body: dict):
    account_id = body.get("account_id")
    if not account_id: raise HTTPException(400, "account_id manquant")
    try:
        pid, grp, new_balance = await engine.join_and_pay(account_id)
        return {
            "player_id": pid,
            "group": grp,
            "group_label": f"G-{grp+1}",
            "new_balance": new_balance,
            "mise": MISE_AMOUNT,
            "state": engine.snapshot()
        }
    except ValueError as e:
        raise HTTPException(409, str(e))

@app.post("/api/click")
async def click(body: dict):
    pid = body.get("player_id")
    if not pid: raise HTTPException(400, "player_id manquant")
    if not engine.players.get(pid): raise HTTPException(404, "Joueur inconnu")
    return await engine.player_click(pid)

# ─── HISTORIQUE & PROFIL ──────────────────────────────────────────────────────
@app.get("/api/history/{account_id}")
async def get_history(account_id: str, limit: int = 50):
    acc = engine.accounts_by_id.get(account_id)
    if not acc: raise HTTPException(404, "Compte introuvable")
    return {
        "history": acc.history[:limit],
        "total": len(acc.history)
    }

@app.get("/api/profile/{account_id}")
async def get_profile(account_id: str):
    acc = engine.accounts_by_id.get(account_id)
    if not acc: raise HTTPException(404, "Compte introuvable")
    win_rate = round((acc.games_won / acc.games_played * 100), 1) if acc.games_played > 0 else 0
    return {
        "id": acc.id,
        "name": acc.name,
        "phone": acc.phone,
        "email": acc.email,
        "wallet": acc.wallet,
        "total_gains": acc.total_gains,
        "total_deposits": acc.total_deposits,
        "total_spent": acc.total_spent,
        "games_played": acc.games_played,
        "games_won": acc.games_won,
        "win_rate": win_rate,
        "member_since": acc.created_at,
        "net_result": acc.total_gains - acc.total_spent,
    }

@app.put("/api/profile/{account_id}")
async def update_profile(account_id: str, body: dict):
    acc = engine.accounts_by_id.get(account_id)
    if not acc: raise HTTPException(404, "Compte introuvable")
    if "name" in body:
        name = (body["name"] or "").strip()
        if not name or len(name) > 20: raise HTTPException(400, "Pseudo invalide")
        acc.name = name
    if "phone" in body:
        phone = (body["phone"] or "").strip()
        if phone: acc.phone = phone
    if "password" in body and "old_password" in body:
        if not verify_password(body["old_password"], acc.password_hash):
            raise HTTPException(400, "Ancien mot de passe incorrect")
        if len(body["password"]) < 6:
            raise HTTPException(400, "Nouveau mot de passe trop court")
        acc.password_hash = hash_password(body["password"])
    save_accounts_to_disk(engine.accounts)
    return {"success": True, "name": acc.name, "phone": acc.phone}

@app.get("/api/leaderboard")
async def leaderboard():
    return {"winners": engine.state.winners, "round": engine.state.round}

@app.get("/health")
async def health():
    return {"status": "ok", "phase": engine.state.phase,
            "accounts": len(engine.accounts), "version": "6.0.0"}

# ─── WEBSOCKET ────────────────────────────────────────────────────────────────
@app.websocket("/ws/{player_id}")
async def ws_player(ws: WebSocket, player_id: str):
    player = engine.players.get(player_id)
    if not player:
        await ws.close(code=4001, reason="Joueur non trouvé")
        return
    await manager.connect(player_id, ws)
    await ws.send_json({"type": "connected", "state": engine.snapshot(), "paid": player.paid})
    try:
        while True:
            data = await ws.receive_json()
            action = data.get("action")
            if action == "click":
                result = await engine.player_click(player_id)
                await ws.send_json({"type": "click_result", **result})
            elif action == "ping":
                await ws.send_json({"type": "pong", "ts": time.time()})
    except WebSocketDisconnect:
        manager.disconnect(player_id)

# ─── SERVE FRONTEND ───────────────────────────────────────────────────────────
@app.get("/")
async def root():
    idx = os.path.join(_static, "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    return HTMLResponse("<h1>ONE TOUCH MILLION</h1>")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
