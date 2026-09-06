"""ChangeSlip: mid-job change-order Accept via magic link."""

from __future__ import annotations

import secrets

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import helpers as H

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
app.secret_key = __import__("os").environ.get("SECRET_KEY", "changeslip-self-hosted-change-me")

app.teardown_appcontext(H.close_db)


def init_db() -> None:
    with app.app_context():
        H.init_schema(H.get_db())


@app.context_processor
def inject_globals() -> dict:
    return {
        "marketing_url": H._env("MARKETING_URL"),
        "smtp_configured": H.smtp_configured(),
        "business_name": H._env("BUSINESS_NAME") or "ChangeSlip",
        "owner_locked": bool(H.owner_password()),
        "logged_in": bool(session.get("owner")) or not H.owner_password(),
        "format_money": H.format_money,
        "require_typed_name": H.require_typed_name(),
    }


@app.before_request
def protect_owner_routes():
    if request.endpoint in H.OPEN_ENDPOINTS or request.endpoint is None:
        return None
    if not H.owner_password():
        return None
    if session.get("owner"):
        return None
    nxt = request.path if request.method == "GET" else "/"
    return redirect(url_for("login", next=nxt))


def _safe_next(val: str | None) -> str:
    raw = (val or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("index")


@app.get("/health")
def health():
    return jsonify({"status": "ok", "smtp_configured": H.smtp_configured()})


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.values.get("next"))
    if not H.owner_password():
        return redirect(nxt)
    if session.get("owner"):
        return redirect(nxt)
    error = None
    if request.method == "POST":
        provided = (request.form.get("password") or "").encode("utf-8")
        expected = H.owner_password().encode("utf-8")
        ok = len(provided) == len(expected) and secrets.compare_digest(provided, expected)
        if ok:
            session["owner"] = True
            return redirect(nxt)
        error = "Incorrect password."
    return render_template("login.html", next=nxt, error=error, public=True)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    rows = H.get_db().execute(
        "SELECT * FROM slips ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return render_template("index.html", slips=rows)


@app.route("/slips/new", methods=["GET", "POST"])
def new_slip():
    if request.method == "GET":
        return render_template("new_slip.html", default_currency=H.default_currency())

    customer_name = (request.form.get("customer_name") or "").strip()
    customer_email = (request.form.get("customer_email") or "").strip() or None
    customer_phone = (request.form.get("customer_phone") or "").strip() or None
    job_ref = (request.form.get("job_ref") or "").strip() or None
    summary = (request.form.get("summary") or "").strip()
    details = (request.form.get("details") or "").strip() or None
    currency = (request.form.get("currency") or H.default_currency()).strip().upper() or H.default_currency()
    amount_raw = (request.form.get("amount_cents") or "").strip()

    errors: list[str] = []
    if not customer_name:
        errors.append("Customer name is required.")
    if not summary:
        errors.append("Summary is required.")
    amount_cents: int | None = None
    try:
        amount_cents = int(amount_raw)
        if amount_cents < 0:
            errors.append("Amount must be an integer ≥ 0 (cents).")
    except ValueError:
        errors.append("Amount must be an integer ≥ 0 (cents).")

    if errors:
        for e in errors:
            flash(e, "error")
        return render_template(
            "new_slip.html",
            default_currency=currency or H.default_currency(),
            form=request.form,
        ), 400

    token = secrets.token_hex(32)
    now = H.utc_now_iso()
    db = H.get_db()
    cur = db.execute(
        """
        INSERT INTO slips (
            token, customer_name, customer_email, customer_phone, job_ref,
            summary, details, amount_cents, currency, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            token, customer_name, customer_email, customer_phone, job_ref,
            summary, details, amount_cents, currency, now,
        ),
    )
    slip_id = cur.lastrowid
    H.add_event(db, slip_id, "created")
    db.commit()
    flash("Change slip created.", "ok")
    return redirect(url_for("slip_detail", slip_id=slip_id))


@app.get("/slips/<int:slip_id>")
def slip_detail(slip_id: int):
    slip = H.get_slip(slip_id)
    if slip is None:
        abort(404)
    return render_template(
        "slip_detail.html",
        slip=slip,
        events=H.list_events(slip_id),
        public_url=H.public_slip_url(slip["token"]),
        sms_text=H.sms_blurb(slip),
        email_text=H.email_blurb(slip),
        email_subject=H.email_subject(slip),
    )


@app.post("/slips/<int:slip_id>/email-link")
def email_link(slip_id: int):
    slip = H.get_slip(slip_id)
    if slip is None:
        abort(404)
    if not H.smtp_configured():
        flash("SMTP is not configured.", "error")
        return redirect(url_for("slip_detail", slip_id=slip_id))
    email = (slip["customer_email"] or "").strip()
    if not email:
        flash("This slip has no customer email.", "error")
        return redirect(url_for("slip_detail", slip_id=slip_id))
    try:
        H.send_smtp(email, H.email_subject(slip), H.email_blurb(slip))
        db = H.get_db()
        H.add_event(db, slip_id, "link_emailed", {"to": email})
        db.commit()
        flash("Link emailed to customer.", "ok")
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("Email link failed for slip_id=%s: %s", slip_id, type(exc).__name__)
        flash("Could not send email. Check SMTP settings.", "error")
    return redirect(url_for("slip_detail", slip_id=slip_id))


@app.post("/slips/<int:slip_id>/void")
def void_slip(slip_id: int):
    slip = H.get_slip(slip_id)
    if slip is None:
        abort(404)
    if slip["status"] != "pending":
        if slip["status"] == "accepted":
            flash("Accepted slips cannot be voided.", "error")
        else:
            flash(f"Cannot void a slip with status {slip['status']}.", "error")
        return redirect(url_for("slip_detail", slip_id=slip_id))

    now = H.utc_now_iso()
    db = H.get_db()
    cur = db.execute(
        "UPDATE slips SET status = 'void', voided_at = ? WHERE id = ? AND status = 'pending'",
        (now, slip_id),
    )
    if cur.rowcount:
        H.add_event(db, slip_id, "voided")
        db.commit()
        flash("Change slip voided.", "ok")
    else:
        db.rollback()
        flash("Slip was no longer pending.", "error")
    return redirect(url_for("slip_detail", slip_id=slip_id))


@app.get("/c/<token>")
def public_slip(token: str):
    slip = H.get_slip_by_token(token)
    if slip is None:
        abort(404)
    return render_template(
        "public_slip.html",
        slip=slip,
        public=True,
        typed_name_required=H.require_typed_name(),
    )


@app.post("/c/<token>/accept")
def accept_slip(token: str):
    slip = H.get_slip_by_token(token)
    if slip is None:
        abort(404)

    if slip["status"] != "pending":
        return render_template(
            "public_slip.html",
            slip=slip,
            public=True,
            typed_name_required=H.require_typed_name(),
        )

    typed_name = (request.form.get("typed_name") or "").strip()
    if H.require_typed_name():
        expected = (slip["customer_name"] or "").strip().lower()
        if typed_name.lower() != expected:
            flash("Please type your full name exactly as shown to Accept.", "error")
            return render_template(
                "public_slip.html",
                slip=slip,
                public=True,
                typed_name_required=True,
            ), 400

    now = H.utc_now_iso()
    ip = H.client_ip()
    ua = H.client_ua()
    db = H.get_db()
    cur = db.execute(
        """
        UPDATE slips SET
            status = 'accepted', accepted_at = ?, accepted_ip = ?,
            accepted_user_agent = ?, accepted_typed_name = ?
        WHERE id = ? AND status = 'pending'
        """,
        (now, ip, ua, typed_name or None, slip["id"]),
    )
    if cur.rowcount:
        H.add_event(
            db, slip["id"], "accepted",
            {"ip": ip, "ua": ua, "typed_name": typed_name or None}, at=now,
        )
        db.commit()
        slip = H.get_slip_by_token(token)
        H.notify_owner(app, slip, "accepted")
    else:
        db.rollback()
        slip = H.get_slip_by_token(token)

    return render_template(
        "public_slip.html",
        slip=slip,
        public=True,
        typed_name_required=H.require_typed_name(),
    )


@app.post("/c/<token>/decline")
def decline_slip(token: str):
    slip = H.get_slip_by_token(token)
    if slip is None:
        abort(404)

    if slip["status"] != "pending":
        return render_template(
            "public_slip.html",
            slip=slip,
            public=True,
            typed_name_required=H.require_typed_name(),
        )

    now = H.utc_now_iso()
    ip = H.client_ip()
    ua = H.client_ua()
    db = H.get_db()
    cur = db.execute(
        """
        UPDATE slips SET
            status = 'declined', declined_at = ?,
            declined_ip = ?, declined_user_agent = ?
        WHERE id = ? AND status = 'pending'
        """,
        (now, ip, ua, slip["id"]),
    )
    if cur.rowcount:
        H.add_event(db, slip["id"], "declined", {"ip": ip, "ua": ua}, at=now)
        db.commit()
        slip = H.get_slip_by_token(token)
        H.notify_owner(app, slip, "declined")
    else:
        db.rollback()
        slip = H.get_slip_by_token(token)

    return render_template(
        "public_slip.html",
        slip=slip,
        public=True,
        typed_name_required=H.require_typed_name(),
    )


@app.get("/c/<token>/receipt")
def receipt(token: str):
    slip = H.get_slip_by_token(token)
    if slip is None:
        abort(404)
    if slip["status"] != "accepted":
        return (
            render_template("receipt.html", slip=slip, public=True, pending_only=True),
            404 if slip["status"] in ("void", "declined") else 200,
        )
    return render_template(
        "receipt.html",
        slip=slip,
        public=True,
        pending_only=False,
        token_suffix=slip["token"][-8:],
    )


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html", public=True), 404


# Re-exports for tests
get_slip = H.get_slip
get_slip_by_token = H.get_slip_by_token
get_db = H.get_db

init_db()


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
