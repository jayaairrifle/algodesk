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
        for s in self.symbols:
            s["open"] = s["bid"] * (1 + random.gauss(0, 0.003))
        self.sr = {"enabled": False, "symbol": "XAUUSD", "lots": 0.01, "dist": 0.6, "step": 0.05}
        self.balance = 10246.38
        self.baseline = 10246.38
        self.day_balance = self.balance
        self.day_start = int(time.time()) // 86400 * 86400
        self.limit_pct = 3.0
        self.paused = False
        self.blocked = False
        self.disabled: set[int] = set()
        self.loss_drift = loss_drift
        self.ticket = 900000
        self.deals: list[dict] = []
        self.sync_from = 0
        # 30 days of closed trades for four simulated EAs: (magic, symbol, trades per day, edge)
        self.bots = [(2001, "XAUUSD", 3, 4.0), (2002, "XAUUSD", 2, 1.5), (1001, "EURUSD", 1, -0.5), (2003, "USDJPY", 2, 2.0)]
        now = int(time.time())
        for day in range(30, -1, -1):
            for magic, sym, per_day, edge in self.bots:
                for _ in range(random.randint(0, per_day)):
                    closed = self.day_start - day * 86400 + random.randint(600, 86000)
                    if closed < now - 60:
                        self.add_trade(magic, sym, closed, round(random.gauss(edge, 12), 2))
        self.positions = [
            {"ticket": 5001, "symbol": "EURUSD", "side": "BUY", "volume": 0.02, "price_open": 1.08553, "sl": 1.08112,
             "tp": 1.08920, "magic": 2003, "comment": "Bounce Area", "time": int(time.time()) - 7200, "contract": 100000},
            {"ticket": 5002, "symbol": "XAUUSD", "side": "SELL", "volume": 0.01, "price_open": 2676.03, "sl": 2689.00,
             "tp": 2657.20, "magic": 2001, "comment": "SNR M15", "time": int(time.time()) - 3600, "contract": 100},
        ]

    def add_trade(self, magic, symbol, closed, net):
        s = self.sym(symbol)
        side = random.choice(("BUY", "SELL"))
        opened = closed - random.randint(300, 7200)
        price = round(s["bid"] * (1 + random.gauss(0, 0.002)), s["digits"])
        pos = self.ticket = self.ticket + 2
        common = {"position": pos, "symbol": symbol, "volume": 0.1, "swap": 0, "fee": 0, "magic": magic, "comment": ""}
        self.deals.append(common | {"ticket": pos, "time": opened, "type": side, "entry": "IN", "price": price,
                                    "profit": 0, "commission": -0.3})
        self.deals.append(common | {"ticket": pos + 1, "time": closed, "type": "SELL" if side == "BUY" else "BUY",
                                    "entry": "OUT", "price": round(price * (1 + (net / 4000 if side == "BUY" else -net / 4000)), s["digits"]), "profit": round(net + 0.3, 2), "commission": 0})

    def realized_today(self):
        return round(sum(d["profit"] + d["commission"] for d in self.deals if d["time"] >= self.day_start), 2)

    TFS = (("M1", .1), ("M5", .25), ("M15", .45), ("M30", .7), ("H1", 1), ("H4", 2), ("D1", 4.5), ("W1", 11), ("MN1", 24))

    def timeframes(self, s, signal):
        """Indicators per timeframe, as agent 0.14 sends them: H1 is the symbol's own values,
        the others drift from it by a fixed per-symbol, per-timeframe offset."""
        out = {}
        for i, (name, atr_mult) in enumerate(self.TFS):
            rnd = random.Random(f"{s['symbol']}{name}")
            v = dict(s, rsi=s["rsi"] if name == "H1" else min(90, max(10, s["rsi"] + rnd.uniform(-18, 18))),
                     structure=s["structure"] if name == "H1" else rnd.choice(("bull", "bear", "mixed")))
            out[name] = {"rsi": round(v["rsi"], 1), "atr_points": round(s["atr"] * atr_mult, 1),
                         "structure": v["structure"], "signal": signal(v)}
        return out

    def sym(self, name):
        return next(s for s in self.symbols if s["symbol"] == name)

    def step(self):
        for s in self.symbols:
            s["bid"] = max(s["point"], s["bid"] + random.gauss(0, 1) * s["atr"] * s["point"] * 0.05)
            s["rsi"] = min(85, max(15, s["rsi"] + random.gauss(0, 0.6) + (50 - s["rsi"]) * 0.01))
        # now and then an EA that is allowed to trade closes a trade
        if random.random() < 0.02 and not self.blocked and not self.paused:
            magic, sym, _, edge = random.choice(self.bots)
            if magic not in self.disabled:
                net = round(random.gauss(edge, 12), 2)
                self.add_trade(magic, sym, int(time.time()), net)
                self.balance += net
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
                        "free_margin": round(eq - margin, 2), "margin_level": round(eq / margin * 100, 2),
                        "margin_call": 100.0, "stop_out": 50.0, "leverage": 500},
            "protection": {"day_start": self.day_start, "baseline": self.baseline, "limit_pct": self.limit_pct,
                           "limit_amount": round(self.baseline * self.limit_pct / 100, 2),
                           "loss_used": round(max(0.0, self.baseline - eq), 2), "entry_state": self.entry_state(),
                           "disabled_magics": sorted(self.disabled)},
            "pl": {"realized": self.realized_today(), "floating": round(eq - self.balance, 2), "closed_deals": 0},
            "fx": {"usd_idr": self.usd_idr},
            "terminal": {"build": 4755, "memory_mb": 346, "memory_total_mb": 8192, "cpu_cores": 4, "ping_ms": 38.2, "trade_allowed": True},
            "positions": [{k: v for k, v in p.items() if k != "contract"}
                          | {"profit": self.profit(p), "digits": self.sym(p["symbol"])["digits"]} for p in self.positions],
            "symbols": [{"symbol": s["symbol"], "digits": s["digits"], "bid": round(s["bid"], s["digits"]),
                         "ask": round(s["bid"] + s["spread"] * s["point"], s["digits"]), "spread": s["spread"], "day_open": round(s["open"], s["digits"]),
                         "rsi": round(s["rsi"], 1), "atr_points": s["atr"], "structure": s["structure"], "signal": signal(s),
                         "tf": self.timeframes(s, signal)}
                        for s in self.symbols],
            "timeframe": "H1",
            "strategies": [self.strategy()],
        }

    def pending_deals(self):
        """Deals from the sync cursor on, 300 at a time, like the MQL5 agent."""
        batch = sorted((d for d in self.deals if d["time"] >= self.sync_from), key=lambda d: (d["time"], d["ticket"]))[:300]
        return batch

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
        if t == "sr_set":
            if cmd.get("symbol") and not any(x["symbol"] == cmd["symbol"] for x in self.symbols):
                return False, f"simbol {cmd['symbol']} tidak ada di broker ini"
            self.sr |= {k: cmd[k] for k in ("enabled", "symbol", "lots", "dist", "step") if k in cmd}
            sr = self.sr
            return True, f"StopReverse {'ON' if sr['enabled'] else 'OFF'}, {sr['symbol']} lot {sr['lots']:.2f} D {sr['dist']:.2f} S {sr['step']:.2f}"
        if t == "sr_close":
            self.sr["enabled"] = False
            return True, "StopReverse dimatikan, posisi dan stop order ditutup"
        return False, f"perintah tidak dikenal: {t}"

    def strategy(self):
        """The StopReverse block of agent 0.15 telemetry."""
        sr, on = self.sr, self.sr["enabled"]
        block = "" if not on else ("entry dijeda dari dashboard" if self.paused else "batas rugi harian tercapai" if self.blocked else "")
        bid = self.sym(sr["symbol"])["bid"]
        status = "OFF" if not on else f"BUY {sr['lots']:.2f} @ {bid - 0.4:.2f} (+0.40) | SELL STOP {bid - sr['dist']:.2f} = " + ("BALIK ARAH" if not block else f"KELUAR saja ({block})")
        return {"id": "stop_reverse", "name": "XAU StopReverse", "magic": 777001, "hedging": True,
                "positions": 1 if on else 0, "orders": 1 if on else 0, "blocked": block, "status": status} | sr


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
    last_sync = 0.0
    while True:
        try:
            sim.step()
            for cmd in request(args.url, "/agent/commands", "GET", args.agent, args.secret).get("commands", []):
                ok, msg = sim.apply(cmd)
                print(f"perintah {cmd['type']} dari {cmd.get('issued_by')}: {msg}")
                request(args.url, "/agent/commands/ack", "POST", args.agent, args.secret,
                        {"id": cmd["id"], "ok": ok, "message": msg, "entry_state": sim.entry_state(), "applied_at": int(time.time())})
            request(args.url, "/agent/telemetry", "POST", args.agent, args.secret, sim.telemetry(args.agent))
            if time.time() - last_sync >= 15:
                last_sync = time.time()
                batch = sim.pending_deals()
                if batch:
                    request(args.url, "/agent/deals", "POST", args.agent, args.secret, {"login": 884921, "deals": batch})
                    sim.sync_from = batch[-1]["time"]
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code}: {e.read().decode(errors='replace')}")
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"backend tidak terjangkau: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
