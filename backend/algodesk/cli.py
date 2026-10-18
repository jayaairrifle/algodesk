"""Admin tasks that must not be reachable over HTTP.

    python -m algodesk.cli create-user admin --role admin --name "Administrator"
    python -m algodesk.cli create-agent vps-01
    python -m algodesk.cli rotate-agent vps-01
"""

import argparse
import getpass
import os
import sys
import time

from .db import DB
from .security import hash_password, new_agent_secret


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="algodesk")
    parser.add_argument("--db", default=os.environ.get("ALGODESK_DB", "algodesk.db"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("create-user", help="Buat pengguna dashboard")
    u.add_argument("username")
    u.add_argument("--role", choices=["admin", "trader", "viewer"], default="viewer")
    u.add_argument("--name", help="Nama tampilan")
    u.add_argument("--password", help="Kata sandi (jika kosong akan ditanyakan)")

    a = sub.add_parser("create-agent", help="Daftarkan agent MT5 dan cetak rahasianya")
    a.add_argument("agent_id")

    r = sub.add_parser("rotate-agent", help="Buat rahasia baru untuk agent")
    r.add_argument("agent_id")

    args = parser.parse_args(argv)
    db = DB(args.db)

    if args.cmd == "create-user":
        password = args.password or getpass.getpass("Kata sandi (min. 10 karakter): ")
        if len(password) < 10:
            print("Kata sandi minimal 10 karakter.", file=sys.stderr)
            return 1
        if db.one("SELECT 1 FROM users WHERE username = ?", (args.username,)):
            print(f"Pengguna {args.username} sudah ada.", file=sys.stderr)
            return 1
        db.run(
            "INSERT INTO users (username, display_name, role, pw_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (args.username, args.name or args.username, args.role, hash_password(password), time.time()),
        )
        print(f"Pengguna {args.username} ({args.role}) dibuat.")
        return 0

    if args.cmd in ("create-agent", "rotate-agent"):
        exists = db.one("SELECT 1 FROM agents WHERE agent_id = ?", (args.agent_id,))
        if args.cmd == "create-agent" and exists:
            print(f"Agent {args.agent_id} sudah ada. Pakai rotate-agent untuk rahasia baru.", file=sys.stderr)
            return 1
        if args.cmd == "rotate-agent" and not exists:
            print(f"Agent {args.agent_id} belum terdaftar.", file=sys.stderr)
            return 1
        secret = new_agent_secret()
        if exists:
            db.run("UPDATE agents SET secret = ? WHERE agent_id = ?", (secret, args.agent_id))
        else:
            db.run("INSERT INTO agents (agent_id, secret, created_at) VALUES (?, ?, ?)", (args.agent_id, secret, time.time()))
        print(f"InpAgentId     = {args.agent_id}")
        print(f"InpAgentSecret = {secret}")
        print("Simpan rahasia ini sekarang; tidak akan ditampilkan lagi.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
