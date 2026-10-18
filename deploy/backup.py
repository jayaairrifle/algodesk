#!/usr/bin/env python3
"""Consistent SQLite backup (safe while the server is running).

    backup.py <backup_dir> <days_to_keep>
"""

import os
import sqlite3
import sys
import time
from pathlib import Path


def main() -> int:
    target, keep = Path(sys.argv[1]), int(sys.argv[2])
    source = os.environ.get("ALGODESK_DB", "algodesk.db")
    target.mkdir(parents=True, exist_ok=True)
    out = target / f"algodesk-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    cutoff = time.time() - keep * 86400
    for old in target.glob("algodesk-*.db"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
    print(f"backup: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
