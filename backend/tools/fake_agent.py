"""Stand-in for AlgoDeskAgent.mq5 so the dashboard can be tried without MT5.

It signs requests exactly like the MQL5 agent, sends telemetry for a
simulated demo account and applies dashboard commands the same way
(pause/resume, enable/disable magic, set_limit, daily loss block).

    python tools/fake_agent.py --url http://127.0.0.1:8000/api --agent vps-01 --secret <secret>
"""

import argparse
import hashlib
import hmac
import json
import random
import time
import urllib.error
import urllib.request

SYMBOLS = [
    # name, bid, digits, point, spread, rsi, atr points, structure
    ("EURUSD", 1.08462, 5, 0.00001, 9, 56.8, 632, "bull"),
    ("GBPUSD", 1.26750, 5, 0.00001, 12, 48.2, 748, "mixed"),
    ("XAUUSD", 2678.32, 2, 0.01, 19, 42.6, 12840, "bear"),
    ("USDJPY", 154.238, 3, 0.001, 12, 54.1, 327, "mixed"),
    ("NAS100", 20211.7, 1, 0.1, 14, 66.4, 1832, "bull"),
    ("AUDUSD", 0.65874, 5, 0.00001, 11, 51.3, 581, "mixed"),
    ("USDCAD", 1.37952, 5, 0.00001, 14, 61.7, 660, "bull"),
    ("USDCHF", 0.86311, 5, 0.00001, 13, 39.8, 615, "bear"),
]


class Sim:
    def __init__(self, loss_drift: float, usd_idr: float = 0.0):
        self.usd_idr = usd_idr
        self.symbols = [dict(zip(("symbol", "bid", "digits", "point", "spread", "rsi", "atr", "structure"), s)) for s in SYMBOLS]
        self.balance = 10246.38
        self.baseline = 10246.38
        self.day_balance = self.balance
        self.day_start = int(time.time()) // 86400 * 86400
        self.limit_pct = 3.0
        self.paused = False
        self.blocked = False
        self.disabled: set[int] = set()
        self.loss_drift = loss_drift
        self.positions = [
            {"ticket": 5001, "symbol": "EURUSD", "side": "BUY", "volume": 0.02, "price_open": 1.08553, "sl": 1.08112,
             "tp": 1.08920, "magic": 260101, "comment": "AlgoDashboardAgent", "time": int(time.time()) - 7200, "contract": 100000},
            {"ticket": 5002, "symbol": "XAUUSD", "side": "SELL", "volume": 0.01, "price_open": 2676.03, "sl": 2689.00,
             "tp": 2657.20, "magic": 180221, "comment": "EA Trend Demo", "time": int(time.time()) - 3600, "contract": 100},
        ]

    def sym(self, name):
        return next(s for s in self.symbols if s["symbol"] == name)

    def step(self):
        for s in self.symbols:
            s["bid"] = max(s["point"], s["bid"] + random.gauss(0, 1) * s["atr"] * s["point"] * 0.05)
            s["rsi"] = min(85, max(15, s["rsi"] + random.gauss(0, 0.6) + (50 - s["rsi"]) * 0.01))
        # optional steady loss to watch the protection trigger
        self.balance -= self.loss_drift
        if not self.blocked and self.baseline - self.equity() >= self.baseline * self.limit_pct / 100:
            self.blocked = True
            print("batas rugi tercapai: entry diblokir")

    def profit(self, p):
        s = self.sym(p["symbol"])
        price = s["bid"] if p["side"] == "BUY" else s["bid"] + s["spread"] * s["point"]
        sign = 1 if p["side"] == "BUY" else -1
        return round(sign * (price - p["price_open"]) * p["volume"] * p["contract"], 2)

    def equity(self):
        return self.balance + sum(self.profit(p) for p in self.positions)

    def entry_state(self):
        return "blocked" if self.blocked else "paused" if self.paused else "allowed"

    def telemetry(self, agent_id):
        eq = self.equity()
        margin = 2223.22
        def signal(s):
            if s["structure"] == "bull" and s["rsi"] >= 55:
                return "BUY"
            if s["structure"] == "bear" and s["rsi"] <= 45:
                return "SELL"
            return "none"
        return {
            "agent_id": agent_id, "agent_version": "0.10-sim", "sent_at": int(time.time()), "server_time": int(time.time()),
            "terminal_connected": True,
            "account": {"login": 884921, "server": "Broker-Demo 01", "company": "Broker Demo", "currency": "USD", "demo": True,
                        "balance": round(self.balance, 2), "equity": round(eq, 2), "margin": margin,
                        "free_margin": round(eq - margin, 2), "margin_level": round(eq / margin * 100, 2)},
            "protection": {"day_start": self.day_start, "baseline": self.baseline, "limit_pct": self.limit_pct,
                           "limit_amount": round(self.baseline * self.limit_pct / 100, 2),
                           "loss_used": round(max(0.0, self.baseline - eq), 2), "entry_state": self.entry_state(),
                           "disabled_magics": sorted(self.disabled)},
            "pl": {"realized": round(self.balance - self.day_balance, 2), "floating": round(eq - self.balance, 2), "closed_deals": 0},
            "fx": {"usd_idr": self.usd_idr},
            "positions": [{k: v for k, v in p.items() if k != "contract"}
                          | {"profit": self.profit(p), "digits": self.sym(p["symbol"])["digits"]} for p in self.positions],
            "symbols": [{"symbol": s["symbol"], "digits": s["digits"], "bid": round(s["bid"], s["digits"]),
                         "ask": round(s["bid"] + s["spread"] * s["point"], s["digits"]), "spread": s["spread"],
                         "rsi": round(s["rsi"], 1), "atr_points": s["atr"], "structure": s["structure"], "signal": signal(s)}
                        for s in self.symbols],
            "timeframe": "H1",
        }

    def apply(self, cmd):
        t = cmd.get("type")
        if t == "pause_entry":
            self.paused = True
            return True, "entry dijeda"
        if t == "resume_entry":
            self.paused = False
            return True, "jeda dicabut, tetapi batas rugi harian masih memblokir entry" if self.blocked else "entry dilanjutkan"
        if t in ("disable_magic", "enable_magic"):
            magic = int(cmd.get("magic") or 0)
            if magic <= 0:
                return False, "magic tidak valid"
            (self.disabled.add if t == "disable_magic" else self.disabled.discard)(magic)
            return True, f"magic {magic} {'dinonaktifkan' if t == 'disable_magic' else 'diaktifkan'}"
        if t == "set_limit":
            v = float(cmd.get("value") or 0)
            if not 0.5 <= v <= 20:
                return False, "batas harus 0.5 sampai 20 persen"
            self.limit_pct = v
            return True, f"batas rugi diubah ke {v:.2f}%"
        return False, f"perintah tidak dikenal: {t}"


def request(base, path, method, agent_id, secret, payload=None):
    body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}\n{method}\n{path}\n{body}".encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(base + path, data=body.encode() if body else None, method=method, headers={
        "Content-Type": "application/json", "X-AlgoDesk-Agent": agent_id,
        "X-AlgoDesk-Timestamp": ts, "X-AlgoDesk-Signature": sig,
    })
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read() or b"{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/api")
    ap.add_argument("--agent", default="vps-01")
    ap.add_argument("--secret", required=True)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--loss-drift", type=float, default=0.0, help="USD rugi per langkah, untuk menguji proteksi")
    ap.add_argument("--usd-idr", type=float, default=0.0, help="kurs USDIDR broker; 0 = broker tidak punya simbol USDIDR")
    args = ap.parse_args()

    sim = Sim(args.loss_drift, args.usd_idr)
    print(f"Agent tiruan {args.agent} mengirim ke {args.url} (Ctrl+C untuk berhenti)")
    while True:
        try:
            sim.step()
            for cmd in request(args.url, "/agent/commands", "GET", args.agent, args.secret).get("commands", []):
                ok, msg = sim.apply(cmd)
                print(f"perintah {cmd['type']} dari {cmd.get('issued_by')}: {msg}")
                request(args.url, "/agent/commands/ack", "POST", args.agent, args.secret,
                        {"id": cmd["id"], "ok": ok, "message": msg, "entry_state": sim.entry_state(), "applied_at": int(time.time())})
            request(args.url, "/agent/telemetry", "POST", args.agent, args.secret, sim.telemetry(args.agent))
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code}: {e.read().decode(errors='replace')}")
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"backend tidak terjangkau: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
