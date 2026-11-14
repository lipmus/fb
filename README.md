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

## Looping

Ada di sebelah tombol **Mulai** di header: centang **Loop**, isi `0x` (jumlah putaran;
0 = tanpa batas sampai Stop) dan `30s` (jeda antar putaran). Setelan tersimpan begitu diubah
dan juga ada di tab Pengaturan.

- Satu putaran = setiap akun memutar seluruh antrean sekali (mengikuti mode pembagian).
- **Putaran berikutnya baru mulai setelah SEMUA akun menuntaskan putaran ini.** Kalau tidak,
  akun cepat sudah masuk putaran 2 sementara akun lambat masih di putaran 1 — progresnya
  jadi tidak masuk akal.
- Dashboard menampilkan kartu **Putaran** dengan progres job putaran ini.
- Setiap job menyimpan nomor putarannya di tabel `jobs`, jadi riwayatnya bisa diaudit.
- Stop selalu menghentikan di tempat, termasuk saat menunggu akun lain di akhir putaran.

Catatan semantik: durasi tonton diperlakukan sebagai target, bukan syarat video habis. Video
4 menit yang diminta ditonton 60 detik akan ditandai selesai setelah 60 detik — itu memang
tujuan auto-watch. Kalau loop diaktifkan, video yang sama akan diulang dari awal tiap putaran.

## Impor/ekspor Excel (folder project)

File selalu ditulis & dibaca dari folder project: `D:\auto-nonton-fb\io\`

| File | Isi |
|---|---|
| `antrean.xlsx` | kolom `url`, `durasi_detik`, `status` — satu baris satu link |
| `akun.xlsx` | kolom `nama`, `aktif` (ya/tidak), `status_sesi`, `proxy`, `catatan` |

- Kedua file **ditulis ulang otomatis** setiap antrean atau daftar akun berubah, jadi kamu bisa
  membukanya di Excel kapan saja untuk melihat keadaan sebenarnya.
- Tombol **Simpan ke Excel** / **Simpan akun ke Excel** menulis paksa; **Impor dari Excel**
  membaca file itu kembali.
- Impor antrean bersifat **menambah**; centang **Ganti antrean lama** kalau mau menimpanya.
  Link non-Facebook ditolak, duplikat dilewati — jumlahnya dilaporkan.
- Impor akun menambahkan akun baru dan melewati nama yang sudah ada. **Sesi tetap harus Login
  manual**; impor tidak bisa memindahkan cookie antar mesin.
- Kolom dicocokkan longgar (huruf besar/kecil dan spasi tidak masalah), jadi menambah kolom
  catatan sendiri di Excel tidak merusak impor.
- Lokasi bisa dipaksa lewat env `FAW_IO_DIR`.

## Isi repo

| Path | Fungsi |
|---|---|
| `app/db.py` | SQLite (WAL): accounts, queue_items, jobs (dengan kolom `round`), logs, settings |
| `app/crypto.py` | Enkripsi/dekripsi sesi pakai DPAPI Windows |
| `app/runner.py` | Worker per akun + orkestrator: claim, retry, split, looping, pause/stop |
| `app/gui.py` | Backend pywebview (API untuk UI) + semua aksi |
| `app/io_paths.py` | Lokasi folder `io/` di folder project |
| `app/xlsxio.py` | Baca/tulis `.xlsx` (openpyxl) |
| `app/static/index.html` | UI (tabel antrean drag-drop, dashboard worker, log live) |
| `app/cli.py` | CLI: login, run, check, add-links, download |
| `app/downloader.py` | Validasi/normalisasi link + penamaan file unduhan |
| `tests/*.py` | 7 suite: unit, sesi, paralel, multi-akun, unduhan, UI, loop+Excel |

Arsitektur: **satu proses**, satu thread Worker per akun, masing-masing menyalakan Playwright
sync sendiri dan meluncurkan Chrome sistem. Alasan Chrome sistem: Chromium bundled Playwright
tidak membawa codec H.264, jadi video Facebook tampil hitam. Rusaknya satu worker tidak
menjatuhkan worker lain, dan tidak ada proses terpisah yang perlu di-reap saat aplikasi ditutup.

## Menjalankan tes

```
py -3.12 tests/smoke.py        # unit + browser nyata; cetak OK/FAIL per aspek
py -3.12 tests/session.py      # login/sesi: deteksi login telat render, simpan+pulihkan cookie
py -3.12 tests/parallel.py     # 3 worker paralel nyata: mode all/split, batas worker, item rusak
py -3.12 tests/multi_account.py # N akun = N proses Chrome, cookie tidak bocor antar akun
py -3.12 tests/downloader.py   # pengunduh: server lokal + video uji, cek file hasil
py -3.12 tests/loop.py         # looping (2 putaran, batas, urutan) + impor/ekspor Excel
py -3.12 tests/ui_js.py        # UI: elemen hilang / error JS / call() tanpa API (tanpa GUI)
py -3.12 tests/ui_boot.py      # buka window GUI sungguhan, verifikasi bridge JS↔Python
```

Folder `io/` dibuat otomatis saat pertama kali menulis file Excel. Namanya ada di tab
Pengaturan (Info → folder data) dan di atas kotak tempel link.

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
