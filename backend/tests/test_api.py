import json
import time

import pytest
from fastapi.testclient import TestClient

from algodesk.app import create_app
from algodesk.security import hash_password, sign

SECRET = "s" * 32
TELEMETRY = {
    "agent_id": "vps-01",
    "account": {"login": 884921, "balance": 10246.38, "equity": 10203.39, "currency": "USD"},
    "protection": {"baseline": 10246.38, "limit_pct": 3.0, "loss_used": 42.99, "entry_state": "allowed", "disabled_magics": []},
    "positions": [],
    "symbols": [],
}


@pytest.fixture
def app(tmp_path):
    app = create_app(str(tmp_path / "t.db"), cookie_secure=False)
    db = app.state.db
    now = time.time()
    db.run("INSERT INTO agents (agent_id, secret, created_at) VALUES ('vps-01', ?, ?)", (SECRET, now))
    for name, role in (("admin", "admin"), ("trader", "trader"), ("viewer", "viewer")):
        db.run(
            "INSERT INTO users (username, display_name, role, pw_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, name.title(), role, hash_password("password-123"), now),
        )
    return app


def agent_call(client, method, path, payload=None, secret=SECRET, ts=None, agent="vps-01"):
    body = json.dumps(payload) if payload is not None else ""
    ts = str(ts or int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-AlgoDesk-Agent": agent,
        "X-AlgoDesk-Timestamp": ts,
        "X-AlgoDesk-Signature": sign(secret, ts, method, path, body),
    }
    return client.request(method, "/api" + path, content=body.encode() if body else None, headers=headers)


def login(app, username):
    client = TestClient(app)
    r = client.post("/api/auth/login", json={"username": username, "password": "password-123"})
    assert r.status_code == 200, r.text
    return client


def test_signed_telemetry_is_stored_and_visible(app):
    agent = TestClient(app)
    assert agent_call(agent, "POST", "/agent/telemetry", TELEMETRY).status_code == 200
    state = login(app, "viewer").get("/api/state").json()
    assert state["agent"]["online"] is True
    assert state["telemetry"]["account"]["equity"] == 10203.39
    samples = login(app, "viewer").get("/api/equity").json()["samples"]
    assert len(samples) == 1 and samples[0]["equity"] == 10203.39



def test_equity_chart_follows_the_current_account(app):
    agent = TestClient(app)
    assert agent_call(agent, "POST", "/agent/telemetry", TELEMETRY).status_code == 200
    other = TELEMETRY | {"account": TELEMETRY["account"] | {"login": 555001, "equity": 1500.0, "balance": 1500.0}}
    assert agent_call(agent, "POST", "/agent/telemetry", other).status_code == 200
    samples = login(app, "viewer").get("/api/equity").json()["samples"]
    assert [s["equity"] for s in samples] == [1500.0]


@pytest.mark.parametrize("kwargs, detail", [
    ({"secret": "x" * 32}, "Tanda tangan"),
    ({"ts": int(time.time()) - 120}, "Timestamp"),
    ({"agent": "unknown"}, "Agent tidak dikenal"),
])
def test_bad_agent_requests_are_rejected(app, kwargs, detail):
    r = agent_call(TestClient(app), "POST", "/agent/telemetry", TELEMETRY, **kwargs)
    assert r.status_code == 401
    assert detail in r.json()["detail"]


def test_replayed_request_is_rejected(app):
    client = TestClient(app)
    ts = int(time.time())
    assert agent_call(client, "POST", "/agent/telemetry", TELEMETRY, ts=ts).status_code == 200
    r = agent_call(client, "POST", "/agent/telemetry", TELEMETRY, ts=ts)
    assert r.status_code == 401 and "diputar ulang" in r.json()["detail"]


def test_command_round_trip(app):
    trader = login(app, "trader")
    cid = trader.post("/api/commands", json={"type": "pause_entry"}).json()["id"]
    assert trader.get(f"/api/commands/{cid}").json()["status"] == "pending"

    agent = TestClient(app)
    cmds = agent_call(agent, "GET", "/agent/commands").json()["commands"]
    assert [c["id"] for c in cmds] == [cid]
    assert cmds[0]["issued_by"] == "Trader"

    ack = {"id": cid, "ok": True, "message": "entry dijeda", "entry_state": "paused"}
    assert agent_call(agent, "POST", "/agent/commands/ack", ack).status_code == 200
    done = trader.get(f"/api/commands/{cid}").json()
    assert done["status"] == "acked" and done["entry_state"] == "paused"
    assert agent_call(agent, "GET", "/agent/commands").json()["commands"] == []


def test_unacked_command_expires(app):
    trader = login(app, "trader")
    cid = trader.post("/api/commands", json={"type": "pause_entry"}).json()["id"]
    app.state.db.run("UPDATE commands SET issued_at = issued_at - 1000 WHERE id = ?", (cid,))
    assert agent_call(TestClient(app), "GET", "/agent/commands").json()["commands"] == []
    assert trader.get(f"/api/commands/{cid}").json()["status"] == "expired"


def test_rbac_on_commands(app):
    assert login(app, "viewer").post("/api/commands", json={"type": "pause_entry"}).status_code == 403
    assert login(app, "trader").post("/api/commands", json={"type": "set_limit", "value": 2}).status_code == 403
    assert login(app, "admin").post("/api/commands", json={"type": "set_limit", "value": 2}).status_code == 200
    assert login(app, "admin").post("/api/commands", json={"type": "set_limit", "value": 50}).status_code == 422
    assert login(app, "trader").post("/api/commands", json={"type": "disable_magic"}).status_code == 422


def test_user_management_is_admin_only(app):
    new = {"username": "budi", "display_name": "Budi", "role": "viewer", "password": "password-456"}
    assert login(app, "trader").post("/api/users", json=new).status_code == 403
    admin = login(app, "admin")
    assert admin.post("/api/users", json=new).status_code == 200
    me = admin.get("/api/auth/me").json()
    assert admin.patch(f"/api/users/{me['id']}", json={"role": "viewer"}).status_code == 400


def test_deactivated_user_loses_session(app):
    viewer = login(app, "viewer")
    admin = login(app, "admin")
    vid = viewer.get("/api/auth/me").json()["id"]
    assert admin.patch(f"/api/users/{vid}", json={"active": False}).status_code == 200
    assert viewer.get("/api/state").status_code == 401


def test_login_lockout_after_failures(app):
    client = TestClient(app)
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password-123"})
    assert r.status_code == 429


def test_browser_writes_must_be_json(app):
    admin = login(app, "admin")
    r = admin.post("/api/commands", content="type=pause_entry", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_requires_login(app):
    assert TestClient(app).get("/api/state").status_code == 401


def deal(ticket, pos, t, typ, entry, net=0.0, magic=2001, symbol="XAUUSDc", price=2640.0, volume=0.1):
    return {"ticket": ticket, "position": pos, "time": t, "type": typ, "entry": entry, "symbol": symbol,
            "volume": volume, "price": price, "profit": net, "swap": 0, "commission": 0, "fee": 0,
            "magic": magic, "comment": ""}


def test_deal_history_gives_per_ea_stats(app):
    day = 1_800_000_000 // 86400 * 86400
    tel = TELEMETRY | {"server_time": day + 3600, "protection": TELEMETRY["protection"] | {"day_start": day}}
    agent = TestClient(app)
    assert agent_call(agent, "POST", "/agent/telemetry", tel).status_code == 200
    deals = [
        deal(1, 10, day - 86400 * 3, "BUY", "IN"),                  # opened three days ago
        deal(2, 10, day + 60, "SELL", "OUT", net=12.5, magic=0),    # closed today by SL/TP: magic 0
        deal(3, 11, day + 120, "SELL", "IN", magic=2002),
        deal(4, 11, day + 180, "BUY", "OUT", net=-4.0, magic=2002),
        deal(5, 12, day + 200, "BUY", "IN", magic=2002),
        deal(6, 12, day + 260, "SELL", "OUT", net=6.0, magic=2002),
        deal(7, 13, day - 86400 * 2, "BUY", "IN", magic=2002),
        deal(8, 13, day - 86400 * 2 + 60, "SELL", "OUT", net=3.0, magic=2002),
        {"ticket": 9, "position": 0, "time": day + 30, "type": "BALANCE", "entry": "IN", "profit": 500.0},
    ]
    r = agent_call(agent, "POST", "/agent/deals", {"login": 884921, "deals": deals})
    assert r.status_code == 200 and r.json()["stored"] == 9
    # sending the same deals again does not duplicate them
    assert agent_call(agent, "POST", "/agent/deals", {"login": 884921, "deals": deals[:2]}).status_code == 200

    viewer = login(app, "viewer")
    today = {e["magic"]: e for e in viewer.get("/api/eas/stats?period=day").json()["eas"]}
    assert today[2001]["trades"] == 1 and today[2001]["net"] == 12.5
    assert today[2002]["trades"] == 2 and today[2002]["win_rate"] == 50.0 and today[2002]["profit_factor"] == 1.5
    assert today[2002]["max_drawdown"] == 4.0
    week = {e["magic"]: e for e in viewer.get("/api/eas/stats?period=week").json()["eas"]}
    assert week[2002]["trades"] == 3 and week[2002]["net"] == 5.0
    trades = viewer.get("/api/trades?days=7&magic=2001").json()["trades"]
    assert trades[0]["side"] == "BUY" and trades[0]["open_time"] == day - 86400 * 3
    log = viewer.get("/api/deals").json()["deals"]
    assert [d["ticket"] for d in log] == [6, 5, 4, 3, 2, 9]


def test_deals_of_another_account_are_not_mixed_in(app):
    day = 1_800_000_000 // 86400 * 86400
    tel = TELEMETRY | {"protection": TELEMETRY["protection"] | {"day_start": day}}
    agent = TestClient(app)
    agent_call(agent, "POST", "/agent/telemetry", tel)
    agent_call(agent, "POST", "/agent/deals", {"login": 111, "deals": [deal(1, 5, day + 1, "BUY", "IN"), deal(2, 5, day + 9, "SELL", "OUT", net=9)]})
    assert login(app, "viewer").get("/api/eas/stats").json()["eas"] == []


def test_usd_idr_rate_falls_back_to_the_next_free_api():
    from algodesk.fx import UsdIdr

    def fetch(url):
        if "first" in url:
            raise OSError("down")
        if "second" in url:
            return {"rates": {"IDR": 3.2}}  # nonsense: rejected
        return {"usd": {"idr": 16250.5}}

    fx = UsdIdr(sources=[("a", "https://first", lambda d: d["rates"]["IDR"]),
                         ("b", "https://second", lambda d: d["rates"]["IDR"]),
                         ("c", "https://third", lambda d: d["usd"]["idr"])], fetch=fetch)
    fx.refresh()
    assert (fx.rate, fx.source) == (16250.5, "c")


def test_fx_endpoint_needs_login_and_returns_the_cached_rate(app):
    from algodesk.fx import UsdIdr

    app.state.fx = UsdIdr(sources=[("test", "https://x", lambda d: d["r"])], fetch=lambda url: {"r": 16000})
    app.state.fx.refresh()
    assert TestClient(app).get("/api/fx").status_code == 401
    out = login(app, "viewer").get("/api/fx").json()
    assert out["usd_idr"] == 16000 and out["source"] == "test"
