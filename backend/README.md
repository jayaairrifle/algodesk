# AlgoDesk Backend v0.10

Server untuk dashboard AlgoDesk. Menerima telemetri bertanda tangan dari `AlgoDeskAgent.mq5`, menyimpan perintah dari dashboard sampai diambil agent, dan menyajikan dashboard dengan login serta RBAC.

- Python 3.10+, FastAPI, SQLite (satu file `algodesk.db`, tanpa server database terpisah).
- Dashboard ada di `algodesk/static/index.html` dan disajikan di `/`.
- API agent sesuai kontrak di `algodesk-agent/README.md`.

## Menjalankan

```bash
python -m pip install -r requirements.txt

# 1. buat admin pertama (kata sandi ditanyakan, min. 10 karakter)
python -m algodesk.cli create-user admin --role admin --name "Administrator"

# 2. daftarkan agent; salin dua baris yang dicetak ke input EA
python -m algodesk.cli create-agent vps-01

# 3. jalankan server
python -m uvicorn algodesk.app:create_app --factory --host 127.0.0.1 --port 8000
```

Di Windows PowerShell perintahnya sama; ganti `python` dengan `py` jika perlu.

Lokasi database diatur lewat `ALGODESK_DB` (default `algodesk.db` di folder kerja).

### HTTPS (wajib untuk akun real)

Cookie sesi hanya dikirim lewat HTTPS secara default. Taruh server di belakang reverse proxy dengan sertifikat, misalnya Caddy:

```
algodesk.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Lalu di EA: `InpBackendUrl = https://algodesk.example.com/api`, dan tambahkan `https://algodesk.example.com` di Tools > Options > Expert Advisors > Allow WebRequest.

Untuk mencoba di komputer sendiri tanpa HTTPS, jalankan dengan `ALGODESK_COOKIE_SECURE=0`.

### Mencoba tanpa MT5

`tools/fake_agent.py` meniru agent MQL5 (tanda tangan, telemetri, dan cara menerapkan perintah sama persis):

```bash
python tools/fake_agent.py --url http://127.0.0.1:8000/api --agent vps-01 --secret <rahasia dari create-agent>
# tambahkan --loss-drift 2 untuk melihat proteksi memblokir entry setelah rugi 3%
```

## Peran

| Peran | Lihat dashboard | Jeda/lanjut entry, aktif/nonaktif magic | Ubah batas rugi | Kelola pengguna & nama EA |
|---|---|---|---|---|
| Super Admin (`admin`) | Ya | Ya | Ya | Ya |
| Trader (`trader`) | Ya | Ya | Tidak | Tidak |
| Viewer (`viewer`) | Ya | Tidak | Tidak | Tidak |

## Keamanan yang sudah ada

- Request agent diverifikasi HMAC-SHA256 dengan toleransi jam 30 detik; POST yang diputar ulang ditolak.
- Kata sandi disimpan dengan scrypt; sesi berupa cookie HttpOnly + SameSite=Strict, berlaku 12 jam.
- Login dikunci 5 menit setelah 5 kali gagal per nama pengguna.
- Semua tulis dari browser wajib JSON (menutup celah CSRF lewat form), header CSP dan anti-iframe dipasang.
- Pengguna yang dinonaktifkan, diganti peran, atau diganti sandinya langsung keluar dari semua sesi.
- Perintah yang tidak diambil agent dalam 120 detik ditandai kedaluwarsa, sehingga tombol lama tidak tiba-tiba berlaku saat agent hidup lagi.

Catatan: rahasia agent disimpan apa adanya di database karena server perlu menghitung HMAC. Lindungi file `algodesk.db` dan cadangkan secara berkala. Ganti rahasia dengan `python -m algodesk.cli rotate-agent vps-01`.

## Tes

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

## Belum termasuk

- Statistik performa per strategi (win rate, profit factor). Butuh agent mengirim riwayat deal; menu Performa saat ini menampilkan kurva equity 7 hari.
- Notifikasi (Telegram/email) saat batas rugi tercapai atau agent offline.
