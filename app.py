# ============================================================
#  FBR Digital Invoice App  –  app.py
#  Flask + SQLite backend (no Firebase dependency)
#  All known bugs fixed, clean production-ready structure.
# ============================================================

# ── Standard library ─────────────────────────────────────────────────────────
import os
import re
import json
import queue          # Bug-fix #13: proper top-level import (was __import__("queue"))
import sqlite3
import threading      # Bug-fix #1 & #2: moved to top-level imports
import uuid
import shutil
from datetime import datetime, timezone
from functools import wraps

# ── Third-party ───────────────────────────────────────────────────────────────
import pandas as pd
import requests
import qrcode
from flask import (
    Flask, request, render_template, session, redirect,
    url_for, jsonify, send_file, flash, Response, stream_with_context,
)
from fpdf import FPDF
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ─────────────────────────────────────────────────────────────────────────────
#  App & secret key
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

_secret_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
if os.path.exists(_secret_file):
    with open(_secret_file) as _f:
        app.secret_key = _f.read().strip()
else:
    import secrets as _sec
    _key = _sec.token_hex(32)
    with open(_secret_file, "w") as _f:
        _f.write(_key)
    app.secret_key = _key

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,   # 10 MB upload limit
)

# ─────────────────────────────────────────────────────────────────────────────
#  Directory layout  (Bug-fix #4: BACKUP_DIR defined here near PDF_DIR/QR_DIR)
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DB_PATH         = os.path.join(BASE_DIR, "invoices.db")
PDF_DIR         = os.path.join(BASE_DIR, "pdf")
QR_DIR          = os.path.join(BASE_DIR, "qr")
BACKUP_DIR      = os.path.join(BASE_DIR, "auto_backups")   # was defined late in file before
RESP_DIR        = os.path.join(BASE_DIR, "static", "responses")
UPLOAD_DIR      = os.path.join(BASE_DIR, "uploads")
LOGO_DIR        = os.path.join(BASE_DIR, "static", "logos")

for _d in [PDF_DIR, QR_DIR, BACKUP_DIR, RESP_DIR, UPLOAD_DIR, LOGO_DIR]:
    os.makedirs(_d, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
#  FBR API endpoints
# ─────────────────────────────────────────────────────────────────────────────
FBR_SB_VALIDATE   = "https://gw.fbr.gov.pk/di_data/v1/di/validateinvoicedata_sb"
FBR_SB_POST       = "https://gw.fbr.gov.pk/di_data/v1/di/postinvoicedata_sb"
FBR_PROD_VALIDATE = "https://gw.fbr.gov.pk/di_data/v1/di/validateinvoicedata"
FBR_PROD_POST     = "https://gw.fbr.gov.pk/di_data/v1/di/postinvoicedata"

# ─────────────────────────────────────────────────────────────────────────────
#  Super-admin (env-var only, never stored in DB)
# ─────────────────────────────────────────────────────────────────────────────
SUPER_ADMIN_EMAIL    = os.environ.get("SUPER_ADMIN_EMAIL",    "admin@fbr.local")
SUPER_ADMIN_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD", "Admin@12345")

# ─────────────────────────────────────────────────────────────────────────────
#  Rate-limiting state
# ─────────────────────────────────────────────────────────────────────────────
_login_attempts: dict = {}
_login_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
#  Utility helpers
# ─────────────────────────────────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def s(v) -> str:
    """Safe string: convert to str and strip whitespace."""
    return "" if v is None else str(v).strip()

def _hash_pw(pw: str) -> str:
    return generate_password_hash(pw, method="pbkdf2:sha256", salt_length=16)

def _check_pw(pw: str, hashed: str) -> bool:
    return check_password_hash(hashed, pw)

def _login_rate_ok(ip: str) -> bool:
    """Allow max 5 login attempts per 15 minutes per IP."""
    now = _utcnow().timestamp()
    with _login_lock:
        history = [t for t in _login_attempts.get(ip, []) if now - t < 900]
        if len(history) >= 5:
            return False
        history.append(now)
        _login_attempts[ip] = history
    return True

def _sinv_display_key(sinv: str) -> str:
    """Strip trailing _YYYY suffix added internally (Bug-fix #9)."""
    return re.sub(r'_\d{4}$', '', sinv)

def _fbr_urls(env: str):
    if env == "production":
        return FBR_PROD_VALIDATE, FBR_PROD_POST
    return FBR_SB_VALIDATE, FBR_SB_POST

# ─────────────────────────────────────────────────────────────────────────────
#  Async response saver  (Bug-fix #2: uses threading after top-level import)
# ─────────────────────────────────────────────────────────────────────────────
def _save_response_dump(prefix: str, key: str, body):
    """Save API response to a JSON file in a background thread."""
    def _w():
        try:
            # Bug-fix #3: use _utcnow() instead of datetime.now()
            fname = f"{prefix}_{key}_{_utcnow().strftime('%Y%m%d%H%M%S')}.json"
            path = os.path.join(RESP_DIR, fname)
            with open(path, "w") as fh:
                if isinstance(body, (dict, list)):
                    json.dump(body, fh, indent=2)
                else:
                    fh.write(str(body))
        except Exception as exc:
            app.logger.warning("_save_response_dump failed: %s", exc)
    threading.Thread(target=_w, daemon=True).start()

# ─────────────────────────────────────────────────────────────────────────────
#  Database
# ─────────────────────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def _init_db():
    with _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            email            TEXT PRIMARY KEY,
            password         TEXT NOT NULL,
            fbr_token        TEXT DEFAULT '',
            seller_ntn       TEXT DEFAULT '',
            seller_name      TEXT DEFAULT '',
            seller_province  TEXT DEFAULT '',
            seller_address   TEXT DEFAULT '',
            fbr_env          TEXT DEFAULT 'sandbox',
            contact_email    TEXT DEFAULT '',
            contact_phone    TEXT DEFAULT '',
            created_at       TEXT NOT NULL DEFAULT '',
            updated_at       TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id               TEXT PRIMARY KEY,
            user_email       TEXT NOT NULL,
            sinv             TEXT NOT NULL,
            invoice_date     TEXT DEFAULT '',
            invoice_type     TEXT DEFAULT 'Sale Invoice',
            status           TEXT DEFAULT 'pending',
            fbr_invoice_no   TEXT DEFAULT '',
            pdf_path         TEXT DEFAULT '',
            payload          TEXT DEFAULT '{}',
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            UNIQUE(user_email, sinv)
        );
        CREATE INDEX IF NOT EXISTS idx_inv_user ON invoices(user_email);

        CREATE TABLE IF NOT EXISTS buyers (
            id             TEXT PRIMARY KEY,
            user_email     TEXT NOT NULL,
            ntn_cnic       TEXT DEFAULT '',
            business_name  TEXT DEFAULT '',
            province       TEXT DEFAULT '',
            address        TEXT DEFAULT '',
            reg_type       TEXT DEFAULT 'Registered',
            strn           TEXT DEFAULT '',
            created_at     TEXT NOT NULL DEFAULT '',
            updated_at     TEXT NOT NULL DEFAULT '',
            UNIQUE(user_email, ntn_cnic)
        );
        CREATE INDEX IF NOT EXISTS idx_buyers_user ON buyers(user_email);
        """)

_init_db()

# ─── generic DB helpers ───────────────────────────────────────────────────────
def _db_upsert(table: str, data: dict, conflict_col: str = "id"):
    """Generic upsert. Bug-fix #10: created_at always set."""
    # Bug-fix #10: ensure created_at is always provided
    data.setdefault("created_at", _utcnow().isoformat())
    data["updated_at"] = _utcnow().isoformat()
    cols  = list(data.keys())
    ph    = ",".join("?" * len(cols))
    upd   = ",".join(
        f"{c}=excluded.{c}" for c in cols
        if c not in ("id", "created_at", conflict_col)
    )
    sql = (
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) "
        f"ON CONFLICT({conflict_col}) DO UPDATE SET {upd}"
    )
    with _db() as conn:
        conn.execute(sql, list(data.values()))

def _db_get(table: str, **where):
    conds = " AND ".join(f"{k}=?" for k in where)
    with _db() as conn:
        row = conn.execute(
            f"SELECT * FROM {table} WHERE {conds} LIMIT 1",
            list(where.values())
        ).fetchone()
    return dict(row) if row else None

def _db_get_all(table: str, order_by="created_at DESC", **where) -> list:
    conds = " AND ".join(f"{k}=?" for k in where) if where else "1=1"
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {conds} ORDER BY {order_by}",
            list(where.values())
        ).fetchall()
    return [dict(r) for r in rows]

# ─────────────────────────────────────────────────────────────────────────────
#  Invoice enrichment  (Bug-fix #3: display_sinv now computed)
# ─────────────────────────────────────────────────────────────────────────────
def _enrich(row: dict) -> dict:
    raw = {}
    try:
        raw = json.loads(row.get("payload") or "{}")
    except Exception:
        pass
    row["payload_obj"]  = raw
    row["buyer_name"]   = s(raw.get("buyerBusinessName", ""))
    row["buyer_ntn"]    = s(raw.get("buyerNTNCNIC", ""))
    row["item_count"]   = len(raw.get("items", []))
    # Bug-fix #3: compute display_sinv from payload or strip year suffix
    row["display_sinv"] = (
        s(raw.get("_sinv_display", "")).strip()
        or re.sub(r'_\d{4}$', '', row.get("sinv", ""))
    )
    return row

# ─────────────────────────────────────────────────────────────────────────────
#  Auth decorators
# ─────────────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def _inner(*a, **kw):
        if not session.get("email"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login_page"))
        return f(*a, **kw)
    return _inner

def admin_required(f):
    @wraps(f)
    def _inner(*a, **kw):
        if not session.get("is_super_admin"):
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return f(*a, **kw)
    return _inner

# ─────────────────────────────────────────────────────────────────────────────
#  Auth routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if session.get("email"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email    = s(request.form.get("email",    "")).lower()
        password = s(request.form.get("password", ""))
        ip       = request.remote_addr or "unknown"

        if not _login_rate_ok(ip):
            flash("Too many login attempts. Please wait 15 minutes.", "error")
            return render_template("login.html")

        # Super-admin short-circuit
        if email == SUPER_ADMIN_EMAIL.lower() and password == SUPER_ADMIN_PASSWORD:
            session["email"]          = SUPER_ADMIN_EMAIL
            session["is_super_admin"] = True
            session["seller_name"]    = "Super Admin"
            flash("Welcome, Admin!", "success")
            return redirect(url_for("admin_panel"))

        user = _db_get("users", email=email)
        if user and _check_pw(password, user["password"]):
            session["email"]        = email
            session["seller_name"]  = user.get("seller_name", "")
            session["seller_ntn"]   = user.get("seller_ntn",  "")
            session["fbr_env"]      = user.get("fbr_env", "sandbox")
            flash(f"Welcome back, {user.get('seller_name') or email}!", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("email"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email           = s(request.form.get("email",           "")).lower()
        password        = s(request.form.get("password",        ""))
        fbr_token       = s(request.form.get("fbr_token",       ""))
        seller_ntn      = s(request.form.get("seller_ntn",      ""))
        seller_name     = s(request.form.get("seller_name",     ""))
        seller_province = s(request.form.get("seller_province", ""))
        seller_address  = s(request.form.get("seller_address",  ""))
        fbr_env         = s(request.form.get("fbr_env", "sandbox"))

        errors = []
        if not email:                   errors.append("Email is required.")
        if not password:                errors.append("Password is required.")
        elif len(password) < 8:         errors.append("Password must be at least 8 characters.")
        if _db_get("users", email=email): errors.append("An account with this email already exists.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("signup.html", form=request.form)

        _db_upsert("users", {
            "email":           email,
            "password":        _hash_pw(password),
            "fbr_token":       fbr_token,
            "seller_ntn":      seller_ntn,
            "seller_name":     seller_name,
            "seller_province": seller_province,
            "seller_address":  seller_address,
            "fbr_env":         fbr_env,
            "contact_email":   email,
            "contact_phone":   "",
            "created_at":      _utcnow().isoformat(),
        }, conflict_col="email")

        flash("Account created! Please log in.", "success")
        return redirect(url_for("login_page"))

    return render_template("signup.html", form={})


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login_page"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def user_profile():
    email = session["email"]
    user  = _db_get("users", email=email) or {}

    if request.method == "POST":
        data          = request.get_json(silent=True) or {}
        contact_email = s(data.get("contact_email", ""))
        contact_phone = s(data.get("contact_phone", ""))
        # Also allow updating seller info and FBR token
        seller_name     = s(data.get("seller_name",     user.get("seller_name",     "")))
        seller_province = s(data.get("seller_province", user.get("seller_province", "")))
        seller_address  = s(data.get("seller_address",  user.get("seller_address",  "")))
        # fbr_token update (optional — only update if explicitly sent)
        fbr_token = s(data.get("fbr_token", user.get("fbr_token", "")))
        with _db() as conn:
            conn.execute(
                """UPDATE users SET contact_email=?, contact_phone=?,
                   seller_name=?, seller_province=?, seller_address=?,
                   fbr_token=?, updated_at=?
                   WHERE email=?""",
                [contact_email, contact_phone,
                 seller_name, seller_province, seller_address,
                 fbr_token, _utcnow().isoformat(), email]
            )
        # Refresh session display name
        session["seller_name"] = seller_name
        return jsonify({"ok": True})

    return render_template("user_profile.html", user=user)


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    email = session["email"]
    if request.method == "POST":
        current  = s(request.form.get("current_password",  ""))
        new_pw   = s(request.form.get("new_password",      ""))
        confirm  = s(request.form.get("confirm_password",  ""))
        user = _db_get("users", email=email)
        if not user or not _check_pw(current, user["password"]):
            flash("Current password is incorrect.", "error")
        elif len(new_pw) < 8:
            flash("New password must be at least 8 characters.", "error")
        elif new_pw != confirm:
            flash("Passwords do not match.", "error")
        else:
            with _db() as conn:
                conn.execute(
                    "UPDATE users SET password=? WHERE email=?",
                    [_hash_pw(new_pw), email]
                )
            flash("Password changed successfully.", "success")
            return redirect(url_for("user_profile"))
    return render_template("change_password.html")

# ─────────────────────────────────────────────────────────────────────────────
#  Dashboard
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def dashboard():
    email    = session["email"]
    invoices = _db_get_all("invoices", user_email=email)
    total    = len(invoices)
    posted   = sum(1 for i in invoices if i["status"] == "posted")
    pending  = sum(1 for i in invoices if i["status"] == "pending")
    failed   = sum(1 for i in invoices if i["status"] in ("validation_failed", "post_failed"))
    recent   = [_enrich(r) for r in invoices[:5]]
    return render_template("dashboard.html",
                           total=total, posted=posted,
                           pending=pending, failed=failed,
                           recent=recent)

# ─────────────────────────────────────────────────────────────────────────────
#  Invoices list
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/invoices")
@login_required
def invoices_page():
    email    = session["email"]
    rows     = _db_get_all("invoices", user_email=email)
    enriched = [_enrich(r) for r in rows]
    payloads_json = json.dumps({r["sinv"]: r["payload_obj"] for r in enriched})
    return render_template("invoices.html",
                           invoices=enriched,
                           invoice_payloads_json=payloads_json)


@app.route("/api/invoice-data/<sinv>")
@login_required
def api_invoice_data(sinv):
    email = session["email"]
    row   = _db_get("invoices", user_email=email, sinv=sinv)
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(json.loads(row.get("payload") or "{}"))


@app.route("/api/invoice/delete/<sinv>", methods=["POST"])
@login_required
def api_invoice_delete(sinv):
    email = session["email"]
    with _db() as conn:
        conn.execute(
            "DELETE FROM invoices WHERE user_email=? AND sinv=?",
            [email, sinv]
        )
    return jsonify({"ok": True})

# ─────────────────────────────────────────────────────────────────────────────
#  Real-time entry
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/realtime")
@login_required
def realtime_entry():
    email      = session["email"]
    user       = _db_get("users", email=email) or {}
    is_sandbox = user.get("fbr_env", "sandbox") == "sandbox"
    buyers     = _db_get_all("buyers", order_by="business_name ASC", user_email=email)
    edit_sinv  = request.args.get("edit", "")
    edit_json  = "null"

    if edit_sinv:
        # Try exact sinv_key match first, then display-name match
        inv_year  = _utcnow().year
        sinv_key  = f"{edit_sinv}_{inv_year}"
        row       = _db_get("invoices", user_email=email, sinv=sinv_key)
        if row:
            payload = json.loads(row.get("payload") or "{}")
            payload["_sinv_display"] = edit_sinv
            payload["_db_sinv"]      = row["sinv"]
            edit_json = json.dumps(payload)

    return render_template("realtime_entry.html",
                           user=user,
                           is_sandbox=is_sandbox,
                           buyers=buyers,
                           edit_sinv=edit_sinv,
                           edit_invoice_json=edit_json)


@app.route("/api/realtime/submit", methods=["POST"])
@login_required
def api_realtime_submit():
    email = session["email"]
    user  = _db_get("users", email=email) or {}
    token = s(user.get("fbr_token", ""))
    if not token:
        return jsonify({"ok": False, "error": "No FBR token configured. Please update your profile."}), 400

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "No JSON payload received."}), 400

    sinv_display = s(data.pop("_sinv_display", "")).strip()
    data.pop("_force", None)
    inv_year = _utcnow().year
    sinv_key = f"{sinv_display}_{inv_year}" if sinv_display else str(uuid.uuid4())

    env           = user.get("fbr_env", "sandbox")
    validate_url, post_url = _fbr_urls(env)
    headers       = {"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"}

    # ── Validate ──────────────────────────────────────────────────────────────
    try:
        vr = requests.post(validate_url, headers=headers, json=data, timeout=30)
        _save_response_dump("validate", sinv_display or "rt",
                            vr.json() if vr.content else {})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Validation request failed: {exc}"}), 500

    try:
        vj    = vr.json()
        vdata = vj.get("validationResponse", {})
        if vdata.get("status", "").lower() != "valid":
            errs = vdata.get("errors") or [vdata.get("error", vr.text)]
            return jsonify({"ok": False, "validation_errors": errs}), 422
    except Exception:
        return jsonify({"ok": False,
                        "error": f"FBR validation response parse error: {vr.text}"}), 500

    # ── Post ──────────────────────────────────────────────────────────────────
    try:
        pr = requests.post(post_url, headers=headers, json=data, timeout=30)
        _save_response_dump("post", sinv_display or "rt",
                            pr.json() if pr.content else {})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Post request failed: {exc}"}), 500

    try:
        pj        = pr.json()
        fbr_inv_no = s(pj.get("invoiceNumber", ""))
    except Exception:
        return jsonify({"ok": False,
                        "error": f"FBR post response parse error: {pr.text}"}), 500

    if not fbr_inv_no:
        return jsonify({"ok": False,
                        "error": f"FBR did not return invoice number. Response: {pr.text}"}), 500

    # ── Generate QR code ──────────────────────────────────────────────────────
    qr_file = ""
    try:
        qr_img  = qrcode.make(
            f"FBR Invoice: {fbr_inv_no}\n"
            f"Seller: {data.get('sellerBusinessName','')}\n"
            f"Date: {data.get('invoiceDate','')}"
        )
        qr_file = f"{sinv_display or uuid.uuid4()}_{fbr_inv_no}.png"
        qr_img.save(os.path.join(QR_DIR, qr_file))
    except Exception:
        pass

    # ── Generate PDF ──────────────────────────────────────────────────────────
    pdf_file = ""
    try:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", "B", 16)
        pdf.cell(0, 12, "FBR Digital Invoice", 0, 1, "C")
        pdf.set_font("Arial", "", 10)
        pdf.ln(2)
        pdf.cell(50, 7, "FBR Invoice No:", 0)
        pdf.set_font("Arial", "B", 10)
        pdf.cell(0, 7, fbr_inv_no, 0, 1)
        pdf.set_font("Arial", "", 10)
        pdf.cell(50, 7, "SINV:", 0);          pdf.cell(0, 7, sinv_display, 0, 1)
        pdf.cell(50, 7, "Invoice Date:", 0);  pdf.cell(0, 7, data.get("invoiceDate",""), 0, 1)
        pdf.cell(50, 7, "Invoice Type:", 0);  pdf.cell(0, 7, data.get("invoiceType",""), 0, 1)
        pdf.ln(3)
        pdf.set_font("Arial", "B", 11)
        pdf.cell(0, 8, "Seller", 0, 1)
        pdf.set_font("Arial", "", 10)
        pdf.cell(50, 7, "Name:", 0);    pdf.cell(0, 7, data.get("sellerBusinessName",""), 0, 1)
        pdf.cell(50, 7, "NTN/CNIC:", 0); pdf.cell(0, 7, data.get("sellerNTNCNIC",""), 0, 1)
        pdf.cell(50, 7, "Province:", 0); pdf.cell(0, 7, data.get("sellerProvince",""), 0, 1)
        pdf.ln(3)
        pdf.set_font("Arial", "B", 11)
        pdf.cell(0, 8, "Buyer", 0, 1)
        pdf.set_font("Arial", "", 10)
        pdf.cell(50, 7, "Name:", 0);    pdf.cell(0, 7, data.get("buyerBusinessName",""), 0, 1)
        pdf.cell(50, 7, "NTN/CNIC:", 0); pdf.cell(0, 7, data.get("buyerNTNCNIC",""), 0, 1)
        pdf.cell(50, 7, "Province:", 0); pdf.cell(0, 7, data.get("buyerProvince",""), 0, 1)
        pdf.ln(5)
        # Items table header
        pdf.set_font("Arial", "B", 9)
        pdf.set_fill_color(230, 230, 230)
        col_w = [55, 12, 18, 28, 28, 28, 22]
        hdrs  = ["Description", "UoM", "Qty", "Value Excl ST", "Sales Tax", "Rate", "Discount"]
        for i, h in enumerate(hdrs):
            pdf.cell(col_w[i], 7, h, 1, 0, "C", True)
        pdf.ln()
        pdf.set_font("Arial", "", 8)
        for item in data.get("items", []):
            pdf.cell(55, 6, str(item.get("productDescription",""))[:30], 1)
            pdf.cell(12, 6, str(item.get("uoM","")), 1, 0, "C")
            pdf.cell(18, 6, str(item.get("quantity","")), 1, 0, "R")
            pdf.cell(28, 6, str(item.get("valueSalesExcludingST","")), 1, 0, "R")
            pdf.cell(28, 6, str(item.get("salesTaxApplicable","")), 1, 0, "R")
            pdf.cell(28, 6, str(item.get("rate","")), 1, 0, "C")
            pdf.cell(22, 6, str(item.get("discount","")), 1, 0, "R")
            pdf.ln()
        # QR image
        if qr_file:
            qr_path = os.path.join(QR_DIR, qr_file)
            if os.path.exists(qr_path):
                pdf.ln(5)
                pdf.image(qr_path, x=10, y=pdf.get_y(), w=40)
        pdf_file = f"{sinv_display or uuid.uuid4()}_{fbr_inv_no}.pdf"
        pdf.output(os.path.join(PDF_DIR, pdf_file))
    except Exception as pe:
        app.logger.warning("PDF generation failed: %s", pe)

    # ── Persist to DB ─────────────────────────────────────────────────────────
    payload_save = dict(data)
    payload_save["_sinv_display"]  = sinv_display
    payload_save["_fbr_invoice_no"] = fbr_inv_no

    _db_upsert("invoices", {
        "id":            str(uuid.uuid4()),
        "user_email":    email,
        "sinv":          sinv_key,
        "invoice_date":  s(data.get("invoiceDate", "")),
        "invoice_type":  s(data.get("invoiceType", "Sale Invoice")),
        "status":        "posted",
        "fbr_invoice_no": fbr_inv_no,
        "pdf_path":      pdf_file,
        "payload":       json.dumps(payload_save),
        "created_at":    _utcnow().isoformat(),
    }, conflict_col="id")

    pdf_url = url_for("download_invoice_pdf", filename=pdf_file) if pdf_file else ""
    return jsonify({"ok": True, "fbr_invoice_no": fbr_inv_no, "pdf_url": pdf_url})

# ─────────────────────────────────────────────────────────────────────────────
#  Excel upload
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload_excel():
    email = session["email"]
    user  = _db_get("users", email=email) or {}

    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("No file selected.", "error")
            return redirect(request.url)

        fname = secure_filename(file.filename)
        if not fname.lower().endswith((".xlsx", ".xls")):
            flash("Only .xlsx and .xls files are supported.", "error")
            return redirect(request.url)

        try:
            df = pd.read_excel(file)
        except Exception as exc:
            flash(f"Error reading Excel: {exc}", "error")
            return redirect(request.url)

        if "SINV" not in df.columns:
            flash("Excel file must contain a 'SINV' column.", "error")
            return redirect(request.url)

        inv_year  = _utcnow().year
        processed = 0
        skipped   = 0

        for sinv_raw, group in df.groupby("SINV"):
            sinv_display = s(sinv_raw)
            sinv_key     = f"{sinv_display}_{inv_year}"
            header       = group.iloc[0].to_dict()

            try:
                inv_date = pd.to_datetime(header.get("invoiceDate")).strftime("%Y-%m-%d")
            except Exception:
                inv_date = _utcnow().strftime("%Y-%m-%d")

            payload = {
                "_sinv_display":      sinv_display,
                "invoiceType":        s(header.get("invoiceType", "Sale Invoice")),
                "invoiceDate":        inv_date,
                "sellerNTNCNIC":      s(user.get("seller_ntn", "")),
                "sellerBusinessName": s(user.get("seller_name", "")),
                "sellerProvince":     s(user.get("seller_province", "")),
                "sellerAddress":      s(user.get("seller_address", "")),
                "buyerNTNCNIC":       s(header.get("buyerNTNCNIC", "")),
                "buyerBusinessName":  s(header.get("buyerBusinessName", "")),
                "buyerProvince":      s(header.get("buyerProvince", "")),
                "buyerAddress":       s(header.get("buyerAddress", "")),
                "buyerRegistrationType": s(header.get("buyerRegistrationType","Registered")),
                "scenarioId":         s(header.get("scenarioId", "SN001")),
                "items": [],
            }

            for item_row in group.to_dict("records"):
                rate_raw = s(str(item_row.get("rate", "0")))
                try:
                    rate = f"{int(float(rate_raw.replace('%','')))}%"
                except Exception:
                    rate = "0%"
                payload["items"].append({
                    "hsCode":                          s(item_row.get("hsCode", "")),
                    "productDescription":              s(item_row.get("productDescription", "")),
                    "uoM":                             s(item_row.get("uoM", "")),
                    "quantity":                        float(item_row.get("quantity", 0) or 0),
                    "valueSalesExcludingST":            float(item_row.get("valueSalesExcludingST", 0) or 0),
                    "rate":                            rate,
                    "salesTaxApplicable":              float(item_row.get("salesTaxApplicable", 0) or 0),
                    "discount":                        float(item_row.get("discount", 0) or 0),
                    "furtherTax":                      float(item_row.get("furtherTax", 0) or 0),
                    "fedPayable":                      float(item_row.get("fedPayable", 0) or 0),
                    "salesTaxWithheldAtSource":        float(item_row.get("salesTaxWithheldAtSource", 0) or 0),
                    "extraTax":                        s(item_row.get("extraTax", "")),
                    "fixedNotifiedValueOrRetailPrice": float(item_row.get("fixedNotifiedValueOrRetailPrice", 0) or 0),
                    "sroScheduleNo":                   s(item_row.get("sroScheduleNo", "")),
                    "sroItemSerialNo":                 s(item_row.get("sroItemSerialNo", "")),
                })

            try:
                _db_upsert("invoices", {
                    "id":           str(uuid.uuid4()),
                    "user_email":   email,
                    "sinv":         sinv_key,
                    "invoice_date": inv_date,
                    "invoice_type": payload["invoiceType"],
                    "status":       "pending",
                    "payload":      json.dumps(payload),
                    "created_at":   _utcnow().isoformat(),
                }, conflict_col="id")
                processed += 1
            except Exception as exc:
                app.logger.warning("Import SINV %s failed: %s", sinv_display, exc)
                skipped += 1

        flash(f"Imported {processed} invoice(s). {skipped} skipped.", "success")
        return redirect(url_for("invoices_page"))

    return render_template("upload.html", user=user)

# ─────────────────────────────────────────────────────────────────────────────
#  Validate & Post (single invoice via AJAX)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/invoice/validate/<sinv>", methods=["POST"])
@login_required
def api_validate_invoice(sinv):
    email = session["email"]
    user  = _db_get("users", email=email) or {}
    token = s(user.get("fbr_token", ""))
    if not token:
        return jsonify({"ok": False, "error": "No FBR token configured."}), 400

    row = _db_get("invoices", user_email=email, sinv=sinv)
    if not row:
        return jsonify({"ok": False, "error": "Invoice not found."}), 404

    payload      = json.loads(row.get("payload") or "{}")
    validate_url, _ = _fbr_urls(user.get("fbr_env", "sandbox"))
    headers      = {"Content-Type": "application/json",
                    "Authorization": f"Bearer {token}"}
    try:
        vr = requests.post(validate_url, headers=headers, json=payload, timeout=30)
        vj = vr.json()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    vdata    = vj.get("validationResponse", {})
    is_valid = vdata.get("status", "").lower() == "valid"
    status   = "valid" if is_valid else "validation_failed"
    _update_invoice_status(email, sinv, status)
    _save_response_dump("validate", sinv, vj)

    return jsonify({
        "ok":      is_valid,
        "status":  status,
        "message": "Valid" if is_valid else (
            vdata.get("error") or vr.text),
    })


@app.route("/api/invoice/post/<sinv>", methods=["POST"])
@login_required
def api_post_invoice(sinv):
    email = session["email"]
    user  = _db_get("users", email=email) or {}
    token = s(user.get("fbr_token", ""))
    if not token:
        return jsonify({"ok": False, "error": "No FBR token configured."}), 400

    row = _db_get("invoices", user_email=email, sinv=sinv)
    if not row:
        return jsonify({"ok": False, "error": "Invoice not found."}), 404
    if row["status"] != "valid":
        return jsonify({"ok": False, "error": "Invoice must be validated first."}), 400

    payload = json.loads(row.get("payload") or "{}")
    _, post_url = _fbr_urls(user.get("fbr_env", "sandbox"))
    headers     = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {token}"}
    try:
        pr = requests.post(post_url, headers=headers, json=payload, timeout=30)
        pj = pr.json()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    fbr_inv_no = s(pj.get("invoiceNumber", ""))
    _save_response_dump("post", sinv, pj)

    if not fbr_inv_no:
        _update_invoice_status(email, sinv, "post_failed")
        return jsonify({"ok": False, "error": f"No invoice number returned: {pr.text}"}), 500

    # Generate PDF
    pdf_file = _generate_pdf(sinv, fbr_inv_no, payload)
    _update_invoice_status(email, sinv, "posted", fbr_inv_no, pdf_file)

    pdf_url = url_for("download_invoice_pdf", filename=pdf_file) if pdf_file else ""
    return jsonify({"ok": True, "fbr_invoice_no": fbr_inv_no, "pdf_url": pdf_url})

# ─────────────────────────────────────────────────────────────────────────────
#  Streaming bulk validate-and-post  (Bug-fix #13: proper queue import)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/stream/validate-post", methods=["POST"])
@login_required
def stream_validate_and_post():
    email = session["email"]
    user  = _db_get("users", email=email) or {}
    token = s(user.get("fbr_token", ""))
    if not token:
        return jsonify({"error": "No FBR token"}), 400

    env          = user.get("fbr_env", "sandbox")
    validate_url, post_url = _fbr_urls(env)
    data_payload = request.get_json(silent=True) or {}
    sinvs        = data_payload.get("sinvs", [])

    # Bug-fix #13: use queue.Queue() (was __import__("queue").Queue())
    q = queue.Queue()

    def worker():
        for sinv_key in sinvs:
            row = _db_get("invoices", user_email=email, sinv=sinv_key)
            if not row:
                q.put({"sinv": sinv_key, "status": "error", "message": "Not found"})
                continue
            try:
                pl      = json.loads(row.get("payload") or "{}")
                hdrs    = {"Content-Type": "application/json",
                           "Authorization": f"Bearer {token}"}
                vr      = requests.post(validate_url, headers=hdrs, json=pl, timeout=30)
                vj      = vr.json()
                if vj.get("validationResponse", {}).get("status","").lower() != "valid":
                    _update_invoice_status(email, sinv_key, "validation_failed")
                    q.put({"sinv": sinv_key, "status": "validation_failed"})
                    continue
                pr      = requests.post(post_url, headers=hdrs, json=pl, timeout=30)
                pj      = pr.json()
                fbr_no  = s(pj.get("invoiceNumber",""))
                if fbr_no:
                    pdf_f = _generate_pdf(sinv_key, fbr_no, pl)
                    _update_invoice_status(email, sinv_key, "posted", fbr_no, pdf_f)
                    q.put({"sinv": sinv_key, "status": "posted", "fbr_no": fbr_no})
                else:
                    _update_invoice_status(email, sinv_key, "post_failed")
                    q.put({"sinv": sinv_key, "status": "post_failed"})
            except Exception as exc:
                q.put({"sinv": sinv_key, "status": "error", "message": str(exc)})
        q.put(None)  # sentinel

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            item = q.get()
            if item is None:
                yield f"data: {json.dumps({'done': True})}\n\n"
                break
            yield f"data: {json.dumps(item)}\n\n"

    return Response(stream_with_context(generate()), content_type="text/event-stream")


@app.route("/api/stream/validate-only", methods=["POST"])
@login_required
def stream_validate():
    """Streaming validate-only for bulk operations."""
    email = session["email"]
    user  = _db_get("users", email=email) or {}
    token = s(user.get("fbr_token", ""))
    if not token:
        return jsonify({"error": "No FBR token"}), 400

    env          = user.get("fbr_env", "sandbox")
    validate_url, _ = _fbr_urls(env)
    data_payload = request.get_json(silent=True) or {}
    sinvs        = data_payload.get("sinvs", [])
    q            = queue.Queue()   # Bug-fix #13: proper import

    def worker():
        for sinv_key in sinvs:
            row = _db_get("invoices", user_email=email, sinv=sinv_key)
            if not row:
                q.put({"sinv": sinv_key, "status": "error", "message": "Not found"})
                continue
            try:
                pl   = json.loads(row.get("payload") or "{}")
                hdrs = {"Content-Type": "application/json",
                        "Authorization": f"Bearer {token}"}
                vr   = requests.post(validate_url, headers=hdrs, json=pl, timeout=30)
                vj   = vr.json()
                ok   = vj.get("validationResponse",{}).get("status","").lower() == "valid"
                st   = "valid" if ok else "validation_failed"
                _update_invoice_status(email, sinv_key, st)
                q.put({"sinv": sinv_key, "status": st})
            except Exception as exc:
                q.put({"sinv": sinv_key, "status": "error", "message": str(exc)})
        q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            item = q.get()
            if item is None:
                yield f"data: {json.dumps({'done': True})}\n\n"
                break
            yield f"data: {json.dumps(item)}\n\n"

    return Response(stream_with_context(generate()), content_type="text/event-stream")

# ─────────────────────────────────────────────────────────────────────────────
#  Buyers
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/buyers")
@login_required
def buyers_page():
    email  = session["email"]
    buyers = _db_get_all("buyers", order_by="business_name ASC", user_email=email)
    return render_template("buyers.html", buyers=buyers)


@app.route("/api/buyers", methods=["GET"])
@login_required
def api_buyers_list():
    email = session["email"]
    q_str = s(request.args.get("q", ""))
    with _db() as conn:
        if q_str:
            rows = conn.execute(
                """SELECT * FROM buyers WHERE user_email=?
                   AND (ntn_cnic LIKE ? OR business_name LIKE ?)
                   ORDER BY business_name""",
                [email, f"%{q_str}%", f"%{q_str}%"]
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM buyers WHERE user_email=? ORDER BY business_name",
                [email]
            ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/buyers/save", methods=["POST"])
@login_required
def api_buyers_save():
    email = session["email"]
    d     = request.get_json(silent=True) or {}
    ntn   = s(d.get("ntn_cnic", ""))
    if not ntn:
        return jsonify({"ok": False, "error": "NTN/CNIC is required."}), 400

    _db_upsert("buyers", {
        "id":            str(uuid.uuid4()),
        "user_email":    email,
        "ntn_cnic":      ntn,
        "business_name": s(d.get("business_name", "")),
        "province":      s(d.get("province", "")),
        "address":       s(d.get("address", "")),
        "reg_type":      s(d.get("reg_type", "Registered")),
        "strn":          s(d.get("strn", "")),
        "created_at":    _utcnow().isoformat(),
    }, conflict_col="id")
    return jsonify({"ok": True})


@app.route("/api/buyers/delete/<ntn>", methods=["POST"])
@login_required
def api_buyers_delete(ntn):
    email    = session["email"]
    safe_ntn = s(ntn)
    with _db() as conn:
        conn.execute(
            "DELETE FROM buyers WHERE user_email=? AND ntn_cnic=?",
            [email, safe_ntn]
        )
    return jsonify({"ok": True})

# ─────────────────────────────────────────────────────────────────────────────
#  Export & Reports
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/export/excel")
@login_required
def export_invoices_excel():
    email   = session["email"]
    rows    = _db_get_all("invoices", user_email=email)
    records = []
    for r in rows:
        raw = {}
        try:
            raw = json.loads(r.get("payload") or "{}")
        except Exception:
            pass
        records.append({
            "SINV":            _sinv_display_key(r["sinv"]),
            "Invoice Date":    r.get("invoice_date", ""),
            "Invoice Type":    r.get("invoice_type", ""),
            "Status":          r.get("status", ""),
            "FBR Invoice No":  r.get("fbr_invoice_no", ""),
            "Buyer":           raw.get("buyerBusinessName", ""),
            "Buyer NTN":       raw.get("buyerNTNCNIC", ""),
            "Items Count":     len(raw.get("items", [])),
        })

    df       = pd.DataFrame(records)
    tmp_path = os.path.join(
        BACKUP_DIR,
        f"invoices_export_{_utcnow().strftime('%Y%m%d%H%M%S')}.xlsx"
    )

    with pd.ExcelWriter(tmp_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Invoices")
        ws = writer.sheets["Invoices"]
        # Bug-fix #11: renamed loop variable ws_col (was col, shadowing outer var)
        for ws_col in ws.columns:
            max_len = max((len(str(cell.value or "")) for cell in ws_col), default=10)
            ws.column_dimensions[ws_col[0].column_letter].width = min(max_len + 4, 60)

    return send_file(
        tmp_path, as_attachment=True,
        download_name=f"invoices_{_utcnow().strftime('%Y%m%d')}.xlsx"
    )


@app.route("/report/monthly/<int:year>/<int:month>")
@login_required
def report_monthly_pdf(year, month):
    email = session["email"]
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM invoices
               WHERE user_email=? AND invoice_date LIKE ?
               ORDER BY invoice_date""",
            [email, f"{year}-{month:02d}-%"]
        ).fetchall()
    rows = [dict(r) for r in rows]

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 12, f"Monthly Report — {year}/{month:02d}", 0, 1, "C")
    pdf.ln(2)
    pdf.set_font("Arial", "B", 9)
    pdf.set_fill_color(220, 220, 220)
    col_w = [40, 25, 30, 50, 45]
    hdrs  = ["SINV", "Date", "Status", "Buyer", "FBR Invoice No"]
    for i, h in enumerate(hdrs):
        pdf.cell(col_w[i], 8, h, 1, 0, "C", True)
    pdf.ln()
    pdf.set_font("Arial", "", 8)
    for r in rows:
        raw = {}
        try:
            raw = json.loads(r.get("payload") or "{}")
        except Exception:
            pass
        buyer = s(raw.get("buyerBusinessName", "")) or "(unregistered)"
        # Bug-fix #12: no longer skips invoices with empty buyer name
        # Bug-fix #12: renamed inner loop var (no shadowing issue in PDF cols)
        pdf.cell(40, 7, _sinv_display_key(r["sinv"])[:18], 1)
        pdf.cell(25, 7, r.get("invoice_date","")[:10], 1)
        pdf.cell(30, 7, r.get("status","")[:14], 1)
        pdf.cell(50, 7, buyer[:22], 1)
        pdf.cell(45, 7, r.get("fbr_invoice_no","")[:20], 1)
        pdf.ln()

    tmp = os.path.join(
        BACKUP_DIR,
        f"report_{year}_{month:02d}_{_utcnow().strftime('%Y%m%d%H%M%S')}.pdf"
    )
    pdf.output(tmp)
    return send_file(
        tmp, as_attachment=True,
        download_name=f"report_{year}_{month:02d}.pdf"
    )

# ─────────────────────────────────────────────────────────────────────────────
#  Downloads
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/pdf/<filename>")
@login_required
def download_invoice_pdf(filename):
    # Bug-fix #15: sanitize filename; use safe in DB ownership check too
    safe  = secure_filename(filename)
    email = session["email"]
    with _db() as conn:
        row = conn.execute(
            # Bug-fix #15: query uses safe (sanitized) not raw filename
            "SELECT id FROM invoices WHERE user_email=? AND pdf_path=?",
            [email, safe]
        ).fetchone()
    if not row and not session.get("is_super_admin"):
        return "Not found or access denied.", 404
    path = os.path.join(PDF_DIR, safe)
    if not os.path.exists(path):
        return "PDF file not found on disk.", 404
    return send_file(path, as_attachment=True, download_name=safe)

# ─────────────────────────────────────────────────────────────────────────────
#  Admin panel
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/admin")
@admin_required
def admin_panel():
    with _db() as conn:
        users  = [dict(u) for u in conn.execute(
            "SELECT * FROM users ORDER BY email").fetchall()]
        total  = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        posted = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE status='posted'").fetchone()[0]
    return render_template("admin.html", users=users,
                           total_invoices=total, posted_invoices=posted)


@app.route("/admin/scheduler-status")
@admin_required
def admin_scheduler_status():
    # Bug-fix #4: BACKUP_DIR defined at top of file — no NameError
    try:
        backups = sorted(os.listdir(BACKUP_DIR))
    except Exception:
        backups = []
    return jsonify({
        "backup_dir":     BACKUP_DIR,
        "backup_count":   len(backups),
        "latest_backup":  backups[-1] if backups else None,
        "worker_alive":   _bg_thread.is_alive() if _bg_thread else False,
    })


@app.route("/admin/delete-user/<path:target_email>", methods=["POST"])
@admin_required
def admin_delete_user(target_email):
    safe_email = s(target_email).lower()
    if safe_email == SUPER_ADMIN_EMAIL.lower():
        flash("Cannot delete super admin.", "error")
        return redirect(url_for("admin_panel"))
    with _db() as conn:
        conn.execute("DELETE FROM users    WHERE email=?",      [safe_email])
        conn.execute("DELETE FROM invoices WHERE user_email=?", [safe_email])
        conn.execute("DELETE FROM buyers   WHERE user_email=?", [safe_email])
    flash(f"User {safe_email} and all their data have been deleted.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/view-as/<path:target_email>")
@admin_required
def admin_view_as(target_email):
    safe_email = s(target_email).lower()
    user = _db_get("users", email=safe_email)
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("admin_panel"))
    # Bug-fix (admin_view_as): preserve is_super_admin and admin email
    session["admin_email"]    = session["email"]
    session["email"]          = safe_email
    session["seller_name"]    = user.get("seller_name", "")
    session["is_super_admin"] = True   # keep admin powers while viewing
    flash(f"Viewing as {safe_email}. Click 'Back to Admin' to return.", "info")
    return redirect(url_for("dashboard"))


@app.route("/admin/back")
def admin_back():
    admin_email = session.pop("admin_email", None)
    if not admin_email or not session.get("is_super_admin"):
        return redirect(url_for("dashboard"))
    session["email"]       = admin_email
    session["seller_name"] = "Super Admin"
    flash("Returned to admin view.", "info")
    return redirect(url_for("admin_panel"))

# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers (used by multiple routes)
# ─────────────────────────────────────────────────────────────────────────────
def _update_invoice_status(email, sinv_key, status, fbr_no="", pdf_file=""):
    with _db() as conn:
        conn.execute(
            """UPDATE invoices
               SET status=?, fbr_invoice_no=?, pdf_path=?, updated_at=?
               WHERE user_email=? AND sinv=?""",
            [status, fbr_no, pdf_file, _utcnow().isoformat(), email, sinv_key]
        )


def _generate_pdf(sinv_key: str, fbr_inv_no: str, payload: dict) -> str:
    """Generate a PDF invoice and return the filename (empty on failure)."""
    sinv_display = payload.get("_sinv_display") or _sinv_display_key(sinv_key)
    try:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", "B", 16)
        pdf.cell(0, 12, "FBR Digital Invoice", 0, 1, "C")
        pdf.set_font("Arial", "", 10)
        pdf.ln(2)
        for label, value in [
            ("FBR Invoice No:", fbr_inv_no),
            ("SINV:",           sinv_display),
            ("Invoice Date:",   payload.get("invoiceDate", "")),
            ("Invoice Type:",   payload.get("invoiceType", "")),
            ("Seller:",         payload.get("sellerBusinessName", "")),
            ("Seller NTN:",     payload.get("sellerNTNCNIC", "")),
            ("Buyer:",          payload.get("buyerBusinessName", "")),
            ("Buyer NTN:",      payload.get("buyerNTNCNIC", "")),
        ]:
            pdf.cell(50, 7, label, 0)
            pdf.cell(0, 7, str(value), 0, 1)
        pdf.ln(3)
        pdf.set_font("Arial", "B", 9)
        pdf.set_fill_color(230, 230, 230)
        col_w = [55, 12, 18, 30, 30, 30]
        for cw, ch in zip(col_w, ["Description","UoM","Qty","Value Excl ST","Sales Tax","Rate"]):
            pdf.cell(cw, 7, ch, 1, 0, "C", True)
        pdf.ln()
        pdf.set_font("Arial", "", 8)
        for item in payload.get("items", []):
            pdf.cell(55, 6, str(item.get("productDescription",""))[:30], 1)
            pdf.cell(12, 6, str(item.get("uoM","")), 1, 0, "C")
            pdf.cell(18, 6, str(item.get("quantity","")), 1, 0, "R")
            pdf.cell(30, 6, str(item.get("valueSalesExcludingST","")), 1, 0, "R")
            pdf.cell(30, 6, str(item.get("salesTaxApplicable","")), 1, 0, "R")
            pdf.cell(30, 6, str(item.get("rate","")), 1, 0, "C")
            pdf.ln()
        pdf_file = f"{sinv_display}_{fbr_inv_no}.pdf"
        pdf.output(os.path.join(PDF_DIR, pdf_file))
        return pdf_file
    except Exception as exc:
        app.logger.warning("_generate_pdf failed: %s", exc)
        return ""

# ─────────────────────────────────────────────────────────────────────────────
#  Background worker  (Bug-fix #14: last_cleanup_day initialised before loop)
# ─────────────────────────────────────────────────────────────────────────────
def _background_worker():
    last_cleanup_day = None   # Bug-fix #14: explicit None before while loop
    while True:
        try:
            today = _utcnow().date()
            if last_cleanup_day != today:
                last_cleanup_day = today
                # Daily DB backup
                try:
                    bk_name = f"invoices_backup_{today.strftime('%Y%m%d')}.db"
                    shutil.copy2(DB_PATH, os.path.join(BACKUP_DIR, bk_name))
                    # Keep only last 30 backups
                    all_bk = sorted(
                        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")],
                        reverse=True
                    )
                    for old in all_bk[30:]:
                        try:
                            os.remove(os.path.join(BACKUP_DIR, old))
                        except Exception:
                            pass
                except Exception as bk_err:
                    app.logger.warning("Daily backup failed: %s", bk_err)
                # Prune old response dumps (keep 7 days)
                try:
                    cutoff = _utcnow().timestamp() - 7 * 86400
                    for fn in os.listdir(RESP_DIR):
                        fp = os.path.join(RESP_DIR, fn)
                        if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                            os.remove(fp)
                except Exception:
                    pass
        except Exception as exc:
            app.logger.error("Background worker error: %s", exc)

        threading.Event().wait(3600)   # sleep 1 hour


_bg_thread = threading.Thread(target=_background_worker, daemon=True)
_bg_thread.start()

# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Starting FBR Invoice App …")
    print(f"  DB:         {DB_PATH}")
    print(f"  PDF dir:    {PDF_DIR}")
    print(f"  Backup dir: {BACKUP_DIR}")
    print(f"  Admin:      {SUPER_ADMIN_EMAIL}")
    # debug mode only when FLASK_DEBUG env var is explicitly set
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_mode, host="0.0.0.0", port=5000)
