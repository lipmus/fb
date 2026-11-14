# Facebook Auto Watch Manager (Multi-Akun)

Aplikasi desktop Windows: memutar daftar link video Facebook otomatis, banyak akun paralel,
durasi tonton per link bisa diatur, auto-scroll, status + log real-time. Sesuai PRD
*PRD – Facebook Auto Watch Manager (Multi-Akun)*.

## Jalan cepat

```
start.bat                     # pasang dependensi (sekali) lalu buka aplikasi
```

Atau manual:

```
py -3.12 -m pip install -r requirements.txt
py -3.12 -c "from app.gui import run; run()"
```

Data aplikasi (db, sesi, log) ada di `%LOCALAPPDATA%\FacebookAutoWatch`.
Bisa dialihkan lewat env `FAW_DATA_DIR`.

## Alur pakai (4 langkah, satu layar per bagian)

1. **Akun** → Tambah akun → klik **Login** → jendela Chrome terbuka, login manual di situ
   (termasuk 2FA). Sesi tersimpan otomatis begitu Facebook terdeteksi sudah login.
   Password tidak pernah disimpan; cookie disimpan terenkripsi (DPAPI Windows).
2. **Antrean** → tempel banyak link (satu per baris) → Tambah ke antrean.
   Drag baris untuk urutan, klik sel durasi untuk ubah per link, atau pilih beberapa baris
   lalu isi durasi massal / rentang acak.
3. **Pengaturan** → durasi default, auto-scroll + kecepatan, maks worker, mode pembagian
   (semua akun putar seluruh antrean / antrean dibagi rata), retry maks.
4. **Dashboard** → **Mulai**. Tiap akun punya worker sendiri (browser context terisolasi,
   cookie sendiri). Status, sisa waktu, dan error per akun tampil real-time.

## CLI

```
py -3.12 -m app.cli accounts                 # daftar akun + status sesi
py -3.12 -m app.cli add-account "Akun 1"
py -3.12 -m app.cli login --id 1             # login manual di jendela Chrome
py -3.12 -m app.cli check                    # cek semua sesi headless
py -3.12 -m app.cli add-links links.txt
py -3.12 -m app.cli set-duration 90
py -3.12 -m app.cli run --duration 60        # jalan tanpa GUI, Ctrl+C = stop bersih
py -3.12 -m app.cli download URL --outdir C:\Download
```

## Pengunduh (tab Pengunduh / `cli download`)

Mengunduh video memakai **cookie sesi akun yang sudah login** — tidak perlu mengekstrak
cookie atau memalsukan tanda tangan URL sendiri. Cara kerja: buka halaman dengan browser
yang sudah login, tangkap URL media terbesar dari network, unduh lewat request context yang
sama. Kalau URL media tidak terdeteksi, pesannya muncul di daftar (video private/berbatas).

Format: `{id}_{label}.mp4`. Kalau `--label` kosong, nama file = `{id}.mp4` — pakai
`--label` untuk memberi konteks.

## Isi repo

| Path | Fungsi |
|---|---|
| `app/db.py` | SQLite (WAL): accounts, queue_items, jobs, logs, settings + pemulihan job terputus |
| `app/crypto.py` | Enkripsi/dekripsi sesi pakai DPAPI Windows |
| `app/runner.py` | Worker per akun + orkestrator (claim, retry, split, pause/stop) |
| `app/gui.py` | Backend pywebview (API untuk UI) + semua aksi |
| `app/static/index.html` | UI (tabel antrean drag-drop, dashboard worker, log live) |
| `app/cli.py` | CLI: login, run, check, add-links, download |
| `app/downloader.py` | Validasi/normalisasi link + penamaan file unduhan |
| `tests/smoke.py` | Tes unit + integrasi nyata (Chrome + video lokal + auto-scroll) |
| `tests/ui_boot.py` | Tes UI end-to-end: window pywebview sungguhan, cek bridge JS↔Python |

Arsitektur: **satu proses**, satu thread Worker per akun, masing-masing menyalakan Playwright
sync sendiri dan meluncurkan Chrome sistem. Alasan Chrome sistem: Chromium bundled Playwright
tidak membawa codec H.264, jadi video Facebook tampil hitam. Rusaknya satu worker tidak
menjatuhkan worker lain, dan tidak ada proses terpisah yang perlu di-reap saat aplikasi ditutup.

## Menjalankan tes

```
py -3.12 tests/smoke.py       # unit + browser nyata; cetak OK/FAIL per aspek
py -3.12 tests/session.py     # login/sesi: deteksi login telat render, simpan+pulihkan cookie
py -3.12 tests/parallel.py    # 3 worker paralel nyata: mode all/split, batas worker, item rusak
py -3.12 tests/multi_account.py # N akun = N proses Chrome, cookie tidak bocor antar akun
py -3.12 tests/downloader.py  # pengunduh: server lokal + video uji, cek file hasil
py -3.12 tests/ui_boot.py     # buka window GUI sungguhan, verifikasi bridge JS↔Python
```

## Sesi tidak tersimpan / minta login berulang?

Cek log: pesan penyebabnya selalu ditulis. Alur penyimpanan sesi:

1. Setelah kamu submit login, app menunggu halaman Facebook **3 kali berturut-turut**
   terlihat sudah login (bukan sekali) — transisi setelah submit sempat terbaca "login"
   sekejap sebelum redirect selesai.
2. Lalu **cookie `c_user` dicek**. Tanpa cookie itu, sesi tidak berguna: app membatalkan
   penyimpanan dan memberi tahu alasannya, bukan menandai akun aktif.
3. File ditulis, lalu **dibaca ulang** untuk memastikan bisa didekripsi. Gagal = status `error`.

Penyebab tersering, berurutan:

- **Jendela login ditutup terlalu cepat.** App butuh ±6 detik setelah submit + tulis file.
  Tunggu sampai kartu akun berubah jadi `aktif`; log akan berbunyi
  "Login berhasil; sesi disimpan & terverifikasi (N cookie)".
- **Facebook minta verifikasi (checkpoint/2FA/CAPTCHA).** Status akun jadi `butuh verifikasi`
  dan **jendela Chrome dibiarkan terbuka** — selesaikan verifikasi di jendela itu. Aplikasi
  menunggu (sampai 30 menit) lalu menyimpan sesi otomatis. Tidak ada bypass CAPTCHA.
  Kalau jendela itu ditutup sebelum selesai, status kembali `belum login`.
- **Login lewat akun yang tidak punya hak.** Sesi tersimpan tapi halaman video menolak.
- **Hapus akun** ikut menghapus file sesinya (`sessions\account_<id>.enc`).

Cek manual kapan saja: tombol **Cek sesi** di tab Akun (headless), atau
`py -3.12 -m app.cli check`.

## Kalau Facebook mengubah struktur halaman

Semua selector dan deteksi halaman terkumpul di bagian atas `app/runner.py`
(`COOKIE_DISMISS`, `CHECKPOINT_HINTS`, `BAD_PAGE_HINTS`, `FB_HOSTS`, `FB_HOME`).
Ubah di situ saja; logika scroll dan pemutaran tidak perlu disentuh.

## Batasan & catatan kepatuhan

- Verifikasi/CAPTCHA **tidak dilewati**. Kalau Facebook minta verifikasi, akun itu berhenti
  dan ditandai `butuh verifikasi` sampai login ulang.
- Gunakan hanya untuk akun milik sendiri, batasi jumlah worker, dan jangan pakai untuk
  menggelembungkan angka tayang / monetisasi.
- Proxy opsional per akun (field Proxy di daftar akun).
- macOS/Linux belum didukung: enkripsi sesi memakai DPAPI (khusus Windows).
