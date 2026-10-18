# AlgoDesk

Dashboard pemantauan dan proteksi risiko untuk EA MetaTrader 5.

| Folder | Isi |
|---|---|
| `agent/` | `AlgoDeskAgent.mq5` (proteksi rugi harian, telemetri, kanal perintah) dan `AlgoDeskGuard.mqh` (cek izin entry untuk setiap EA) |
| `backend/` | Server FastAPI + SQLite, login dan RBAC, dashboard web, agent tiruan untuk uji coba |
| `prototype/` | Prototipe HTML awal dengan data simulasi |

Mulai dari `backend/README.md` untuk menjalankan server, lalu `agent/README.md` untuk memasang agent di terminal MT5.

## Pasang di VPS Linux (Ubuntu/Debian)

```bash
git clone https://github.com/jayaairrifle/algodesk.git
cd algodesk
sudo bash deploy/install.sh                      # HTTPS otomatis di <ip-vps>.sslip.io
# atau dengan domain sendiri (arahkan DNS A ke IP VPS dulu):
sudo bash deploy/install.sh --domain algodesk.domainanda.com --email anda@email.com
```

Skrip memasang Python, Caddy (sertifikat HTTPS otomatis), layanan systemd `algodesk`, dan backup database harian. Di akhir, skrip meminta kata sandi Super Admin dan mencetak `InpAgentId` serta `InpAgentSecret` untuk AlgoDeskAgent. Port 80 dan 443 harus terbuka.

Update ke versi terbaru: `sudo bash deploy/update.sh` dari folder repo. Database dan pengaturan tetap aman.
