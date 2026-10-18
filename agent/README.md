# AlgoDesk MT5 Agent v0.10

Agent sisi terminal untuk dashboard AlgoDesk. Isinya dua file:

| File | Lokasi di folder data MT5 | Fungsi |
|---|---|---|
| `MQL5/Experts/AlgoDesk/AlgoDeskAgent.mq5` | `MQL5/Experts/AlgoDesk/` | Proteksi rugi harian, penegakan, telemetri, kanal perintah |
| `MQL5/Include/AlgoDesk/AlgoDeskGuard.mqh` | `MQL5/Include/AlgoDesk/` | Cek izin entry untuk setiap EA (VE5, EA Trend, dll.) |

Status: kode belum dikompilasi di MetaEditor. Kompilasi dulu dan laporkan error jika ada.

## Cara kerja

1. **Baseline harian.** Saat hari server baru dimulai (jam `InpDayStartHour`), agent menyimpan equity saat itu sebagai baseline. Jika agent baru dipasang di tengah hari, baseline = equity saat agent mulai.
2. **Batas rugi.** Jika `baseline − equity ≥ baseline × batas%`, agent menyetel status `BLOCK`. Blokir bertahan sampai reset harian, walau equity pulih.
3. **Tanpa internet tetap aman.** Semua status disimpan di Global Variables terminal (`AD_<login>_BASE`, `_DAY`, `_LIMIT_PCT`, `_BLOCK`, `_PAUSE`, `_M<magic>_OFF`, `_HB`). EA membaca langsung lewat guard, jadi backend mati tidak membuka celah.
4. **Penegakan untuk EA tanpa guard.** Dengan `InpCloseBlockedEntry = true`, posisi baru dari magic yang diawasi langsung ditutup jika dibuka saat entry diblokir/dijeda. Trade manual (magic 0) tidak disentuh kecuali `InpIncludeManual = true`.
5. **Posisi terbuka** tetap mengikuti SL/TP masing-masing EA. `InpCloseAllOnLimit` (default mati) menutup semuanya saat batas tercapai.

## Memasang guard di EA (contoh VE5)

```mql5
#include <AlgoDesk/AlgoDeskGuard.mqh>

// tepat sebelum membuka posisi baru
ENUM_AD_ENTRY r = AlgoDesk_Check(InpMagicNumber);
if(r != AD_ENTRY_OK)
  {
   PrintFormat("VE5: entry dilewati, %s", AlgoDesk_ReasonText(r));
   return;
  }
trade.Buy(lots, _Symbol, ask, sl, tp);
```

Tambahkan `#define ALGODESK_FAIL_CLOSED` sebelum `#include` jika EA harus berhenti entry ketika agent belum menetapkan baseline hari ini.

## Pemasangan

1. Salin folder `MQL5` ke folder data terminal (File > Open Data Folder), lalu kompilasi `AlgoDeskAgent.mq5`.
2. Pasang agent di **satu** chart saja per akun.
3. Mode lokal: biarkan `InpBackendUrl` kosong. Proteksi tetap jalan, tanpa telemetri.
4. Dengan backend: isi `InpBackendUrl` (mis. `https://algodesk.example.com/api`), `InpAgentId`, dan `InpAgentSecret` (≥16 karakter). Tambahkan domain backend di Tools > Options > Expert Advisors > Allow WebRequest.

Catatan Strategy Tester: Global Variables di tester terpisah dari terminal live, jadi uji proteksi di akun demo.

## Kontrak API backend

Semua request membawa header:

```
X-AlgoDesk-Agent: <agent_id>
X-AlgoDesk-Timestamp: <unix detik UTC>
X-AlgoDesk-Signature: hex(HMAC_SHA256(secret, "<timestamp>\n<METHOD>\n<path>\n<body>"))
```

`path` tanpa base URL, mis. `/agent/telemetry`. Body GET kosong. Backend sebaiknya menolak timestamp yang selisihnya lebih dari 30 detik.

Verifikasi di backend (Python):

```python
import hmac, hashlib, time
def verify(secret, ts, method, path, body, sig):
    if abs(time.time() - int(ts)) > 30: return False
    msg = f"{ts}\n{method}\n{path}\n{body}".encode()
    return hmac.compare_digest(hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest(), sig)
```

### `POST /agent/telemetry`

Dikirim tiap `InpTelemetrySec` detik.

```json
{
  "agent_id": "vps-01", "agent_version": "0.10", "sent_at": 1792027095, "server_time": 1792052295,
  "terminal_connected": true,
  "account": { "login": 884921, "server": "Broker-Demo 01", "company": "...", "currency": "USD", "demo": true,
               "balance": 10246.38, "equity": 10203.39, "margin": 2223.22, "free_margin": 7980.17, "margin_level": 458.94 },
  "protection": { "day_start": 1792022400, "baseline": 10246.38, "limit_pct": 3.00, "limit_amount": 307.39,
                  "loss_used": 42.99, "entry_state": "allowed", "disabled_magics": [] },
  "positions": [ { "ticket": 123, "symbol": "EURUSD", "digits": 5, "side": "BUY", "volume": 0.02, "price_open": 1.08553,
                   "sl": 1.08112, "tp": 1.08920, "profit": -18.20, "magic": 260101, "comment": "", "time": 1792039000 } ],
  "symbols": [ { "symbol": "EURUSD", "digits": 5, "bid": 1.08462, "ask": 1.08471, "spread": 9, "rsi": 56.8,
                 "atr_points": 632.0, "structure": "bull", "signal": "BUY" } ],
  "timeframe": "H1"
}
```

`entry_state`: `allowed`, `paused`, atau `blocked`. `atr_points` dalam point simbol (broker 5 digit: 10 point = 1 pip).

### `GET /agent/commands`

Dicek tiap `InpCommandPollSec` detik. Backend hanya mengembalikan perintah yang belum di-ack.

```json
{ "commands": [ { "id": "cmd_01", "type": "pause_entry", "issued_by": "Administrator" } ] }
```

| `type` | Field tambahan | Efek |
|---|---|---|
| `pause_entry` | | Jeda semua entry baru |
| `resume_entry` | | Cabut jeda (blokir batas rugi tetap berlaku) |
| `disable_magic` / `enable_magic` | `magic` | Matikan/hidupkan entry satu magic number |
| `set_limit` | `value` (0.5–20) | Ubah batas rugi %; menaikkan batas tidak mencabut blokir hari ini |

Backend wajib memeriksa role (RBAC) sebelum menyimpan perintah. Agent tidak mengenal pengguna.

### `POST /agent/commands/ack`

```json
{ "id": "cmd_01", "ok": true, "message": "entry dijeda", "entry_state": "paused", "applied_at": 1792027100 }
```

Dashboard menampilkan "Menunggu konfirmasi agent" sampai ack ini diterima.

## Batasan yang perlu diketahui

- `LIMIT_PCT` diambil dari input saat agent start; perubahan via `set_limit` hilang jika agent di-restart. Ubah input juga bila ingin permanen.
- Penutupan otomatis entry terblokir terjadi setelah order tereksekusi, jadi ada spread/komisi kecil. Guard di dalam EA mencegahnya sejak awal.
- `WebRequest` bersifat sinkron; backend lambat (lebih dari `InpHttpTimeoutMs`) akan memperlambat timer agent, tetapi tidak menghentikan proteksi.
