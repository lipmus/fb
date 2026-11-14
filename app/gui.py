"""UI desktop (pywebview) + backend Worker per akun.

pywebview dipakai karena: stdlib GUI (tkinter) tak sanggup tabel drag-drop + style modern,
dan Electron/Tauri overkill untuk aplikasi 1 operator lokal. pywebview = WebView2 yang
sudah ada di Windows 10/11, ~5 MB, HTML yang sama bisa dibuka di browser biasa buat debug.

Model: SATU proses (ini yang bikin shutdown bersih). Tiap akun = 1 thread Worker,
masing-masing menyalakan playwright sync sendiri; subprocess browser tidak saling bunuh.
"""
import json
import threading
import time
import traceback
from pathlib import Path

from .downloader import dest_path, is_fb_video_url, normalize, video_id
from .paths import data_dir, db_path, session_path
from .runner import Orchestrator, check_session, launch, dismiss_cookie_wall
from .crypto import load as sess_load
from .db import DB

STATIC = Path(__file__).parent / "static"


class Api:
    def __init__(self):
        self.db = DB(db_path())
        self.orch = Orchestrator(self.db)
        self.busy = set()          # akun yang sedang proses login (worker manual)
        self._dl = {}
        self.recovered = self.db.recover()
        if self.recovered:
            self.db.log(f"Pemulihan: {self.recovered} job terputus dikembalikan ke antrean", level="warn")

    # ---------------- settings ----------------
    def ui_ready(self, ver=None):
        """Dipanggil JS setelah boot berhasil - bukti bridge UI <-> backend hidup."""
        try:
            p = data_dir() / "ui_ready.json"
            p.write_text(json.dumps({"ver": ver, "ts": time.time()}), encoding="utf-8")
        except Exception:
            pass
        return {"ok": True}

    def settings(self):
        s = self.db.settings()
        s["data_dir"] = str(data_dir())
        s["version"] = "1.0"
        return s

    def save_settings(self, patch):
        patch = patch or {}
        allow = {"default_duration", "scroll_enabled", "scroll_speed", "max_workers",
                 "mode", "headless", "mute", "retry_max", "channel"}
        for k, v in patch.items():
            if k in allow:
                self.db.set_setting(k, v)
        return {"ok": True, "settings": self.settings()}

    # ---------------- accounts ----------------
    def accounts(self):
        return {"accounts": self.db.accounts(), "busy": sorted(self.busy)}

    def add_account(self, name):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "Nama akun wajib diisi"}
        aid = self.db.add_account(name, "")
        self.db.log(f"Akun '{name}' ditambahkan; sesi kosong", aid)
        return {"ok": True, "id": aid}

    def set_account(self, aid, patch):
        patch = patch or {}
        kw = {}
        if "enabled" in patch:
            kw["enabled"] = 1 if patch["enabled"] else 0
        if "name" in patch and str(patch["name"]).strip():
            kw["name"] = str(patch["name"]).strip()
        if "proxy" in patch:
            kw["proxy"] = str(patch["proxy"] or "").strip()
        if kw:
            self.db.update_account(int(aid), **kw)
        return {"ok": True}

    def delete_account(self, aid):
        aid = int(aid)
        self.db.del_account(aid)
        p = session_path(data_dir(), aid)
        if p.exists():
            p.write_bytes(b"")
            p.unlink()
        self.db.log(f"Akun #{aid} dihapus (sesi + riwayat ikut dihapus)", level="warn")
        return {"ok": True}

    def login_account(self, aid, headless=False):
        """Buka jendela browser terisolasi; user login manual; sesi disimpan terenkripsi."""
        aid = int(aid)
        acc = self.db.account(aid)
        if not acc:
            return {"ok": False, "error": "akun tidak ditemukan"}
        if aid in self.busy:
            return {"ok": False, "error": "login akun ini sedang berjalan"}
        self.busy.add(aid)

        def job():
            from playwright.sync_api import sync_playwright
            s = self.db.settings()
            t = None
            try:
                t = threading.Thread(target=self._login_flow, args=(aid, headless, s), daemon=True)
                t.start()
            except Exception as e:
                self.db.log(f"Login gagal start: {e}", aid, level="error")
            finally:
                if t:
                    t.join()
                self.busy.discard(aid)

        threading.Thread(target=job, daemon=True).start()
        return {"ok": True, "message": "Jendela login dibuka. Login manual di jendela itu, "
                                       "sesi tersimpan otomatis setelah terdeteksi aktif."}

    def _login_flow(self, aid, headless, settings):
        from playwright.sync_api import sync_playwright
        acc = self.db.account(aid)
        try:
            with sync_playwright() as pw:
                b = pw.chromium.launch(channel="chrome", headless=headless,
                                       args=["--disable-blink-features=AutomationControlled",
                                             "--no-first-run", "--no-default-browser-check"])
                ctx = launch(b, acc, settings, None)
                page = ctx.new_page()
                print(f"[login] akun #{aid} siap dibuat")
                page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
                ok = False
                end = time.time() + 900
                while time.time() < end:
                    try:
                        if page.is_closed():
                            break
                    except Exception:
                        break
                    if self._is_logged_in(page):
                        ok = True
                        break
                    page.wait_for_timeout(2000)
                if ok:
                    self.db.update_account(aid, status="active", last_error="")
                    sess = ctx.storage_state()
                    from .crypto import save as sess_save
                    sess_save(session_path(data_dir(), aid), json.dumps(sess))
                    self.db.log("Login berhasil; sesi disimpan terenkripsi", aid)
                else:
                    self.db.update_account(aid, status="needs_login",
                                           last_error="login tidak terdeteksi selesai")
                    self.db.log("Login tidak selesai / jendela ditutup", aid, level="warn")
                ctx.close()
                b.close()
        except Exception as e:
            self.db.update_account(aid, status="error", last_error=f"{type(e).__name__}: {e}")
            self.db.log(f"Login error: {type(e).__name__}: {e}", aid, level="error")

    @staticmethod
    def _is_logged_in(page):
        url = (page.url or "").lower()
        if any(h in url for h in ("checkpoint", "two_factor", "login/device-based",
                                  "/login", "login.php", "/r.php", "/recover")):
            return False
        try:
            has_login = page.query_selector("input[name='email'], input[name='pass']")
        except Exception:
            return False
        return has_login is None and url.startswith("http")

    def check_account(self, aid):
        aid = int(aid)
        acc = self.db.account(aid)
        if not acc:
            return {"ok": False, "error": "akun tidak ditemukan"}

        def job():
            from playwright.sync_api import sync_playwright
            s = self.db.settings()
            try:
                with sync_playwright() as pw:
                    b = pw.chromium.launch(channel="chrome", headless=True,
                                           args=["--disable-blink-features=AutomationControlled"])
                    st = None
                    p = session_path(data_dir(), aid)
                    if p.exists():
                        st = json.loads(sess_load(p))
                    ctx = launch(b, acc, s, st)
                    page = ctx.new_page()
                    check_session(page, self.db, aid)
                    ctx.close()
                    b.close()
            except Exception as e:
                self.db.update_account(aid, status="error", last_error=f"{type(e).__name__}: {e}")
                self.db.log(f"Cek sesi gagal: {e}", aid, level="error")

        threading.Thread(target=job, daemon=True).start()
        return {"ok": True, "message": "cek sesi dijalankan"}

    # ---------------- queue ----------------
    def queue(self):
        items = self.db.queue()
        s = self.db.settings()
        d = int(s["default_duration"])
        for it in items:
            it["effective"] = it["dur_min"] or d
            j = self.db.jobs_for_item(it["id"])
            it["jobs"] = [{"account": (self.db.account(x["account_id"]) or {}).get("name", f"#{x['account_id']}"),
                           "result": x["result"], "message": x["message"]} for x in j]
        return {"items": items, "default_duration": d}

    def add_links(self, text):
        lines = [normalize(x) for x in (text or "").replace("\r", "\n").split("\n")]
        lines = [x for x in lines if x]
        bad, dup_batch, ok = [], [], []
        seen = set()
        for u in lines:
            if not is_fb_video_url(u):
                bad.append(u)
                continue
            if u in seen:
                dup_batch.append(u)
                continue
            seen.add(u)
            ok.append(u)
        ids, dup_db = self.db.add_items(ok)
        msg = f"{len(ids)} link ditambahkan"
        if dup_db:
            msg += f", {len(dup_db)} duplikat dilewati"
        if dup_batch:
            msg += f", {len(dup_batch)} duplikat di tempelan"
        if bad:
            msg += f", {len(bad)} link ditolak"
        self.db.log(msg)
        return {"ok": True, "added": len(ids), "duplicates": len(dup_db) + len(dup_batch),
                "invalid": bad[:20], "invalid_count": len(bad), "message": msg}

    def update_item(self, iid, patch):
        patch = patch or {}
        kw = {}
        for src, dst in (("dur_min", "dur_min"), ("dur_max", "dur_max"), ("status", "status")):
            if src in patch:
                try:
                    kw[dst] = max(0, int(patch[src] or 0))
                except Exception:
                    pass
        if "url" in patch:
            kw["url"] = normalize(str(patch["url"]))
        if "dur" in patch:
            v = max(0, int(patch["dur"] or 0))
            kw["dur_min"] = v
            kw["dur_max"] = 0
        if kw:
            self.db.update_item(int(iid), **kw)
        return {"ok": True}

    def bulk_duration(self, ids, dur=None, lo=None, hi=None):
        ids = [int(i) for i in (ids or [])]
        if not ids:
            return {"ok": False, "error": "tidak ada item dipilih"}
        for i in ids:
            if lo is not None or hi is not None:
                self.db.update_item(i, dur_min=max(0, int(lo or 0)), dur_max=max(0, int(hi or 0)))
            else:
                self.db.update_item(i, dur_min=max(0, int(dur or 0)), dur_max=0)
        # item yang sedang berjalan tidak diubah selama durasi aktif (FR-15)
        self.db.log(f"Durasi {len(ids)} item diubah massal -> berlaku untuk item berikutnya")
        return {"ok": True, "count": len(ids)}

    def reorder(self, ids):
        self.db.reorder([int(i) for i in (ids or [])])
        return {"ok": True}

    def shuffle(self):
        import random
        items = self.db.queue()
        ids = [i["id"] for i in items]
        random.shuffle(ids)
        self.db.reorder(ids)
        self.db.log("Urutan antrean diacak")
        return {"ok": True}

    def delete_item(self, iid):
        self.db.del_item(int(iid))
        return {"ok": True}

    def delete_items(self, ids):
        for i in (ids or []):
            self.db.del_item(int(i))
        return {"ok": True, "count": len(ids or [])}

    def clear_queue(self, only_done=False):
        if only_done:
            n = 0
            for it in self.db.queue():
                if it["status"] in ("done", "failed", "partial", "skipped"):
                    self.db.del_item(it["id"])
                    n += 1
            self.db.log(f"{n} item selesai/gagal dibersihkan")
            return {"ok": True, "count": n}
        self.db.clear_queue()
        self.db.log("Antrean dikosongkan", level="warn")
        return {"ok": True}

    def reset_status(self):
        for it in self.db.queue():
            self.db.update_item(it["id"], status="waiting", retries=0)
        self.db.log("Status antrean direset ke menunggu")
        return {"ok": True}

    # ---------------- import / export ----------------
    def export_queue(self, fmt="csv"):
        items = self.db.queue()
        out = data_dir() / f"queue_{int(time.time())}.{fmt}"
        if fmt == "csv":
            rows = ["url,dur_min,dur_max,status"]
            for it in items:
                rows.append(f'{it["url"]},{it["dur_min"]},{it["dur_max"]},{it["status"]}')
            out.write_text("\n".join(rows), encoding="utf-8")
        else:
            out.write_text("\n".join(i["url"] for i in items), encoding="utf-8")
        self.db.log(f"Antrean diekspor: {out}")
        return {"ok": True, "path": str(out)}

    def import_queue(self, path, fmt="auto"):
        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": f"file tidak ada: {path}"}
        text = p.read_text(encoding="utf-8", errors="ignore")
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        if lines and lines[0].lower().startswith("url"):
            lines = lines[1:]
        urls, durs = [], []
        for ln in lines:
            parts = [x.strip() for x in ln.split(",")]
            urls.append(parts[0])
            durs.append(int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None)
        res = self.add_links("\n".join(urls))
        if any(d is not None for d in durs):
            for it, d in zip(self.db.queue()[-len(urls):], durs):
                if d:
                    self.db.update_item(it["id"], dur_min=d, dur_max=0)
        return res

    def export_logs(self, account_id=None, level=None):
        rows = self.db.logs(account_id, level, limit=20000)
        out = data_dir() / f"logs_{int(time.time())}.csv"
        lines = ["waktu,akun,item,level,pesan"]
        for r in reversed(rows):
            acc = (self.db.account(r["account_id"]) or {}).get("name", "") if r["account_id"] else ""
            msg = (r["message"] or "").replace('"', "'").replace("\n", " ")
            t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
            lines.append(f'{t},{acc},{r["item_id"] or ""},{r["level"]},"{msg}"')
        out.write_text("\n".join(lines), encoding="utf-8")
        return {"ok": True, "path": str(out)}

    def logs(self, account_id=None, level=None, limit=300):
        rows = self.db.logs(account_id, level, limit=limit)
        for r in rows:
            r["time"] = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            r["account"] = (self.db.account(r["account_id"]) or {}).get("name", "") if r["account_id"] else ""
        return {"logs": rows}

    # ---------------- run control ----------------
    def start(self):
        accs = self.db.accounts()
        active = [a for a in accs if a["enabled"]]
        if not active:
            return {"ok": False, "error": "tidak ada akun aktif"}
        no_session = [a["name"] for a in active if not session_path(data_dir(), a["id"]).exists()]
        if no_session:
            return {"ok": False, "error": f"Akun belum login (sesi kosong): {', '.join(no_session)}"}
        if not self.db.queue():
            return {"ok": False, "error": "antrean kosong"}
        ok, msg = self.orch.start(accs, self.db.settings())
        self.db.log(("Mulai: " if ok else "Gagal mulai: ") + msg, level="info" if ok else "error")
        return {"ok": ok, "message": msg, "error": None if ok else msg}

    def pause(self):
        return {"ok": True, "message": self.orch.pause()[1]}

    def resume(self):
        ok, m = self.orch.resume()
        return {"ok": ok, "message": m, "error": None if ok else m}

    def stop(self):
        ok, m = self.orch.stop(hard=False)
        if ok:
            threading.Thread(target=self.orch.cleanup_now, daemon=True).start()
        return {"ok": True, "message": m}

    def account_action(self, aid, action):
        aid = int(aid)
        fn = {"stop": self.orch.stop_account, "pause": self.orch.pause_account,
              "resume": self.orch.resume_account}.get(action)
        if not fn:
            return {"ok": False, "error": "aksi tidak dikenal"}
        ok, m = fn(aid)
        return {"ok": ok, "message": m, "error": None if ok else m}

    def status(self):
        s = self.orch.status()
        s["accounts"] = self.db.accounts()
        s["busy_login"] = sorted(self.busy)
        s["ram_mb"] = self._ram_mb()
        return s

    @staticmethod
    def _ram_mb():
        try:
            import psutil
            return round(psutil.Process().memory_info().rss / 1048576, 1)
        except Exception:
            return None

    # ---------------- downloader ----------------
    def download_links(self, text, outdir, account_id=None, label="", allow_any=False):
        raw = [x.strip() for x in (text or "").replace("\r", "\n").split("\n") if x.strip()]
        # allow_any: URL media langsung (bertanda tangan) - JANGAN dinormalisasi,
        # query-nya bagian dari izin akses
        urls = raw if allow_any else [normalize(x) for x in raw]
        if not allow_any:
            urls = [u for u in urls if u and is_fb_video_url(u)]
        else:
            urls = [u for u in urls if u.startswith("http")]
        if not urls:
            return {"ok": False, "error": "tidak ada link valid"}
        outdir = Path(outdir or (data_dir() / "downloads"))
        key = f"dl_{int(time.time())}"
        self._dl[key] = {"total": len(urls), "done": 0, "items": [], "stop": False}
        threading.Thread(target=self._dl_run, args=(key, urls, outdir, account_id, label), daemon=True).start()
        return {"ok": True, "key": key, "total": len(urls), "outdir": str(outdir)}

    def download_progress(self, key):
        return self._dl.get(key) or {"total": 0, "done": 0, "items": []}

    def download_stop(self, key):
        if key in self._dl:
            self._dl[key]["stop"] = True
        return {"ok": True}

    def _dl_run(self, key, urls, outdir, account_id, label):
        from playwright.sync_api import sync_playwright
        st = self._dl[key]
        s = self.db.settings()
        accs = self.db.accounts()
        acc = None
        if account_id:
            acc = self.db.account(int(account_id))
        else:
            for a in accs:
                if session_path(data_dir(), a["id"]).exists():
                    acc = a
                    break
        if not acc:
            st["error"] = "tidak ada akun dengan sesi login tersimpan"
            return
        try:
            with sync_playwright() as pw:
                b = pw.chromium.launch(channel="chrome", headless=bool(s.get("headless")),
                                       args=["--disable-blink-features=AutomationControlled",
                                             "--autoplay-policy=no-user-gesture-required"])
                state = None
                p = session_path(data_dir(), acc["id"])
                if p.exists():
                    state = json.loads(sess_load(p))
                ctx = launch(b, acc, s, state)
                page = ctx.new_page()
                for u in urls:
                    if st["stop"]:
                        break
                    rec = {"url": u, "status": "gagal", "message": ""}
                    try:
                        captured = {"url": None, "len": -1}

                        def on_resp(resp):
                            try:
                                u2 = resp.url
                                if ".mp4" not in u2 and "video" not in u2.lower():
                                    return
                                if "/video/" not in u2 and ".mp4" not in u2.split("?")[0]:
                                    return
                                ln = int((resp.headers or {}).get("content-length") or 0)
                                if ln > captured["len"]:
                                    captured["len"], captured["url"] = ln, u2
                            except Exception:
                                pass

                        page.on("response", on_resp)
                        page.goto(u, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(3000)
                        dismiss_cookie_wall(page)
                        try:
                            page.evaluate("(()=>{const v=document.querySelector('video');"
                                          "if(v){v.muted=true;v.play&&v.play().catch(()=>{});}})()")
                        except Exception:
                            pass
                        # tunggu respons video terdeteksi (maks 40s)
                        for _ in range(40):
                            if captured["url"]:
                                break
                            page.wait_for_timeout(1000)
                        print(f"[dl] {u} -> capture: {captured['url']} ({captured['len']} bytes)")
                        if not captured["url"]:
                            rec["message"] = "URL media tidak terdeteksi (video mungkin dibatasi / butuh login)"
                        else:
                            dest = dest_path(outdir, u, label)
                            resp = ctx.request.get(captured["url"], timeout=120000)
                            if resp.ok:
                                dest.write_bytes(resp.body())
                                rec.update(status="berhasil", path=str(dest),
                                           size=dest.stat().st_size, message=f"{dest.stat().st_size // 1024} KB")
                            else:
                                rec["message"] = f"HTTP {resp.status} saat unduh media"
                        page.remove_listener("response", on_resp)
                    except Exception as e:
                        rec["message"] = f"{type(e).__name__}: {e}"
                    st["items"].append(rec)
                    st["done"] += 1
                ctx.close()
                b.close()
        except Exception as e:
            st["error"] = f"{type(e).__name__}: {e}"
        st["finished"] = True


def run():
    import os
    import webview
    api = Api()
    window = webview.create_window(
        "Facebook Auto Watch Manager",
        (STATIC / "index.html").as_uri(),
        js_api=api, width=1420, height=920, min_size=(1100, 700),
    )
    # ponytail: hook khusus tes otomatis; tanpa env ini aplikasi jalan normal
    selftest = os.environ.get("FAW_SELFTEST_SECONDS")
    if selftest:
        def closer():
            time.sleep(float(selftest))
            try:
                window.destroy()
            except Exception:
                pass
        threading.Thread(target=closer, daemon=True).start()
    webview.start(debug=False)


if __name__ == "__main__":
    run()
