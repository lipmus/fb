"""Satu browser terotomasi = satu worker akun.

Keep-alive: worker hidup terus selama akun enabled, ambil item dari queue (claim/release).
Playwright disinkron di thread sendiri (Windows: tidak boleh lewat asyncio.
Juga sengaja system Chrome, bukan Chromium bundled - codec H.264/MP4 tidak ada
di build Chromium playwright, jadi video FB = layar hitam.
"""
import json
import random
import re
import threading
import time

from playwright.sync_api import sync_playwright

from .crypto import load as sess_load, save as sess_save
from .paths import data_dir, session_path

# selector ini yang paling gampang berubah kalau FB update struktur. tambah di sini saja.
COOKIE_DISMISS = [
    "div[aria-label='Allow all cookies']",
    "div[aria-label='Tolak cookie opsional']",
    "div[aria-label='Decline optional cookies']",
]
LOGIN_SELECTOR = "input[name='email'], input[name='pass']"
CHECKPOINT_HINTS = [
    "checkpoint",
    "two_factor",
    "login/device-based",
    "/recover",
    "consent",
    "/privacy/consent",
    "confirmemail",
    "challenge",
]
VIDEO_SELECTORS = ["video[src]", "video"]
BAD_PAGE_HINTS = [
    "content isn't available",
    "konten ini tidak tersedia",
    "video isn't available",
    "halaman tidak ditemukan",
    "page not found",
    "not available right now",
    "sudah tidak tersedia",
]


def _rand_scrolls(step=700, pause_ms=1500):
    yield random.randint(0, 160)
    while True:
        yield step + random.randint(-120, 220)


def solve_duration(item, default):
    lo = item.get("dur_min") or 0
    hi = item.get("dur_max") or 0
    if lo and hi:
        return max(1, random.randint(min(lo, hi), max(lo, hi)))
    if lo or hi:
        return max(1, lo or hi)
    return max(1, int(default))


def build_context(browser, acc, settings, storage_state=None):
    kw = dict(
        viewport={"width": 1280, "height": 800},
        locale="id-ID",
        timezone_id="Asia/Jakarta",
        user_agent=None,
    )
    if acc.get("proxy"):
        kw["proxy"] = {"server": acc["proxy"]}
    if settings.get("headless"):
        kw["viewport"] = {"width": 1280, "height": 800}
    if storage_state:
        kw["storage_state"] = storage_state
    return browser.new_context(**kw)


def launch_browser(pw, settings):
    """Luncurkan Chrome untuk satu worker.

    HARUS dipanggil dari dalam thread worker: objek Playwright terkunci ke thread
    pembuatnya (greenlet), jadi instance tidak bisa dipakai lintas thread.
    """
    args = ["--disable-blink-features=AutomationControlled", "--no-first-run",
            "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required"]
    if settings.get("mute"):
        args.append("--mute-audio")
    headless = bool(settings.get("headless"))
    if settings.get("channel", "auto") in ("auto", "chrome"):
        try:
            return pw.chromium.launch(channel="chrome", headless=headless, args=args), "chrome"
        except Exception:
            pass
    return pw.chromium.launch(headless=headless, args=args), "chromium"


def close_context(ctx):
    """Tutup context tanpa menyimpan storage_state."""
    try:
        ctx.close()
    except Exception:
        pass


def launch(browser, acc, settings, storage_state=None):
    """Buat context terisolasi untuk akun. Cookie disimpan di storage_state terenkripsi
    (dipulihkan oleh pemanggil), bukan di direktori profil Chromium - karena system Chrome
    dipakai bareng profil default user."""
    kw = dict(viewport={"width": 1280, "height": 800}, locale="id-ID",
              timezone_id="Asia/Jakarta")
    if acc.get("proxy"):
        kw["proxy"] = {"server": acc["proxy"]}
    if storage_state:
        kw["storage_state"] = storage_state
    try:
        return browser.new_context(**kw)
    except Exception as e:
        raise RuntimeError(f"gagal buat browser context: {e}") from e


def dismiss_cookie_wall(page):
    for sel in COOKIE_DISMISS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=2000)
                page.wait_for_timeout(600)
                return True
        except Exception:
            pass
    return False


def _looks_logged_in(page):
    """True kalau URL bukan login/checkpoint; tak ada DOM cek karena FB mudah berubah."""
    url = (page.url or "").lower()
    if any(h in url for h in CHECKPOINT_HINTS):
        return False
    if any(k in url for k in ("/login", "/r.php", "login.php")):
        return False
    return True


def check_session(page, db, aid):
    page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    dismiss_cookie_wall(page)
    ok = _looks_logged_in(page)
    db.update_account(aid, status="active" if ok else "needs_login",
                      last_error="" if ok else "Sesi Facebook tidak aktif / butuh login")
    return ok


def verify_and_wait_login(page, db, aid, timeout_s=600):
    """Dipanggil dari proses CLI login: buka FB, tunggu user login manual sampai status aktif."""
    db.update_account(aid, status="needs_login", last_error="")
    page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
    end = time.time() + timeout_s
    last_status = None
    while time.time() < end:
        page.wait_for_timeout(2000)
        ok = _looks_logged_in(page)
        if ok:
            db.update_account(aid, status="active", last_error="")
            return True
        if last_status != page.url:
            last_status = page.url
    db.update_account(aid, status="needs_login", last_error="timeout menunggu login manual")
    return False


class Worker(threading.Thread):
    def __init__(self, orch, acc, settings):
        super().__init__(daemon=True, name=f"worker-{acc['id']}")
        self.orch = orch
        self.db = orch.db
        self.acc = acc
        self.settings = settings
        self.aid = acc["id"]
        self.cmd = None
        self.cmd_event = threading.Event()
        self.busy = False
        self.item_id = None
        self.remaining = 0.0
        self.paused = False
        self.done = False
        self.hard_error = ""
        self._last_check = 0.0

    # ---- control dari UI/orchestrator ----
    def send(self, cmd):
        self.cmd = cmd
        self.cmd_event.set()

    def _take_cmd(self):
        if not self.cmd_event.is_set():
            return None
        self.cmd_event.clear()
        c, self.cmd = self.cmd, None
        return c

    # ---- lifecycle ----
    def run(self):
        ctx = None
        page = None
        pw = None
        browser = None
        try:
            # semua objek Playwright dibuat DI DALAM thread ini (greenlet terikat thread)
            pw = sync_playwright().start()
            browser, chan = launch_browser(pw, self.settings)
            self.db.log(f"Browser siap ({chan})", self.aid)
            state = None
            sp = session_path(self.settings["app_dir"], self.aid)
            if sp.exists():
                try:
                    state = json.loads(sess_load(sp))
                except Exception as e:
                    self.db.log(f"Sesi tersimpan tidak bisa dibuka ({e}); login ulang diperlukan",
                                self.aid, level="warn")
            ctx = launch(browser, self.acc, self.settings, state)
            page = ctx.new_page()
            page.set_default_timeout(30000)
            if not check_session(page, self.db, self.aid):
                self.db.log("Sesi tidak aktif - akun dilewati sampai login ulang", self.aid, level="warn")
                self.orch.on_worker_exit(self)
                return
            self._persist(page, sp)
            self.db.log("Worker siap", self.aid)
            self.orch.on_worker_ready(self)
            self._loop(page)
        except Exception as e:
            self.hard_error = f"{type(e).__name__}: {e}"
            self.db.update_account(self.aid, status="error", last_error=self.hard_error)
            self.db.log(f"Worker berhenti: {self.hard_error}", self.aid, level="error")
        finally:
            if page is not None:
                try:
                    self._persist(page, session_path(self.settings["app_dir"], self.aid))
                except Exception:
                    pass
            close_context(ctx)
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if pw is not None:
                    pw.stop()
            except Exception:
                pass
            self.done = True
            self.orch.on_worker_exit(self)

    def _persist(self, page, path):
        """Simpan cookie terenkripsi. Gagal simpan tidak fatal - hanya peringatan."""
        try:
            st = page.context.storage_state()
            sess_save(path, json.dumps(st))
        except Exception as e:
            self.db.log(f"Gagal simpan sesi: {e}", self.aid, level="warn")

    def _loop(self, page):
        idle_since = time.time()
        while not self.orch.stopping:
            c = self._take_cmd()
            if c == "stop":
                self.db.log("Worker berhenti (perintah stop)", self.aid)
                return
            if c == "pause":
                self.paused = True
                self.db.log("Worker dijeda", self.aid)
            elif c == "resume":
                self.paused = False
                self.db.log("Worker dilanjutkan", self.aid)
                idle_since = time.time()

            if self.paused or not self.orch.is_running():
                time.sleep(0.5)
                continue

            item = self.orch.claim(self, page)
            if not item:
                if time.time() - idle_since > 60 or self.orch.is_drained():
                    self.db.log("Antrean untuk worker ini sudah habis", self.aid)
                    return
                time.sleep(random.uniform(3, 7))
                # cek sesi tiap ~60s saat idle; terlalu sering ke FB = curiga bot
                if time.time() - self._last_check > 60:
                    self._last_check = time.time()
                    check_session(page, self.db, self.aid)
                continue

            idle_since = time.time()
            jid = self.db.add_job(self.aid, item["id"])
            try:
                ok, msg = self._run_item(page, item)
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
                self.db.log(f"Item gagal: {msg}", self.aid, item["id"], "error")
            result = "done" if ok else "failed"
            self.db.finish_job(jid, result, msg)
            self.orch.release(self, item, ok, msg)
            self.item_id = None
            self.remaining = 0.0

    # ---- inti: buka link, putar, scroll ----
    def _run_item(self, page, item):
        url = item["url"]
        dur = solve_duration(item, self.settings["default_duration"])
        self.item_id = item["id"]
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            return False, f"gagal buka link: {type(e).__name__}: {e}"
        page.wait_for_timeout(2500)
        dismiss_cookie_wall(page)

        if not _looks_logged_in(page):
            ok = check_session(page, self.db, self.aid)
            if not ok:
                return False, "sesi/login diminta FB saat buka link"

        body = ""
        try:
            body = (page.inner_text("body") or "")[:4000].lower()
        except Exception:
            pass
        if any(h in body for h in BAD_PAGE_HINTS):
            return False, "video tidak tersedia / halaman kosong"

        # pastikan video jalan. FB autoplay kadang butuh sentuhan user.
        try:
            v = page.query_selector("video")
            if v:
                state = page.evaluate("(()=>{const v=document.querySelector('video');"
                                      "return v?{p:v.paused,t:v.currentTime,d:v.duration}:null})()")
                if state is None or state.get("d") in (None, 0):
                    page.wait_for_timeout(4000)
                page.evaluate("(()=>{const v=document.querySelector('video');"
                              "if(v&&v.paused){v.muted=true;v.play().catch(()=>{});}})()")
        except Exception:
            pass

        scroll_on = bool(self.settings.get("scroll_enabled")) and bool(item.get("_scroll", 1))
        speed = {"slow": 1400, "medium": 850, "fast": 420}.get(self.settings.get("scroll_speed"), 850)

        # tunggu video benar-benar bergerak (kalau tidak, tetap dihitung tapi dilaporkan)
        t0 = time.time()
        self.remaining = float(dur)
        progressed = 0.0
        last_t = -1.0
        last_good = time.time()
        saw_video = False
        no_video_deadline = t0 + 12
        while time.time() - t0 < dur:
            if self._take_cmd() == "stop":
                return False, "dihentikan"
            if self.orch.stopping:
                return False, "dihentikan"
            if self.paused:
                time.sleep(0.5)
                continue
            left = dur - (time.time() - t0)
            self.remaining = left
            st = None
            try:
                st = page.evaluate("(()=>{const v=document.querySelector('video');"
                                   "return v?{p:v.paused,t:v.currentTime,d:v.duration}:null})()")
            except Exception:
                return False, "tab/halaman hilang saat memutar"
            if st:
                saw_video = True
                progressed = max(progressed, st.get("t") or 0.0)
                if abs((st.get("t") or 0.0) - last_t) > 0.05:
                    last_t = st.get("t") or 0.0
                    last_good = time.time()
            elif time.time() > no_video_deadline:
                return False, "tidak ada elemen video di halaman ini"
            # jangan lewat dari durasi yang diminta
            page.wait_for_timeout(int(max(150, min(speed * 0.35, left * 1000 - 150))))
            if time.time() - t0 >= dur:
                break
            if scroll_on:
                try:
                    page.mouse.wheel(0, random.randint(int(speed * 0.4), int(speed * 1.1)))
                except Exception:
                    pass
            # video mandek >20s tanpa progres -> lapor, jangan gantung durasi penuh
            if saw_video and progressed <= 0.5 and time.time() - last_good > 20:
                return False, f"video tidak berputar (durasi diminta {dur}s)"

        played = round(progressed)
        if progressed <= 0.5:
            return False, f"tak ada pemutaran terdeteksi selama {dur}s"
        pct = min(100, int(progressed / max(1.0, dur) * 100))
        return True, f"tayang {played}s / minta {dur}s ({pct}% dari durasi), video posisi {round(last_t)}s"


class Orchestrator:
    """Kelola worker + bagi item antrean + retry. State 'running' disimpan di DB."""

    def __init__(self, db):
        self.db = db
        self.lock = threading.RLock()
        self.workers = {}
        self.running = False
        self.stopping = False
        self.retry_max = 3
        self.mode = "all"
        self.split_map = {}      # aid -> set(item_id) kalau mode split
        self._claimed = {}       # item_id -> aid sedang dipakai
        self._jobs_per_item = {}  # item_id -> {aid: hasil}
        self._done_items = set()
        self._cleanup_lock = threading.Lock()

    # ---- start / stop ----
    def is_running(self):
        return self.running and not self.stopping

    def start(self, accounts, settings):
        with self.lock:
            if self.running:
                return False, "sudah berjalan"
            accs = [a for a in accounts if a["enabled"]]
            if not accs:
                return False, "tidak ada akun aktif"
            settings = dict(settings)
            settings["app_dir"] = str(data_dir())
            self.mode = settings.get("mode", "all")
            self.retry_max = int(settings.get("retry_max", 3))
            self.stopping = False
            self._done_items = set()
            self._claimed = {}
            self._jobs_per_item = {}

            # reset status item supaya run baru tidak meneruskan status run lama
            for it in self.db.queue():
                self.db.update_item(it["id"], status="waiting", retries=0)

            order = self.db.queue()
            if self.mode == "split" and order:
                # round-robin, item 1..n ke akun 1..n bergiliran
                accs_sorted = sorted(accs, key=lambda a: a["id"])
                self.split_map = {a["id"]: set() for a in accs_sorted}
                for i, it in enumerate(order):
                    self.split_map[accs_sorted[i % len(accs_sorted)]["id"]].add(it["id"])
            else:
                self.split_map = {}

            self.running = True
            self.workers = {}
            for a in accs:
                if len(self.workers) >= int(settings.get("max_workers", 5)):
                    self.db.log(f"Akun '{a['name']}' tidak dijalankan: batas worker tercapai", a["id"], level="warn")
                    break
                self.workers[a["id"]] = Worker(self, a, settings)
            for w in self.workers.values():
                w.start()
            self.db.log(f"Run dimulai: {len(self.workers)} akun, mode={self.mode}, "
                        f"{len(order)} item antrean")
            return True, f"{len(self.workers)} worker jalan"

    def stop(self, hard=False):
        with self.lock:
            if not self.running:
                return False, "tidak berjalan"
            self.stopping = True
            for w in self.workers.values():
                w.send("stop")
        if hard:
            self.cleanup_now()
        return True, "stop diproses"

    def cleanup_now(self):
        """Join worker sekali saja (idempoten), dipakai stop(hard) dan on_worker_exit."""
        with self._cleanup_lock:
            if not self.workers:
                self.running = False
                return
            self._join_and_cleanup()

    def pause(self):
        for w in self.workers.values():
            w.send("pause")
        return True, "dijeda"

    def resume(self):
        if not self.running:
            return False, "tidak berjalan"
        for w in self.workers.values():
            w.send("resume")
        return True, "dilanjutkan"

    def stop_account(self, aid):
        w = self.workers.get(aid)
        if not w:
            return False, "akun tidak punya worker aktif"
        w.send("stop")
        return True, "akun dihentikan"

    def pause_account(self, aid):
        w = self.workers.get(aid)
        if not w:
            return False, "akun tidak punya worker aktif"
        w.send("pause")
        return True, "akun dijeda"

    def resume_account(self, aid):
        w = self.workers.get(aid)
        if not w:
            return False, "akun tidak punya worker aktif"
        w.send("resume")
        return True, "akun dilanjutkan"

    def _join_and_cleanup(self):
        """Worker menutup browser + playwright-nya sendiri di finally; di sini hanya join."""
        for w in list(self.workers.values()):
            try:
                w.join(timeout=25)
            except Exception:
                pass
        self.running = False
        self.stopping = False
        self.workers = {}
        self.db.log("Semua worker berhenti; browser ditutup")

    # ---- pembagian item ----
    def is_drained(self):
        """True kalau tidak ada lagi item yang bisa diklaim worker mana pun."""
        with self.lock:
            for it in self.db.queue():
                if it["status"] in ("done", "skipped"):
                    continue
                for aid in self.workers:
                    if not self._item_allowed(aid, it["id"]):
                        continue
                    jobs = self.db.jobs_for_item(it["id"])
                    if not any(j["account_id"] == aid for j in jobs):
                        return False
            return True

    def _item_allowed(self, aid, iid):
        if self.mode != "split":
            return True
        return iid in self.split_map.get(aid, set())

    def claim(self, worker, page):
        """Item berikutnya yang BELUM dikerjakan worker ini. Idempoten -> resume/restart aman.

        Kunci eksklusi per (akun, item), bukan per item: di mode 'all' semua akun memang
        harus memutar seluruh antrean, dan itulah paralelismenya.
        """
        aid = worker.aid
        with self.lock:
            for it in self.db.queue():
                iid = it["id"]
                if it["status"] in ("done", "skipped"):
                    continue
                if not self._item_allowed(aid, iid):
                    continue
                jobs = self.db.jobs_for_item(iid)
                if any(j["account_id"] == aid for j in jobs):
                    # akun ini sudah punya job untuk item ini (done/failed/interrupted)
                    continue
                self.db.update_item(iid, status="running")
                worker.item_id = iid
                return it
        return None

    def release(self, worker, item, ok, msg):
        iid = item["id"]
        with self.lock:
            jobs = self.db.jobs_for_item(iid)
            results = {}
            for j in jobs:
                results.setdefault(j["account_id"], []).append(j["result"])
            targets = self._target_accounts(iid)
            all_done = all(
                any(r in ("done",) for r in results.get(a, [])) or
                len([r for r in results.get(a, []) if r in ("failed", "interrupted")]) >= self.retry_max
                for a in targets
            )
            any_done = any("done" in v for v in results.values())
            any_fail = any(("failed" in v or "interrupted" in v) for v in results.values())
            if any_fail and not all_done:
                st = "partial" if any_done else "failed"
            elif all_done and any_done:
                st = "done"
            elif all_done:
                st = "failed"
            else:
                st = "running"
            retries = len(results.get(worker.aid, []))
            self.db.update_item(iid, status=st, retries=retries)
        worker.remaining = 0.0

    def _target_accounts(self, iid):
        if self.mode == "split":
            return [a for a, ids in self.split_map.items() if iid in ids]
        return list(self.workers.keys())

    def on_worker_ready(self, worker):
        pass

    def on_worker_exit(self, worker):
        with self.lock:
            alive = [w for w in self.workers.values() if not w.done]
            if self.running and not self.stopping and not alive:
                self.db.log("Semua worker selesai / berhenti")
                threading.Thread(target=self.cleanup_now, daemon=True).start()

    # ---- status ----
    def status(self):
        with self.lock:
            items = self.db.queue()
            done = sum(1 for i in items if i["status"] == "done")
            failed = sum(1 for i in items if i["status"] in ("failed", "partial"))
            running = sum(1 for i in items if i["status"] == "running")
            ws = []
            for w in self.workers.values():
                acc = self.db.account(w.aid) or {}
                cur = self.db.item(w.item_id) if w.item_id else None
                ws.append({
                    "id": w.aid,
                    "name": acc.get("name", f"#{w.aid}"),
                    "status": acc.get("status", "?"),
                    "busy": w.item_id is not None and not w.paused,
                    "paused": w.paused,
                    "done": w.done,
                    "item_id": w.item_id,
                    "url": (cur or {}).get("url", ""),
                    "remaining": round(w.remaining, 1),
                    "error": acc.get("last_error", ""),
                })
            return {
                "running": self.running, "stopping": self.stopping,
                "mode": self.mode, "workers": ws,
                "total": len(items), "done": done, "failed": failed, "active": running,
            }
