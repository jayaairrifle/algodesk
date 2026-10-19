"""USD/IDR rate from free public APIs (no key), for showing amounts in rupiah.

The broker's USDIDR symbol stays the first choice in the dashboard because it is live; these
sources update about once a day, which is enough for display. The rate is cached and refreshed
in the background, so a slow or unreachable API never delays a request.
"""

import json
import threading
import time
import urllib.request

REFRESH = 15 * 60    # seconds between fetches after a success
RETRY = 2 * 60       # seconds before trying again after every source failed
TIMEOUT = 8

# (name, url, function that pulls the IDR-per-USD number out of the JSON)
SOURCES = [
    ("open.er-api.com", "https://open.er-api.com/v6/latest/USD", lambda d: d["rates"]["IDR"]),
    ("fawazahmed0/currency-api", "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
     lambda d: d["usd"]["idr"]),
    ("frankfurter.dev", "https://api.frankfurter.dev/v1/latest?base=USD&symbols=IDR", lambda d: d["rates"]["IDR"]),
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
        self.next_try = 0.0
        self.lock = threading.Lock()
        self.busy = False

    def refresh(self) -> None:
        for name, url, pick in self.sources:
            try:
                rate = float(pick(self.fetch(url)))
            except Exception:
                continue
            if 1000 <= rate <= 100000:  # sanity bound; IDR has been ~10k-20k per USD
                with self.lock:
                    self.rate, self.source, self.updated = rate, name, time.time()
                    self.next_try = self.updated + REFRESH
                return
        with self.lock:
            self.next_try = time.time() + RETRY

    def _refresh_in_background(self) -> None:
        try:
            self.refresh()
        finally:
            self.busy = False

    def get(self) -> dict:
        with self.lock:
            due = time.time() >= self.next_try and not self.busy
            if due:
                self.busy = True
            out = {"usd_idr": self.rate, "source": self.source, "updated": self.updated}
        if due:
            threading.Thread(target=self._refresh_in_background, daemon=True).start()
        return out
