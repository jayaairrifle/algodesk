"""USD/IDR rate from free public APIs (no key), for showing amounts in rupiah.

The first source is Indodax's USDT/IDR market, which trades around the clock: USDT tracks the
US dollar within a fraction of a percent, so it serves as a live USD price. The others are
daily reference rates, used when Indodax cannot be reached. The rate is cached, refreshed in the
background every minute, and the last 24 hours are kept for the dashboard's chart.
"""

import json
import threading
import time
import urllib.request

REFRESH = 60         # seconds between fetches after a success
RETRY = 2 * 60       # seconds before trying again after every source failed
TIMEOUT = 8
HISTORY = 24 * 3600  # seconds of rates kept for the chart

# (name, url, function that pulls the IDR-per-USD number out of the JSON, live market or daily rate)
SOURCES = [
    ("Indodax USDT/IDR", "https://indodax.com/api/ticker/usdtidr", lambda d: d["ticker"]["last"], True),
    ("open.er-api.com", "https://open.er-api.com/v6/latest/USD", lambda d: d["rates"]["IDR"], False),
    ("fawazahmed0/currency-api", "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
     lambda d: d["usd"]["idr"], False),
    ("frankfurter.dev", "https://api.frankfurter.dev/v1/latest?base=USD&symbols=IDR", lambda d: d["rates"]["IDR"], False),
]


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "AlgoDesk/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


class UsdIdr:
    def __init__(self, sources=SOURCES, fetch=_get_json):
        self.sources, self.fetch = sources, fetch
        self.rate: float | None = None
        self.source: str | None = None
        self.updated: float | None = None
        self.live = False
        self.history: list[tuple[float, float]] = []
        self.next_try = 0.0
        self.lock = threading.Lock()
        self.busy = False

    def refresh(self) -> None:
        for name, url, pick, live in self.sources:
            try:
                rate = float(pick(self.fetch(url)))
            except Exception:
                continue
            if 1000 <= rate <= 100000:  # sanity bound; IDR has been ~10k-20k per USD
                with self.lock:
                    self.rate, self.source, self.updated, self.live = rate, name, time.time(), live
                    self.next_try = self.updated + REFRESH
                    self.history.append((self.updated, rate))
                    while self.history and self.history[0][0] < self.updated - HISTORY:
                        self.history.pop(0)
                return
        with self.lock:
            self.next_try = time.time() + RETRY

    def _refresh_in_background(self) -> None:
        try:
            self.refresh()
        finally:
            self.busy = False

    def get(self, history: bool = False) -> dict:
        with self.lock:
            due = time.time() >= self.next_try and not self.busy
            if due:
                self.busy = True
            out = {"usd_idr": self.rate, "source": self.source, "updated": self.updated, "live": self.live}
            if history:
                out["history"] = [[round(t), r] for t, r in self.history]
        if due:
            threading.Thread(target=self._refresh_in_background, daemon=True).start()
        return out
