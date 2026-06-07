"""
ONE TOUCH MILLION — Backend FastAPI (Optimisé pour l'enchaînement Dépôt -> Jeu)
Paiements fictifs, déduction wallet, historique des parties, profil compte
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
from fastapi.responses import FileResponse, HTMLResponse
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

GROUP_SIZE    = 100_000
MAX_PLAYERS   = 1_000_000
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
    history: list = field(default_factory=list)

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

    # ── DÉPÔT FICTIF (RECHARGE DIRECTE) ──────────────────────────────────────────
    async def fake_deposit(self, account_id: str, amount: int) -> dict:
        async with self._lock:  # Sécurisation contre la modification simultanée
            acc = self.accounts_by_id.get(account_id)
            if not acc:
                raise ValueError("Compte introuvable")
            if amount < 100 or amount > 100_000:
                raise ValueError("Montant invalide (100 – 100 000 FCFA)")
            
            await asyncio.sleep(0.5) # Léger délai de traitement
            acc.wallet += amount
            acc.total_deposits += amount
            save_accounts_to_disk(self.accounts)
            
            # Alerte WS si l'utilisateur est déjà en ligne
            player_id = self._find_player_by_account(account_id)
            if player_id:
                await self.mgr.send(player_id, {
                    "type": "deposit_confirmed",
                    "amount": amount,
                    "new_balance": acc.wallet,
                    "message": f"Dépôt de {amount:,} FCFA validé ! Vous pouvez jouer.",
                })
            log.info(f"[DÉPÔT] {acc.name} +{amount} FCFA -> Solde: {acc.wallet}")
            return {"success": True, "new_balance": acc.wallet}

    def _find_player_by_account(self, account_id: str) -> Optional[str]:
        for pid, p in self.players.items():
            if p.id == account_id:
                return pid
        return None

    # ── REJOINDRE LE JEU (IMMEDIATEMENT APRES CHARGEMENT) ────────────────────────
    async def join_and_pay(self, account_id: str) -> tuple[str, int, int]:
        async with self._lock:
            acc = self.accounts_by_id.get(account_id)
            if not acc:
                raise ValueError("Compte introuvable — Reconnectez-vous")
            if acc.wallet < MISE_AMOUNT:
                raise ValueError(f"Solde insuffisant ({acc.wallet} FCFA). Mise requise : {MISE_AMOUNT} FCFA. Veuillez recharger.")

            # Déduction du ticket de jeu
            acc.wallet -= MISE_AMOUNT
            acc.total_spent += MISE_AMOUNT
            acc.games_played += 1
            save_accounts_to_disk(self.accounts)

            if self.state.total_players >= MAX_PLAYERS:
                raise ValueError("Serveur de jeu complet")

            # Nettoyage des anciennes sessions fantômes
            old_pid = self._find_player_by_account(account_id)
            if old_pid:
                self.mgr.disconnect(old_pid)
                self.players.pop(old_pid, None)

            pid = str(uuid.uuid4())
            grp = min(self.state.total_players // GROUP_SIZE, 9)
            player = Player(id=account_id, name=acc.name, group=grp,
                            phone=acc.phone, email=acc.email,
                            mise=MISE_AMOUNT, paid=True)
            self.players[pid] = player

            # Remplissage par les bots de simulation
            bots = random.randint(50, 150)
            self.state.total_players += bots + 1
            self.state.groups[grp] += 1

            log.info(f"[INSCRIPTION JEU] {acc.name} a rejoint. Solde restant : {acc.wallet} FCFA")

            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self._game_loop())

            return pid, grp, acc.wallet

    # ── TEMPS REEL : COMPTE DES CLICS ───────────────────────────────────────────
    async def player_click(self, pid: str) -> dict:
        async with self._lock: # Évite les double-gains sur clic simultané
            player = self.players.get(pid)
            if not player:
                return {"ok": False, "reason": "not_found"}
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

                winner = Winner(rank=rank, name=player.name, time=elapsed, prize=prize)
                self.state.winners.append(asdict(winner))

                await self.mgr.broadcast({
                    "type": "winner_added",
                    "winner": asdict(winner),
                    "total": len(self.state.winners)
                })

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
                    save_accounts_to_disk(self.accounts)

                    await self.mgr.send(pid, {
                        "type": "prize_sent",
                        "amount": prize,
                        "new_balance": acc.wallet,
                        "message": f"🏆 Gagné ! {prize:,} FCFA ajoutés au portefeuille.",
                    })

                return {"ok": True, "rank": rank, "prize": prize, "time": elapsed}
            else:
                acc = self.accounts.get(player.email)
                if acc:
                    entry = asdict(HistoryEntry(
                        round=self.state.round, timestamp=time.time(),
                        mise=MISE_AMOUNT, rank=None, prize=0, result="too_late"
                    ))
                    acc.history.insert(0, entry)
                    save_accounts_to_disk(self.accounts)
                return {"ok": False, "reason": "too_late"}

    # ── BOUCLE DE JEU AUTOMATIQUE ─────────────────────────────────────────────
    async def _game_loop(self):
        while True:
            try:
                await self._run_countdown()
                await self._run_round()
                await self._end_round()
                await asyncio.sleep(8) # Pause entre les manches
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Erreur Loop: {e}", exc_info=True)
                await asyncio.sleep(2)

    async def _run_countdown(self):
        self.state.phase = "countdown"
        self.bot_clicks = sorted(random.uniform(0.2, ROUND_DURATION - 5) for _ in range(WINNERS_COUNT * 2))
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

        await self.mgr.broadcast({
            "type": "round_start",
            "round": self.state.round,
            "duration": ROUND_DURATION
        })

        end_time = self.state.round_start + ROUND_DURATION
        bot_idx  = 0

        while time.time() < end_time and self.state.phase == "active":
            elapsed = time.time() - self.state.round_start
            remaining = max(0.0, ROUND_DURATION - elapsed)

            while bot_idx < len(self.bot_clicks) and self.bot_clicks[bot_idx] <= elapsed:
                if len(self.state.winners) < WINNERS_COUNT:
                    rank  = len(self.state.winners) + 1
                    prize = PRIZES[rank - 1]
                    bname = f"Joueur_{random.randint(1000,9999)}"
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
                "remaining": round(remaining, 1),
                "winners_count": len(self.state.winners)
            })

            if len(self.state.winners) >= WINNERS_COUNT:
                break
            await asyncio.sleep(0.1)

    async def _end_round(self):
        self.state.phase = "ended"
        for pid, player in list(self.players.items()):
            if player.paid and not player.clicked:
                acc = self.accounts.get(player.email)
                if acc:
                    entry = asdict(HistoryEntry(
                        round=self.state.round, timestamp=time.time(),
                        mise=MISE_AMOUNT, rank=None, prize=0, result="loss"
                    ))
                    acc.history.insert(0, entry)
                    save_accounts_to_disk(self.accounts)

        await self.mgr.broadcast({
            "type": "round_end",
            "round": self.state.round,
            "winners": self.state.winners
        })

        self.players.clear()
        self.state.round += 1

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
app = FastAPI(title="ONE TOUCH MILLION", version="6.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_static = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static):
    app.mount("/static", StaticFiles(directory=_static), name="static")

manager = ConnectionManager()
engine  = GameEngine(manager)

# ─── ROUTAGE APIS ─────────────────────────────────────────────────────────────
@app.post("/api/auth/register")
async def auth_register(body: dict):
    name, phone, email, pwd = (body.get("name") or "").strip(), (body.get("phone") or "").strip(), (body.get("email") or "").strip().lower(), (body.get("password") or "").strip()
    if not name or not phone or "@" not in email or len(pwd) < 6: raise HTTPException(400, "Données d'inscription invalides")
    try:
        acc = await engine.create_account(name, phone, email, pwd)
        return {"account_id": acc.id, "name": acc.name, "email": acc.email, "phone": acc.phone, "wallet": acc.wallet}
    except ValueError as e: raise HTTPException(409, str(e))

@app.post("/api/auth/login")
async def auth_login(body: dict):
    email, pwd = (body.get("email") or "").strip().lower(), (body.get("password") or "").strip()
    try:
        acc = await engine.login(email, pwd)
        return {"account_id": acc.id, "name": acc.name, "email": acc.email, "phone": acc.phone, "wallet": acc.wallet, "total_gains": acc.total_gains}
    except ValueError as e: raise HTTPException(401, str(e))

@app.get("/api/wallet/{account_id}")
async def get_wallet(account_id: str):
    acc = engine.accounts_by_id.get(account_id)
    if not acc: raise HTTPException(404, "Introuvable")
    return {"wallet": acc.wallet, "total_gains": acc.total_gains, "games_played": acc.games_played}

# L'ENDPOINT DEMANDE : L'utilisateur recharge son compte ici
@app.post("/api/deposit/fake")
async def deposit_fake(body: dict):
    account_id = body.get("account_id")
    amount     = int(body.get("amount") or 0)
    if not account_id or amount <= 0: raise HTTPException(400, "Paramètres invalides")
    try:
        return await engine.fake_deposit(account_id, amount)
    except ValueError as e: raise HTTPException(400, str(e))

# INSCRIPTION AU JEU : Accessible immédiatement dès que le solde est supérieur à 500 FCFA
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
    except ValueError as e: raise HTTPException(400, str(e))

@app.get("/api/history/{account_id}")
async def get_history(account_id: str):
    acc = engine.accounts_by_id.get(account_id)
    if not acc: raise HTTPException(404, "Compte introuvable")
    return {"history": acc.history, "total": len(acc.history)}

@app.get("/api/profile/{account_id}")
async def get_profile(account_id: str):
    acc = engine.accounts_by_id.get(account_id)
    if not acc: raise HTTPException(404, "Compte introuvable")
    return {"id": acc.id, "name": acc.name, "wallet": acc.wallet, "games_played": acc.games_played, "games_won": acc.games_won}

# ─── CANAL TEMPS REEL WEBSOCKET ───────────────────────────────────────────────
@app.websocket("/ws/{player_id}")
async def ws_player(ws: WebSocket, player_id: str):
    player = engine.players.get(player_id)
    if not player:
        await ws.close(code=4001, reason="Inscrivez-vous via /api/join d'abord")
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
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(player_id)

@app.get("/")
async def root():
    idx = os.path.join(_static, "index.html")
    return FileResponse(idx) if os.path.exists(idx) else HTMLResponse("<h1>ONE TOUCH MILLION RUNNING</h1>")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
