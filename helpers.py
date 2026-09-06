"""ChangeSlip helpers: env, db, money, mail, blurbs."""
from __future__ import annotations

import json
import os
import sqlite3
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from flask import g, request

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = APP_ROOT / "changeslip.db"

OPEN_ENDPOINTS = {
    "health",
    "login",
    "logout",
    "public_slip",
    "accept_slip",
    "decline_slip",
    "receipt",
    "static",
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def database_path() -> str:
    raw = _env("DATABASE_PATH")
    return raw if raw else str(DEFAULT_DB)


def smtp_configured() -> bool:
    return bool(_env("SMTP_HOST"))


def owner_password() -> str:
    return os.environ.get("OWNER_PASSWORD", "").strip()


def public_base_url() -> str:
    return _env("PUBLIC_BASE_URL").rstrip("/")


def default_currency() -> str:
    return _env("CURRENCY") or "USD"


def require_typed_name() -> bool:
    raw = (_env("REQUIRE_TYPED_NAME") or "false").lower()
    return raw in {"1", "true", "yes", "on"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    return to_iso(utc_now())


def format_money(amount_cents: int, currency: str) -> str:
    currency = (currency or "USD").upper()
    dollars = amount_cents / 100.0
    if currency == "USD":
        return f"${dollars:,.2f}"
    return f"{dollars:,.2f} {currency}"


def public_slip_url(token: str) -> str:
    base = public_base_url() or "http://localhost:8080"
    return f"{base}/c/{token}"


def connect_db() -> sqlite3.Connection:
    path = database_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def get_db() -> sqlite3.Connection:
    db = getattr(g, "_db", None)
    if db is None:
        db = connect_db()
        g._db = db
    return db


def close_db(_exc: BaseException | None = None) -> None:
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS slips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            customer_name TEXT NOT NULL,
            customer_email TEXT,
            customer_phone TEXT,
            job_ref TEXT,
            summary TEXT NOT NULL,
            details TEXT,
            amount_cents INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            accepted_at TEXT,
            declined_at TEXT,
            voided_at TEXT,
            accepted_ip TEXT,
            accepted_user_agent TEXT,
            accepted_typed_name TEXT,
            declined_ip TEXT,
            declined_user_agent TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slip_id INTEGER NOT NULL REFERENCES slips(id),
            kind TEXT NOT NULL,
            at TEXT NOT NULL,
            meta_json TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_slips_token ON slips(token);
        CREATE INDEX IF NOT EXISTS idx_slips_created_at ON slips(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_slips_status ON slips(status);
        CREATE INDEX IF NOT EXISTS idx_events_slip_id ON events(slip_id);
        """
    )
    db.commit()


def get_slip(slip_id: int):
    return get_db().execute("SELECT * FROM slips WHERE id = ?", (slip_id,)).fetchone()


def get_slip_by_token(token: str):
    return get_db().execute("SELECT * FROM slips WHERE token = ?", (token,)).fetchone()


def list_events(slip_id: int):
    return get_db().execute(
        "SELECT * FROM events WHERE slip_id = ? ORDER BY id ASC", (slip_id,)
    ).fetchall()


def add_event(conn, slip_id: int, kind: str, meta=None, at=None) -> None:
    conn.execute(
        "INSERT INTO events (slip_id, kind, at, meta_json) VALUES (?, ?, ?, ?)",
        (slip_id, kind, at or utc_now_iso(), json.dumps(meta) if meta is not None else None),
    )


def client_ip() -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or (request.remote_addr or "")


def client_ua() -> str:
    return (request.headers.get("User-Agent") or "")[:500]


def send_smtp(to_email: str, subject: str, body: str) -> None:
    host = _env("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP is not configured.")
    from_email = _env("FROM_EMAIL")
    if not from_email:
        raise RuntimeError("FROM_EMAIL is required to send mail.")
    port = int(_env("SMTP_PORT") or "587")
    user = _env("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD", "")
    tls_raw = _env("SMTP_TLS") or "true"
    use_tls = tls_raw.lower() in {"1", "true", "yes", "on"}
    from_name = _env("FROM_NAME")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = to_email
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def sms_blurb(slip) -> str:
    business = _env("BUSINESS_NAME") or "us"
    amount = format_money(slip["amount_cents"], slip["currency"])
    link = public_slip_url(slip["token"])
    return (
        f"Hi {slip['customer_name']}, {business} needs your OK on a change: "
        f"{slip['summary']} for {amount}. Accept or decline here: {link}"
    )


def email_blurb(slip) -> str:
    business = _env("BUSINESS_NAME") or "us"
    amount = format_money(slip["amount_cents"], slip["currency"])
    link = public_slip_url(slip["token"])
    sign_name = _env("FROM_NAME") or business
    body = (
        f"Hi {slip['customer_name']},\n\n"
        f"{business} recorded a mid-job change and needs your Accept before continuing.\n\n"
        f"Change: {slip['summary']}\n"
        f"Amount: {amount}\n"
    )
    if slip["job_ref"]:
        body += f"Job / WO: {slip['job_ref']}\n"
    if slip["details"]:
        body += f"\nDetails:\n{slip['details']}\n"
    body += f"\nReview and Accept or Decline:\n{link}\n\nThank you,\n{sign_name}\n"
    return body


def email_subject(slip) -> str:
    business = _env("BUSINESS_NAME") or "Change order"
    return f"Please confirm change order — {business}"


def notify_owner(app, slip, kind: str) -> None:
    to_email = _env("OWNER_NOTIFY_EMAIL")
    if not (smtp_configured() and to_email):
        return
    amount = format_money(slip["amount_cents"], slip["currency"])
    base = public_base_url() or "http://localhost:8080"
    subject = f"ChangeSlip {kind}: {slip['customer_name']} — {amount}"
    body = (
        f"Status: {kind}\nCustomer: {slip['customer_name']}\n"
        f"Summary: {slip['summary']}\nAmount: {amount}\n\n"
        f"Open in ChangeSlip: {base}/slips/{slip['id']}\n"
    )
    try:
        send_smtp(to_email, subject, body)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("Owner notify failed for slip_id=%s: %s", slip["id"], type(exc).__name__)
