"""Regresi: DB dari versi lama (tanpa kolom jobs.round) harus otomatis dimigrasi.

Bug asli di layar user: 'no such column: round' -> worker mati -> jendela Chrome
menutup sendiri sebelum membuka link. Penyebab: CREATE TABLE IF NOT EXISTS tidak
menambah kolom ke tabel yang sudah ada.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="faw-mig-"))
os.environ["FAW_DATA_DIR"] = str(TMP / "data")

FAIL = []


def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


SCHEMA_LAMA = """
CREATE TABLE settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE accounts(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  profile_dir TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'needs_login',
  enabled INTEGER NOT NULL DEFAULT 1, proxy TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT '', ram_mb REAL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE queue_items(id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
  position INTEGER NOT NULL, dur_min INTEGER NOT NULL DEFAULT 0,
  dur_max INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'waiting',
  retries INTEGER NOT NULL DEFAULT 0, added_at REAL NOT NULL);
CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER,
  item_id INTEGER, started_at REAL, finished_at REAL, result TEXT,
  message TEXT NOT NULL DEFAULT '');
CREATE TABLE logs(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
  account_id INTEGER, item_id INTEGER, level TEXT NOT NULL DEFAULT 'info',
  message TEXT NOT NULL);
"""


def main():
    dbp = TMP / "lama.db"
    raw = sqlite3.connect(str(dbp))
    raw.executescript(SCHEMA_LAMA)
    raw.execute("INSERT INTO accounts(name,created_at) VALUES('lama',1)")
    raw.execute("INSERT INTO queue_items(url,position,added_at) VALUES('https://x/1',1,1)")
    raw.execute("INSERT INTO jobs(account_id,item_id,started_at,result) VALUES(1,1,1,'done')")
    raw.commit()
    cols_before = {r[1] for r in raw.execute("PRAGMA table_info(jobs)").fetchall()}
    raw.close()
    check("DB lama memang belum punya kolom round", "round" not in cols_before,
          str(sorted(cols_before)))

    from app.db import DB
    db = DB(dbp)
    cols_after = {r["name"] for r in db.c.execute("PRAGMA table_info(jobs)").fetchall()}
    check("kolom round ditambahkan otomatis", "round" in cols_after, str(sorted(cols_after)))

    row = db.c.execute("SELECT round FROM jobs").fetchone()
    check("job lama dapat default round=1", row["round"] == 1, str(dict(row) if row else None))
    check("akun lama tetap ada", len(db.accounts()) == 1, str(len(db.accounts())))
    check("antrean lama tetap ada", len(db.queue()) == 1, str(len(db.queue())))

    # jalur kode yang dulu meledak
    wrote = db.add_job(1, 1, 2)
    db.finish_job(wrote, "done", "ok")
    check("add_job dengan round bisa dipakai", wrote > 0, str(wrote))
    got = db.jobs_round(2, 1)
    check("jobs_round mengembalikan job putaran 2", len(got) == 1, str(len(got)))
    check("max_round membaca 2", db.max_round() == 2, str(db.max_round()))

    # migrasi idempoten: buka dua kali tidak error
    db2 = DB(dbp)
    check("membuka DB yang sudah dimigrasi tidak error", "round" in {
        r["name"] for r in db2.c.execute("PRAGMA table_info(jobs)").fetchall()})

    # DB baru dari nol juga harus punya round
    db3 = DB(TMP / "baru.db")
    check("DB baru punya kolom round",
          "round" in {r["name"] for r in db3.c.execute("PRAGMA table_info(jobs)").fetchall()})

    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + ("=" * 45))
    if FAIL:
        print(f"GAGAL {len(FAIL)}: " + ", ".join(FAIL))
        return 1
    print("TES MIGRASI LULUS")
    return 0


if __name__ == "__main__":
    sys.exit(main())