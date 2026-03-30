# FBR Digital Invoice App

A **Flask + SQLite** web application for submitting FBR (Federal Board of Revenue)
Digital Invoices via the FBR API. It supports single real-time entry, bulk Excel
upload, validate & post to FBR, PDF generation, buyer directory, and an admin
panel — all in a clean Bootstrap 5 UI.

---

## 📋 Features

| Feature | Description |
|---|---|
| 🔐 Authentication | Signup / Login / Profile / Password change |
| ⚡ Real-time Entry | Enter a single invoice with dynamic item rows |
| 📊 Excel Bulk Upload | Import many invoices from `.xlsx` / `.xls` |
| ✅ Validate & Post | Send invoices to FBR API (sandbox or production) |
| 📄 PDF + QR Code | Auto-generated after successful FBR post |
| 👥 Buyer Directory | Save and autocomplete buyer NTN/CNIC details |
| 📥 Export | Download all invoices as Excel |
| 🛡️ Admin Panel | Manage users, view system status, daily backups |
| 🔄 Bulk Operations | Validate / post multiple invoices via streaming SSE |

---

## 🔐 Default Login Credentials

> **These are the built-in credentials used when no environment variables are set.**
> Change them before deploying to production (see [Environment Variables](#-environment-variables)).

| Role | Email | Password |
|---|---|---|
| **Super Admin** | `admin@fbr.local` | `Admin@12345` |
| Regular users | *(sign up via `/signup`)* | *(chosen at signup)* |

The Super Admin account is **not** stored in the database — it is authenticated directly
from the `SUPER_ADMIN_EMAIL` and `SUPER_ADMIN_PASSWORD` environment variables (or the
defaults above when those variables are not set).

---

## 🚀 Quick Start (Local Development)

### 1. Clone the repository

```bash
git clone https://github.com/haroonmansha/fbr-invoice-app.git
cd fbr-invoice-app
```

### 2. Create a virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set environment variables (optional for dev)

Copy and edit the example below, or just run with the defaults:

```bash
# Windows PowerShell
$env:FLASK_SECRET_KEY="change-me-in-production"
$env:SUPER_ADMIN_EMAIL="admin@fbr.local"
$env:SUPER_ADMIN_PASSWORD="Admin@12345"

# Linux / macOS
export FLASK_SECRET_KEY="change-me-in-production"
export SUPER_ADMIN_EMAIL="admin@fbr.local"
export SUPER_ADMIN_PASSWORD="Admin@12345"
```

### 5. Run the development server

```bash
python app.py
```

Open **http://localhost:5000** in your browser.

> The SQLite database (`invoices.db`) and all directories (`pdf/`, `qr/`,
> `auto_backups/`, etc.) are created automatically on first run.

---

## 🏭 Production Deployment (Gunicorn + Nginx)

```bash
# Install gunicorn (already in requirements.txt)
gunicorn -w 4 -b 0.0.0.0:8000 "app:app"
```

Use Nginx as a reverse proxy. Example config snippet:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

Or use `passenger_wsgi.py` if your hosting uses Phusion Passenger.

---

## 🔑 Environment Variables

| Variable | Default | Description |
|---|---|---|
| `FLASK_SECRET_KEY` | auto-generated | Flask session secret (set a strong value in production) |
| `SUPER_ADMIN_EMAIL` | `admin@fbr.local` | Super-admin login email |
| `SUPER_ADMIN_PASSWORD` | `Admin@12345` | Super-admin password — **change this!** |
| `SMTP_HOST` | *(optional)* | SMTP server for email notifications |
| `SMTP_PORT` | *(optional)* | SMTP port |
| `SMTP_USER` | *(optional)* | SMTP username |
| `SMTP_PASS` | *(optional)* | SMTP password |

---

## 📂 Project Structure

```
fbr-invoice-app/
├── app.py                    # Main Flask application
├── passenger_wsgi.py         # Phusion Passenger entry point
├── requirements.txt          # Python dependencies
├── .gitignore
├── README.md
├── invoices.db               # SQLite database (auto-created, gitignored)
├── pdf/                      # Generated PDF invoices (gitignored)
├── qr/                       # Generated QR codes (gitignored)
├── auto_backups/             # Daily DB backups (gitignored)
├── static/
│   ├── responses/            # FBR API response dumps (gitignored)
│   └── logos/                # Optional company logos (gitignored)
├── uploads/                  # Temporary Excel uploads (gitignored)
└── templates/
    ├── base.html             # Bootstrap 5 layout with sidebar
    ├── login.html
    ├── signup.html
    ├── dashboard.html
    ├── invoices.html
    ├── realtime_entry.html   # ⭐ Main invoice entry form
    ├── buyers.html
    ├── upload.html
    ├── admin.html
    ├── user_profile.html
    └── change_password.html
```

---

## 📖 How to Use

### First-time Setup
1. Open **http://localhost:5000**
2. Click **Create Account** and fill in your seller details (NTN, name, province, address)
3. Paste your **FBR API Token** (get it from FBR Taxpayer Portal → Digital Invoicing)
4. Choose **Sandbox** for testing or **Production** for live submissions

### Submitting an Invoice
1. Go to **Real-time Entry** from the sidebar
2. Fill invoice header (SINV, date, type)
3. Fill buyer details (or search from saved buyers)
4. Add item rows with HS Code, description, quantity, tax rate
5. Click **Validate & Post to FBR**
6. On success, FBR Invoice Number and PDF download link are shown

### Bulk Upload
1. Go to **Upload Excel** and upload a `.xlsx` file
2. The file must have a `SINV` column (other columns optional — see Upload page for full list)
3. After import, go to **Invoices** to validate and post

---

## 🐛 Bugs Fixed (from original codebase)

1. `import threading` moved to top-level (was causing `NameError`)
2. `import re` and `import queue` moved to top-level
3. `BACKUP_DIR` defined at top with other directory constants
4. `_enrich()` now computes `display_sinv` (was missing → blank in template)
5. `_save_response_dump` uses `_utcnow()` instead of `datetime.now()`
6. `export_invoices_excel` column loop renamed to `ws_col` (no shadowing)
7. `report_monthly_pdf` same rename; no longer silently skips empty-buyer invoices
8. `stream_validate_and_post` / `stream_validate` use `queue.Queue()` properly
9. `_db_upsert` always sets `created_at` via `data.setdefault()`
10. `download_invoice_pdf` uses sanitized `safe` filename in DB ownership check
11. `admin_view_as` preserves `is_super_admin=True` in session
12. `last_cleanup_day` in background worker explicitly initialized before loop

---

## 📜 License

MIT License — free to use and modify.
