# advisorAPP — AI Workspace Scan

## Stack

- **Backend:** Python 3.10+, Flask 3
- **Templates:** Jinja2
- **Database:** SQLite (stdlib), multi-user (DB per user)
- **CSS:** Tailwind (pre-compiled)
- **Server:** Gunicorn (4 workers)
- **Auth:** bcrypt
- **Market data:** yfinance (Yahoo Finance)

Dependencies (`requirements.txt`): Flask>=3.0, gunicorn>=22.0, yfinance>=0.2, bcrypt>=4.0.0

## Struktur Proyek

```
advisorAPP/
├── app.py                  # Flask application (1457 baris, single-file backend)
├── gunicorn.conf.py        # Production server config
├── requirements.txt        # Python dependencies
├── tailwind.config.js      # Tailwind source config
├── tailwind.input.css      # Tailwind source CSS
├── advisor.db              # SQLite database default (auto-created)
├── advisor_1.db            # SQLite database per-user (user id 1)
├── auth.db                 # SQLite database kredensial (users)
├── static/
│   └── tailwind.output.css # Pre-compiled CSS
└── templates/
    ├── base.html           # Base layout (28017 bytes)
    ├── dashboard.html      # Main dashboard
    ├── income.html         # Income tracking
    ├── ledger.html         # Daily overview list
    ├── ledger_day.html     # Detail per hari
    ├── reports.html        # Financial reports
    ├── categories.html     # Category management (expense + income)
    ├── wishlist.html       # Wishlist
    ├── stocks.html         # Stock portfolio + watchlist (31733 bytes)
    ├── login.html          # Login
    └── register.html       # Register
```

## Fitur

- **Expense tracking** — tambah/edit/hapus, kategori
- **Income tracking** — tambah/edit/hapus, sumber income
- **Ledger** — overview harian + filter bulan, halaman detail per hari
- **Reports** — rentang tanggal, total bulanan, breakdown kategori/sumber, daily average
- **Category management** — expense & income, reassign otomatis ke "Uncategorized"/"Other" saat dihapus
- **Wishlist** — prioritas (high/medium/low), tandai purchased, edit, hapus
- **Stock portfolio** — holdings, avg buy price, lots/shares, refresh harga Yahoo, snapshot P/L harian, chart P/L
- **Watchlist stocks** — pantau ticker + prev close
- **API JSON** — `/api/stock-price/<ticker>`, `/api/stock-pl-history`, `/api/daily-expenses`, `/api/category-breakdown`

## Skema Database (advisor.db)

Tabel: `categories`, `expenses`, `wishlist`, `income_categories`, `income`,
`stocks`, `watchlist_stocks`, `stock_pl_history` (+ `users` di auth.db).

Seed default:
- Expense categories: `Makanan`, `Minuman`
- Income categories: `Bank`, `Cash`, `Stocks Dividend`, `Salary`, `Freelance`

## Auth & Data Model

- `before_request` (`app.py:178`) — endpoint publik hanya `login`, `register`, `static`;
  sisanya redirect ke login jika belum ada session `user_id`.
- DB per-user: `advisor_{user_id}.db` (`get_db`, `app.py:38`).
- Registrasi dibatasi 1 user untuk testing (`app.py:197`).
- User pertama menyalin `advisor.db` → `advisor_1.db` (`app.py:216`).

## Catatan / Temuan

- `git status`: branch `main` tertinggal 1 commit dari `origin/main`
  (`ffe85ec gg`) — bisa fast-forward, belum di-pull. Working tree bersih.
- `register` (`app.py:216`) menyalin `advisor.db` ke `advisor_1.db` untuk user
  pertama; user berikutnya pakai `init_db` — konsisten, tapi copy DB berpotensi
  membawa data lama ke user baru.
- `_fetch_yahoo_price` memanggil `t.info` (`app.py:1109`) yang lambat;
  `refresh-all` melakukan loop + `sleep(0.3)` per ticker — bisa lama untuk
  banyak ticker.
- `requirements.txt` mencantumkan `gunicorn` yang hanya berjalan di Linux;
  di Windows perlu alternatif (Waitress, dsb).
- Tidak ada test suite / lint config ditemukan di workspace.

## Cara Menjalankan

```bash
pip install -r requirements.txt
python app.py                  # dev -> http://localhost:5000
gunicorn -c gunicorn.conf.py app:app   # production
```
