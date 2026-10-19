"""Per-EA trading statistics built from the deal history the agent syncs.

A trade is one MT5 position: every deal with the same position id. Its result is the
sum of profit, swap, commission and fee of those deals (the entry commission counts
too). It belongs to the period in which its last closing deal happened, and to the
magic number of its opening deal (closing deals from SL/TP or manual closes can carry
a different magic).
"""

from collections import defaultdict

TRADE_TYPES = ("BUY", "SELL")
CLOSING = ("OUT", "INOUT", "OUT_BY")


def _net(d) -> float:
    return d["profit"] + d["swap"] + d["commission"] + d["fee"]


def closed_trades(deals, start: int, end: int | None = None) -> list[dict]:
    """Positions whose last closing deal falls in [start, end), oldest first."""
    by_pos: dict[int, list] = defaultdict(list)
    for d in deals:
        if d["type"] in TRADE_TYPES and d["position_id"]:
            by_pos[d["position_id"]].append(d)
    trades = []
    for pos, ds in by_pos.items():
        ds.sort(key=lambda d: (d["time"], d["ticket"]))
        outs = [d for d in ds if d["entry"] in CLOSING]
        if not outs:
            continue
        closed = outs[-1]["time"]
        if closed < start or (end is not None and closed >= end):
            continue
        opening = next((d for d in ds if d["entry"] == "IN"), None)
        first = opening or ds[0]
        trades.append({
            "position_id": pos,
            "magic": first["magic"],
            "symbol": first["symbol"],
            # the opening deal's direction is the trade's side; without it, a SELL close means a BUY trade
            "side": first["type"] if opening else ("BUY" if first["type"] == "SELL" else "SELL"),
            "volume": round(sum(d["volume"] for d in outs), 2),
            "open_time": opening["time"] if opening else None,
            "open_price": opening["price"] if opening else None,
            "close_time": closed,
            "close_price": outs[-1]["price"],
            "profit": round(sum(d["profit"] for d in ds), 2),
            "commission": round(sum(d["commission"] + d["fee"] for d in ds), 2),
            "swap": round(sum(d["swap"] for d in ds), 2),
            "net": round(sum(_net(d) for d in ds), 2),
        })
    trades.sort(key=lambda t: (t["close_time"], t["position_id"]))
    return trades


def ea_stats(trades: list[dict], balance: float | None = None) -> list[dict]:
    """Summary per magic number for the given closed trades."""
    groups: dict[int, list] = defaultdict(list)
    for t in trades:
        groups[t["magic"]].append(t)
    out = []
    for magic, ts in sorted(groups.items()):
        wins = [t["net"] for t in ts if t["net"] > 0]
        losses = [t["net"] for t in ts if t["net"] < 0]
        gross_win, gross_loss = sum(wins), -sum(losses)
        cum = peak = max_dd = 0.0
        curve = []
        for t in ts:
            cum += t["net"]
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
            curve.append([t["close_time"], round(cum, 2)])
        symbols: dict[str, int] = defaultdict(int)
        for t in ts:
            symbols[t["symbol"]] += 1
        out.append({
            "magic": magic,
            "trades": len(ts),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(ts) * 100, 1),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "net": round(cum, 2),
            "gross_profit": round(gross_win, 2),
            "gross_loss": round(gross_loss, 2),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd / balance * 100, 2) if balance else None,
            "last_trade": ts[-1]["close_time"],
            "symbols": dict(sorted(symbols.items(), key=lambda kv: -kv[1])),
            "curve": curve[-60:],
        })
    return out
