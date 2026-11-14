"""SQLite storage. Single connection, WAL, lock around writes."""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  profile_dir TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'needs_login',
  enabled INTEGER NOT NULL DEFAULT 1,
  proxy TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT '',
  ram_mb REAL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  position INTEGER NOT NULL,
  dur_min INTEGER NOT NULL DEFAULT 0,
  dur_max INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'waiting',
  retries INTEGER NOT NULL DEFAULT 0,
  added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER,
  item_id INTEGER,
  started_at REAL,
  finished_at REAL,
  result TEXT,
  message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  account_id INTEGER,
  item_id INTEGER,
  level TEXT NOT NULL DEFAULT 'info',
  message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_item ON jobs(item_id, account_id, result);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
"""

DEFAULTS = {
    "default_duration": 60,
    "scroll_enabled": 1,
    "scroll_speed": "medium",
    "max_workers": 5,
    "mode": "all",            # all = tiap akun putar seluruh antrean, split = antrean dibagi
    "headless": 0,
    "mute": 1,
    "retry_max": 3,
    "channel": "auto",        # auto = system Chrome (codec H.264 ada) -> fallback chromium
    "run_accounts": "",       # json list akun untuk run aktif
}

VALID_STATUS = ("waiting", "running", "done", "failed", "skipped", "partial")


class DB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.lock = threading.RLock()
        self.c = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.executescript(SCHEMA)
        self.c.commit()
        for k, v in DEFAULTS.items():
            self.c.execute("INSERT OR IGNORE INTO settings(k,v) VALUES(?,?)", (k, json.dumps(v)))
        self.c.commit()
        self._writes = 0

    # ---------- settings ----------
    def settings(self):
        with self.lock:
            rows = self.c.execute("SELECT k,v FROM settings").fetchall()
        out = dict(DEFAULTS)
        for r in rows:
            try:
                out[r["k"]] = json.loads(r["v"])
            except Exception:
                out[r["k"]] = r["v"]
        return out

    def set_setting(self, k, v):
        with self.lock:
            self.c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                           (k, json.dumps(v)))
            self.commit()

    # ---------- accounts ----------
    def add_account(self, name, profile_dir):
        with self.lock:
            cur = self.c.execute(
                "INSERT INTO accounts(name,profile_dir,status,enabled,created_at) VALUES(?,?,?,1,?)",
                (name, profile_dir, "needs_login", time.time()))
            self.commit()
            return cur.lastrowid

    def accounts(self):
        with self.lock:
            return [dict(r) for r in self.c.execute("SELECT * FROM accounts ORDER BY id").fetchall()]

    def account(self, aid):
        with self.lock:
            r = self.c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None

    def update_account(self, aid, **kw):
        if not kw:
            return
        cols = ",".join(f"{k}=?" for k in kw)
        with self.lock:
            self.c.execute(f"UPDATE accounts SET {cols} WHERE id=?", (*kw.values(), aid))
            self.commit()

    def del_account(self, aid):
        with self.lock:
            self.c.execute("DELETE FROM accounts WHERE id=?", (aid,))
            self.c.execute("DELETE FROM jobs WHERE account_id=?", (aid,))
            self.commit()

    # ---------- queue ----------
    def add_items(self, urls):
        now = time.time()
        added, skipped = [], []
        with self.lock:
            pos = self.c.execute("SELECT COALESCE(MAX(position),0) FROM queue_items").fetchone()[0]
            existing = {r[0] for r in self.c.execute("SELECT url FROM queue_items").fetchall()}
            for u in urls:
                if u in existing:
                    skipped.append(u)
                    continue
                pos += 1
                cur = self.c.execute(
                    "INSERT INTO queue_items(url,position,added_at) VALUES(?,?,?)", (u, pos, now))
                added.append(cur.lastrowid)
                existing.add(u)
            self.commit()
        return added, skipped

    def queue(self):
        with self.lock:
            return [dict(r) for r in self.c.execute("SELECT * FROM queue_items ORDER BY position").fetchall()]

    def item(self, iid):
        with self.lock:
            r = self.c.execute("SELECT * FROM queue_items WHERE id=?", (iid,)).fetchone()
        return dict(r) if r else None

    def update_item(self, iid, **kw):
        if not kw:
            return
        cols = ",".join(f"{k}=?" for k in kw)
        with self.lock:
            self.c.execute(f"UPDATE queue_items SET {cols} WHERE id=?", (*kw.values(), iid))
            self.commit()

    def del_item(self, iid):
        with self.lock:
            self.c.execute("DELETE FROM queue_items WHERE id=?", (iid,))
            self.c.execute("DELETE FROM jobs WHERE item_id=?", (iid,))
            self.commit()

    def clear_queue(self):
        with self.lock:
            self.c.execute("DELETE FROM queue_items")
            self.c.execute("DELETE FROM jobs")
            self.commit()

    def reorder(self, ids):
        with self.lock:
            for i, iid in enumerate(ids, start=1):
                self.c.execute("UPDATE queue_items SET position=? WHERE id=?", (i, iid))
            self.commit()

    # ---------- jobs ----------
    def add_job(self, aid, iid):
        with self.lock:
            cur = self.c.execute(
                "INSERT INTO jobs(account_id,item_id,started_at,result) VALUES(?,?,?,'running')",
                (aid, iid, time.time()))
            self.commit()
            return cur.lastrowid

    def finish_job(self, jid, result, message=""):
        with self.lock:
            self.c.execute("UPDATE jobs SET finished_at=?, result=?, message=? WHERE id=?",
                           (time.time(), result, message[:2000], jid))
            self.commit()

    def jobs_for_item(self, iid):
        with self.lock:
            return [dict(r) for r in self.c.execute(
                "SELECT * FROM jobs WHERE item_id=? ORDER BY id", (iid,)).fetchall()]

    def running_jobs(self):
        with self.lock:
            return [dict(r) for r in self.c.execute("SELECT * FROM jobs WHERE result='running'").fetchall()]

    def recover(self):
        """Panggil saat boot: job yang masih 'running' dianggap terputus, item dikembalikan."""
        with self.lock:
            rows = self.c.execute("SELECT id,item_id FROM jobs WHERE result='running'").fetchall()
            self.c.execute("UPDATE jobs SET result='interrupted', finished_at=? WHERE result='running'",
                           (time.time(),))
            for r in rows:
                it = self.c.execute("SELECT status FROM queue_items WHERE id=?", (r["item_id"],)).fetchone()
                if it and it["status"] == "running":
                    self.c.execute("UPDATE queue_items SET status='waiting' WHERE id=?", (r["item_id"],))
            self.c.commit()
            return len(rows)

    # ---------- logs ----------
    def log(self, message, account_id=None, item_id=None, level="info"):
        with self.lock:
            self.c.execute("INSERT INTO logs(ts,account_id,item_id,level,message) VALUES(?,?,?,?,?)",
                           (time.time(), account_id, item_id, level, str(message)[:2000]))
            self._writes += 1
            if self._writes % 500 == 0:
                self.c.execute("DELETE FROM logs WHERE id < (SELECT MAX(id)-20000 FROM logs)")
            self.commit()

    def logs(self, account_id=None, level=None, limit=300):
        q = "SELECT * FROM logs WHERE 1=1"
        args = []
        if account_id:
            q += " AND account_id=?"
            args.append(account_id)
        if level:
            q += " AND level=?"
            args.append(level)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            return [dict(r) for r in self.c.execute(q, args).fetchall()]

    def commit(self):
        self.c.commit()
