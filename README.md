# AlgoDesk

Dashboard pemantauan dan proteksi risiko untuk EA MetaTrader 5.

| Folder | Isi |
|---|---|
| `agent/` | `AlgoDeskAgent.mq5` (proteksi rugi harian, telemetri, kanal perintah) dan `AlgoDeskGuard.mqh` (cek izin entry untuk setiap EA) |
| `backend/` | Server FastAPI + SQLite, login dan RBAC, dashboard web, agent tiruan untuk uji coba |
| `prototype/` | Prototipe HTML awal dengan data simulasi |

Mulai dari `backend/README.md` untuk menjalankan server, lalu `agent/README.md` untuk memasang agent di terminal MT5.
