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
