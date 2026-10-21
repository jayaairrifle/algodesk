"""AlgoDesk backend: receives signed telemetry from AlgoDeskAgent.mq5, queues
dashboard commands for it, and serves the dashboard with session login + RBAC."""

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .db import DB
from .fx import UsdIdr
from .stats import closed_trades, ea_stats
from .security import hash_password, new_token, sign, token_hash, verify_password

API = "/api"
COOKIE = "algodesk_session"
SESSION_TTL = 12 * 3600
SIGNATURE_SKEW = 30           # seconds of clock difference accepted from the agent
COMMAND_TTL = 120             # unacknowledged commands expire after this many seconds
OFFLINE_AFTER = 15            # agent shown offline when telemetry is older than this
SAMPLE_EVERY = 30             # equity chart resolution in seconds
SAMPLE_KEEP = 8 * 86400
MAX_RANGE_DAYS = 366          # longest custom period of the trade and balance history
MAX_BODY = 512 * 1024
DEALS_PER_POST = 500
DEAL_KEEP = 100 * 86400       # deal history kept per account
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}
LOGIN_MAX_FAILS = 5
LOGIN_LOCK = 300

ROLES = ("admin", "trader", "viewer")
COMMAND_ROLES = {
    "pause_entry": ("admin", "trader"),
    "resume_entry": ("admin", "trader"),
    "disable_magic": ("admin", "trader"),
    "enable_magic": ("admin", "trader"),
    "set_limit": ("admin",),
    "sr_set": ("admin", "trader"),
    "sr_close": ("admin", "trader"),
    "risk_set": ("admin",),
    "risk_reset": ("admin",),
    "session_set": ("admin",),
}
SESSIONS = r"(asia|london|newyork|overlap)"
HHMM_RANGE = r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$"
PARAM_FIELDS = ("enabled", "symbol", "lots", "dist", "step", "mode", "allow", "asia_hours", "london_hours", "newyork_hours")
STATIC = Path(__file__).parent / "static"


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class CommandIn(BaseModel):
    type: Literal["pause_entry", "resume_entry", "disable_magic", "enable_magic", "set_limit", "sr_set", "sr_close",
                  "risk_set", "risk_reset", "session_set"]
    magic: int | None = None
    value: float | None = None
    agent_id: str | None = None
    # sr_set (StopReverse strategy in the agent): only the fields given are changed
    enabled: bool | None = None
    symbol: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._#+-]{1,32}$")
    lots: float | None = Field(default=None, gt=0, le=100)
    dist: float | None = Field(default=None, gt=0, le=10000)
    step: float | None = Field(default=None, ge=0, le=10000)
    # risk_set (daily loss protection): on/off, and the limit as a percentage of the day's baseline or a fixed amount
    mode: Literal["pct", "amount"] | None = None
    # session_set (trading-session filter): allowed sessions, and each session's hours in its own city's local time.
    # The hours keys must not equal a session name: the agent's JSON reader finds a key by its quoted name.
    allow: str | None = Field(default=None, pattern=rf"^{SESSIONS}(,{SESSIONS}){{0,3}}$")
    asia_hours: str | None = Field(default=None, pattern=HHMM_RANGE)
    london_hours: str | None = Field(default=None, pattern=HHMM_RANGE)
    newyork_hours: str | None = Field(default=None, pattern=HHMM_RANGE)

    def params(self) -> dict | None:
        p = {k: getattr(self, k) for k in PARAM_FIELDS if getattr(self, k) is not None}
        return p or None


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=80)
    role: Literal["admin", "trader", "viewer"]
    password: str = Field(min_length=10, max_length=256)


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    role: Literal["admin", "trader", "viewer"] | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=256)


class EaIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scope: str = Field(default="", max_length=120)


def command_out(row) -> dict:
    """A command row with its settings (params) flattened in, as the agent reads them."""
    out = dict(row)
    params = out.pop("params", None)
    return out | (json.loads(params) if params else {})


def user_out(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "active": bool(row["active"]),
        "last_login": row["last_login"],
    }


def create_app(db_path: str | None = None, cookie_secure: bool | None = None) -> FastAPI:
    db = DB(db_path or os.environ.get("ALGODESK_DB", "algodesk.db"))
    if cookie_secure is None:
        cookie_secure = os.environ.get("ALGODESK_COOKIE_SECURE", "1") != "0"

    app = FastAPI(title="AlgoDesk", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.state.fx = UsdIdr()

    seen_signatures: dict[str, float] = {}
    login_fails: dict[str, list[float]] = {}
    guard = threading.Lock()

    # ------------------------------------------------------------------ middleware
    @app.middleware("http")
    async def protect(request: Request, call_next):
        path = request.url.path
        # Browser writes must be JSON: a cross-site form cannot send that without CORS preflight.
        if (
            path.startswith(API + "/")
            and not path.startswith(API + "/agent/")
            and request.method in ("POST", "PUT", "PATCH", "DELETE")
            and not request.headers.get("content-type", "").startswith("application/json")
        ):
            return JSONResponse({"detail": "Content-Type harus application/json"}, status_code=415)
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"detail": "Body terlalu besar"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none'"
        )
        return response

    # ------------------------------------------------------------------ agent auth
    async def agent_auth(request: Request) -> tuple[str, dict | None]:
        agent_id = request.headers.get("x-algodesk-agent", "")
        ts = request.headers.get("x-algodesk-timestamp", "")
        sig = request.headers.get("x-algodesk-signature", "")
        if not (agent_id and ts and sig):
            raise HTTPException(401, "Header tanda tangan agent tidak lengkap")
        row = db.one("SELECT secret FROM agents WHERE agent_id = ? AND active = 1", (agent_id,))
        if row is None:
            raise HTTPException(401, "Agent tidak dikenal")
        try:
            ts_int = int(ts)
        except ValueError:
            raise HTTPException(401, "Timestamp tidak valid")
        now = time.time()
        if abs(now - ts_int) > SIGNATURE_SKEW:
            raise HTTPException(401, "Timestamp di luar toleransi; periksa jam VPS")
        raw = await request.body()
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "Body bukan UTF-8")
        path = request.url.path[len(API):] if request.url.path.startswith(API) else request.url.path
        expected = sign(row["secret"], ts, request.method, path, body)
        if not secrets.compare_digest(expected, sig.lower()):
            raise HTTPException(401, "Tanda tangan tidak cocok")
        # GET /agent/commands is a harmless read and may legitimately repeat within a second.
        if request.method != "GET":
            with guard:
                for k, t in list(seen_signatures.items()):
                    if now - t > SIGNATURE_SKEW * 2:
                        del seen_signatures[k]
                if sig in seen_signatures:
                    raise HTTPException(401, "Request diputar ulang")
                seen_signatures[sig] = now
        db.run("UPDATE agents SET last_seen = ? WHERE agent_id = ?", (now, agent_id))
        payload = None
        if body:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                raise HTTPException(400, "Body bukan JSON")
        return agent_id, payload

    # ------------------------------------------------------------------ user auth
    def current_user(request: Request):
        token = request.cookies.get(COOKIE)
        if not token:
            raise HTTPException(401, "Belum login")
        row = db.one(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
            (token_hash(token), time.time()),
        )
        if row is None:
            raise HTTPException(401, "Sesi berakhir, silakan login lagi")
        return row

    def require(*roles: str):
        def dep(user=Depends(current_user)):
            if user["role"] not in roles:
                raise HTTPException(403, "Peran Anda tidak memiliki akses untuk tindakan ini")
            return user
        return dep

    def pick_agent(agent_id: str | None) -> str | None:
        if agent_id:
            row = db.one("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,))
        else:
            row = db.one("SELECT agent_id FROM agents WHERE active = 1 ORDER BY last_seen DESC NULLS LAST, created_at LIMIT 1")
        return row["agent_id"] if row else None

    # ------------------------------------------------------------------ agent endpoints
    @app.post(API + "/agent/telemetry")
    def agent_telemetry(auth=Depends(agent_auth)):
        agent_id, payload = auth
        if not isinstance(payload, dict):
            raise HTTPException(422, "Telemetri harus berupa objek JSON")
        account = payload.get("account") or {}
        equity, balance = account.get("equity"), account.get("balance")
        if not isinstance(equity, (int, float)) or not isinstance(balance, (int, float)):
            raise HTTPException(422, "account.equity dan account.balance wajib berupa angka")
        now = time.time()
        db.run(
            "INSERT INTO telemetry_latest (agent_id, received_at, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET received_at = excluded.received_at, payload = excluded.payload",
            (agent_id, now, json.dumps(payload)),
        )
        login = account.get("login") if isinstance(account.get("login"), int) else None
        last = db.one("SELECT ts, login FROM equity_samples WHERE agent_id = ? ORDER BY ts DESC LIMIT 1", (agent_id,))
        if last is None or last["login"] != login or now - last["ts"] >= SAMPLE_EVERY:
            db.run("INSERT INTO equity_samples (agent_id, ts, equity, balance, login) VALUES (?, ?, ?, ?, ?)",
                   (agent_id, now, equity, balance, login))
            db.run("DELETE FROM equity_samples WHERE ts < ?", (now - SAMPLE_KEEP,))
        return {"ok": True}

    @app.post(API + "/agent/deals")
    def agent_deals(auth=Depends(agent_auth)):
        """Deal history from the terminal, sent in batches; re-sent deals are simply overwritten."""
        agent_id, payload = auth
        if not isinstance(payload, dict) or not isinstance(payload.get("login"), int) or not isinstance(payload.get("deals"), list):
            raise HTTPException(422, "login dan deals wajib diisi")
        deals = payload["deals"]
        if len(deals) > DEALS_PER_POST:
            raise HTTPException(413, f"Maksimal {DEALS_PER_POST} deal per kiriman")
        login = payload["login"]
        rows = []
        for d in deals:
            if not isinstance(d, dict) or not isinstance(d.get("ticket"), int) or not isinstance(d.get("time"), int):
                raise HTTPException(422, "Setiap deal wajib punya ticket dan time")
            def num(k):
                v = d.get(k)
                return float(v) if isinstance(v, (int, float)) else 0.0
            rows.append((
                login, d["ticket"], agent_id, int(d.get("position") or 0), d["time"],
                str(d.get("type", ""))[:12], str(d.get("entry", ""))[:8], str(d.get("symbol", ""))[:32],
                num("volume"), num("price"), num("profit"), num("swap"), num("commission"), num("fee"),
                int(d.get("magic") or 0), str(d.get("comment", ""))[:64],
            ))
        with db.lock:
            db.conn.executemany(
                "INSERT INTO deals (login, ticket, agent_id, position_id, time, type, entry, symbol, volume, price, "
                "profit, swap, commission, fee, magic, comment) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(login, ticket) DO UPDATE SET position_id = excluded.position_id, time = excluded.time, "
                "type = excluded.type, entry = excluded.entry, symbol = excluded.symbol, volume = excluded.volume, "
                "price = excluded.price, profit = excluded.profit, swap = excluded.swap, commission = excluded.commission, "
                "fee = excluded.fee, magic = excluded.magic, comment = excluded.comment",
                rows,
            )
            newest = max((r[4] for r in rows), default=0)
            if newest:
                db.conn.execute("DELETE FROM deals WHERE login = ? AND time < ?", (login, newest - DEAL_KEEP))
        return {"ok": True, "stored": len(rows)}

    @app.get(API + "/agent/commands")
    def agent_commands(auth=Depends(agent_auth)):
        agent_id, _ = auth
        now = time.time()
        db.run(
            "UPDATE commands SET status = 'expired' WHERE agent_id = ? AND status = 'pending' AND issued_at < ?",
            (agent_id, now - COMMAND_TTL),
        )
        rows = db.all(
            "SELECT c.id, c.type, c.magic, c.value, c.params, u.display_name AS issued_by FROM commands c "
            "LEFT JOIN users u ON u.id = c.issued_by WHERE c.agent_id = ? AND c.status = 'pending' ORDER BY c.issued_at",
            (agent_id,),
        )
        for r in rows:
            db.run("UPDATE commands SET delivered_at = COALESCE(delivered_at, ?) WHERE id = ?", (now, r["id"]))
        return {"commands": [command_out(r) for r in rows]}

    @app.post(API + "/agent/commands/ack")
    def agent_ack(auth=Depends(agent_auth)):
        agent_id, payload = auth
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise HTTPException(422, "id wajib diisi")
        row = db.one("SELECT status FROM commands WHERE id = ? AND agent_id = ?", (payload["id"], agent_id))
        if row is None:
            raise HTTPException(404, "Perintah tidak ditemukan")
        # A late ack after expiry still records what the terminal actually did.
        if row["status"] in ("pending", "expired"):
            db.run(
                "UPDATE commands SET status = ?, acked_at = ?, result = ?, entry_state = ? WHERE id = ?",
                (
                    "acked" if payload.get("ok") else "failed",
                    time.time(),
                    str(payload.get("message", ""))[:300],
                    str(payload.get("entry_state", ""))[:20],
                    payload["id"],
                ),
            )
        return {"ok": True}

    # ------------------------------------------------------------------ auth endpoints
    @app.post(API + "/auth/login")
    def login(body: LoginIn, response: Response):
        key = body.username.lower()
        now = time.time()
        with guard:
            fails = [t for t in login_fails.get(key, []) if now - t < LOGIN_LOCK]
            login_fails[key] = fails
            if len(fails) >= LOGIN_MAX_FAILS:
                raise HTTPException(429, "Terlalu banyak percobaan gagal. Coba lagi dalam 5 menit.")
        row = db.one("SELECT * FROM users WHERE username = ? AND active = 1", (body.username,))
        if row is None or not verify_password(body.password, row["pw_hash"]):
            with guard:
                login_fails.setdefault(key, []).append(now)
            raise HTTPException(401, "Nama pengguna atau kata sandi salah")
        with guard:
            login_fails.pop(key, None)
        token = new_token()
        db.run("DELETE FROM sessions WHERE expires_at < ?", (now,))
        db.run("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)", (token_hash(token), row["id"], now + SESSION_TTL))
        db.run("UPDATE users SET last_login = ? WHERE id = ?", (now, row["id"]))
        response.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="strict", secure=cookie_secure, path="/")
        return user_out(row)

    @app.post(API + "/auth/logout")
    def logout(request: Request, response: Response):
        token = request.cookies.get(COOKIE)
        if token:
            db.run("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get(API + "/auth/me")
    def me(user=Depends(current_user)):
        return user_out(user)

    # ------------------------------------------------------------------ dashboard data
    @app.get(API + "/state")
    def state(agent_id: str | None = None, user=Depends(current_user)):
        now = time.time()
        agents = [dict(r) for r in db.all("SELECT agent_id, last_seen FROM agents WHERE active = 1 ORDER BY agent_id")]
        chosen = pick_agent(agent_id)
        tel = db.one("SELECT received_at, payload FROM telemetry_latest WHERE agent_id = ?", (chosen,)) if chosen else None
        eas = [dict(r) for r in db.all("SELECT magic, name, scope FROM eas ORDER BY magic")]
        return {
            "server_time": now,
            "agents": agents,
            "agent": None if chosen is None else {
                "agent_id": chosen,
                "received_at": tel["received_at"] if tel else None,
                "age": (now - tel["received_at"]) if tel else None,
                "online": bool(tel and now - tel["received_at"] <= OFFLINE_AFTER),
            },
            "telemetry": json.loads(tel["payload"]) if tel else None,
            "eas": eas,
        }

    @app.get(API + "/equity")
    def equity(hours: float = 24, since: int | None = None, until: int | None = None,
               agent_id: str | None = None, user=Depends(current_user)):
        """Equity samples of the last `hours`, or of [since, until) (real epoch) for a custom period."""
        chosen = pick_agent(agent_id)
        if chosen is None:
            return {"samples": []}
        end = until if until is not None else time.time() + 60
        since = since if since is not None else time.time() - max(1.0, min(hours, 192.0)) * 3600
        # Only the account the terminal is logged in to now; samples of a previous account are left out.
        tel = db.one("SELECT payload FROM telemetry_latest WHERE agent_id = ?", (chosen,))
        login = (json.loads(tel["payload"]).get("account") or {}).get("login") if tel else None
        rows = db.all("SELECT ts, equity, balance FROM equity_samples WHERE agent_id = ? AND ts >= ? AND ts < ? AND login IS ? ORDER BY ts",
                      (chosen, since, end, login))
        return {"samples": [dict(r) for r in rows]}

    # ------------------------------------------------------------------ deal history & EA statistics
    def current_account(agent_id: str | None):
        """Login, trading-day start (server time) and balance of the account the terminal is on now."""
        chosen = pick_agent(agent_id)
        tel = db.one("SELECT payload FROM telemetry_latest WHERE agent_id = ?", (chosen,)) if chosen else None
        if tel is None:
            return None, None, None
        t = json.loads(tel["payload"])
        account = t.get("account") or {}
        day_start = (t.get("protection") or {}).get("day_start") or int(t.get("server_time") or time.time()) // 86400 * 86400
        return account.get("login"), int(day_start), account.get("balance")

    def account_deals(login, since: int):
        return [dict(r) for r in db.all(
            "SELECT ticket, position_id, time, type, entry, symbol, volume, price, profit, swap, commission, fee, magic, comment "
            "FROM deals WHERE login = ? AND time >= ? ORDER BY time, ticket", (login, since))]

    @app.get(API + "/eas/stats")
    def eas_stats(period: Literal["day", "week", "month"] = "day", agent_id: str | None = None, user=Depends(current_user)):
        login, day_start, balance = current_account(agent_id)
        if login is None:
            return {"period": period, "start": None, "eas": []}
        start = day_start - (PERIOD_DAYS[period] - 1) * 86400
        # opening deals of trades closed in the period can be older than the period itself
        trades = closed_trades(account_deals(login, start - 30 * 86400), start)
        return {"period": period, "start": start, "eas": ea_stats(trades, balance)}

    def period(days: int, start: int | None, end: int | None, day_start: int):
        """[start, end) in server time: the last `days` trading days, or a custom range of whole days
        (`start` and `end` are day starts, `end` included)."""
        if start is None and end is None:
            return day_start - (max(1, min(days, 90)) - 1) * 86400, None
        if start is None or end is None:
            raise HTTPException(422, "start and end go together")
        start, end = start // 86400 * 86400, end // 86400 * 86400 + 86400
        if start >= end or end - start > MAX_RANGE_DAYS * 86400:
            raise HTTPException(422, f"range must be 1..{MAX_RANGE_DAYS} days")
        return start, end

    @app.get(API + "/trades")
    def trades(days: int = 7, start: int | None = None, end: int | None = None, magic: int | None = None,
               agent_id: str | None = None, user=Depends(current_user)):
        login, day_start, _ = current_account(agent_id)
        if login is None:
            return {"trades": []}
        start, end = period(days, start, end, day_start)
        out = closed_trades(account_deals(login, start - 30 * 86400), start, end)
        if magic is not None:
            out = [t for t in out if t["magic"] == magic]
        return {"start": start, "end": end, "trades": out[::-1][:1000]}

    @app.get(API + "/balance")
    def balance_history(days: int = 7, start: int | None = None, end: int | None = None,
                        agent_id: str | None = None, user=Depends(current_user)):
        """Balance after every deal of the period (trades, swaps, deposits, withdrawals), rebuilt
        backwards from today's balance. Times are broker server time."""
        login, day_start, bal = current_account(agent_id)
        if login is None or bal is None:
            return {"start": None, "points": []}
        start, end = period(days, start, end, day_start)
        ds = account_deals(login, start)
        net = [d["profit"] + d["swap"] + d["commission"] + d["fee"] for d in ds]
        b = bal - sum(net)
        points = [[start, round(b, 2)]]
        for d, v in zip(ds, net):
            if end is not None and d["time"] >= end:
                break
            if v:
                b += v
                points.append([d["time"], round(b, 2)])
        return {"start": start, "end": end, "points": points, "end_balance": round(b, 2)}

    @app.get(API + "/deals")
    def deals(limit: int = 200, agent_id: str | None = None, user=Depends(current_user)):
        """Today's deals, newest first: the trade events of the EA log."""
        login, day_start, _ = current_account(agent_id)
        if login is None:
            return {"deals": []}
        rows = db.all(
            "SELECT ticket, position_id, time, type, entry, symbol, volume, price, profit, swap, commission, fee, magic, comment "
            "FROM deals WHERE login = ? AND time >= ? ORDER BY time DESC, ticket DESC LIMIT ?",
            (login, day_start, max(1, min(limit, 1000))))
        return {"start": day_start, "deals": [dict(r) for r in rows]}

    @app.get(API + "/fx")
    def fx(history: bool = False, user=Depends(current_user)):
        """USD/IDR from a free public API (live USDT/IDR market first), optionally with the last 24 hours."""
        return app.state.fx.get(history)

    # ------------------------------------------------------------------ commands
    @app.post(API + "/commands")
    def create_command(body: CommandIn, user=Depends(current_user)):
        if user["role"] not in COMMAND_ROLES[body.type]:
            raise HTTPException(403, "Peran Anda tidak boleh mengirim perintah ini")
        if body.type in ("disable_magic", "enable_magic") and (body.magic is None or body.magic <= 0):
            raise HTTPException(422, "Magic number wajib diisi")
        if body.type == "set_limit" and (body.value is None or not 0.5 <= body.value <= 20):
            raise HTTPException(422, "Batas rugi harus antara 0,5% dan 20%")
        if body.type in ("sr_set", "session_set") and body.params() is None:
            raise HTTPException(422, "Tidak ada pengaturan yang diubah")
        if body.type == "risk_set":
            if body.enabled is None and body.mode is None:
                raise HTTPException(422, "Tidak ada pengaturan yang diubah")
            if body.mode is not None and body.value is None:
                raise HTTPException(422, "Nilai batas wajib diisi")
            if body.mode == "pct" and not 0.5 <= body.value <= 20:
                raise HTTPException(422, "Batas rugi harus antara 0,5% dan 20%")
            if body.mode == "amount" and not 0 < body.value <= 1e12:
                raise HTTPException(422, "Nominal batas rugi harus lebih dari 0")
        chosen = pick_agent(body.agent_id)
        if chosen is None:
            raise HTTPException(404, "Belum ada agent terdaftar")
        cid = "cmd_" + secrets.token_hex(8)
        params = body.params()
        db.run(
            "INSERT INTO commands (id, agent_id, type, magic, value, params, issued_by, issued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, chosen, body.type, body.magic, body.value, json.dumps(params) if params else None, user["id"], time.time()),
        )
        return {"id": cid, "status": "pending"}

    def command_rows(where: str, args: tuple, limit: int = 1):
        return db.all(
            "SELECT c.id, c.agent_id, c.type, c.magic, c.value, c.params, c.issued_at, c.status, c.delivered_at, c.acked_at, "
            "c.result, c.entry_state, u.display_name AS issued_by, u.role AS issued_role FROM commands c "
            f"LEFT JOIN users u ON u.id = c.issued_by WHERE {where} ORDER BY c.issued_at DESC LIMIT ?",
            args + (limit,),
        )

    @app.get(API + "/commands")
    def list_commands(limit: int = 50, user=Depends(current_user)):
        db.run("UPDATE commands SET status = 'expired' WHERE status = 'pending' AND issued_at < ?", (time.time() - COMMAND_TTL,))
        return {"commands": [command_out(r) for r in command_rows("1 = 1", (), max(1, min(limit, 500)))]}

    @app.get(API + "/commands/{cid}")
    def get_command(cid: str, user=Depends(current_user)):
        db.run("UPDATE commands SET status = 'expired' WHERE id = ? AND status = 'pending' AND issued_at < ?", (cid, time.time() - COMMAND_TTL))
        rows = command_rows("c.id = ?", (cid,))
        if not rows:
            raise HTTPException(404, "Perintah tidak ditemukan")
        return command_out(rows[0])

    # ------------------------------------------------------------------ EA registry
    @app.put(API + "/eas/{magic}")
    def put_ea(magic: int, body: EaIn, user=Depends(require("admin"))):
        if magic <= 0:
            raise HTTPException(422, "Magic number harus positif")
        db.run(
            "INSERT INTO eas (magic, name, scope) VALUES (?, ?, ?) ON CONFLICT(magic) DO UPDATE SET name = excluded.name, scope = excluded.scope",
            (magic, body.name, body.scope),
        )
        return {"magic": magic, "name": body.name, "scope": body.scope}

    @app.delete(API + "/eas/{magic}")
    def delete_ea(magic: int, user=Depends(require("admin"))):
        db.run("DELETE FROM eas WHERE magic = ?", (magic,))
        return {"ok": True}

    # ------------------------------------------------------------------ users
    @app.get(API + "/users")
    def list_users(user=Depends(require("admin"))):
        return {"users": [user_out(r) for r in db.all("SELECT * FROM users ORDER BY id")]}

    @app.post(API + "/users")
    def create_user(body: UserIn, user=Depends(require("admin"))):
        if db.one("SELECT 1 FROM users WHERE username = ?", (body.username,)):
            raise HTTPException(409, "Nama pengguna sudah dipakai")
        cur = db.run(
            "INSERT INTO users (username, display_name, role, pw_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (body.username, body.display_name, body.role, hash_password(body.password), time.time()),
        )
        return user_out(db.one("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)))

    @app.patch(API + "/users/{uid}")
    def patch_user(uid: int, body: UserPatch, user=Depends(require("admin"))):
        row = db.one("SELECT * FROM users WHERE id = ?", (uid,))
        if row is None:
            raise HTTPException(404, "Pengguna tidak ditemukan")
        if uid == user["id"] and (body.role not in (None, "admin") or body.active is False):
            raise HTTPException(400, "Anda tidak bisa menurunkan peran atau menonaktifkan akun sendiri")
        if body.display_name is not None:
            db.run("UPDATE users SET display_name = ? WHERE id = ?", (body.display_name, uid))
        if body.role is not None:
            db.run("UPDATE users SET role = ? WHERE id = ?", (body.role, uid))
        if body.active is not None:
            db.run("UPDATE users SET active = ? WHERE id = ?", (int(body.active), uid))
        if body.password is not None:
            db.run("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(body.password), uid))
        if body.active is False or body.password is not None or (body.role is not None and body.role != row["role"]):
            db.run("DELETE FROM sessions WHERE user_id = ?", (uid,))
        return user_out(db.one("SELECT * FROM users WHERE id = ?", (uid,)))

    # ------------------------------------------------------------------ dashboard page
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app

