"""Local Flask test client covering PRD §12 as much as possible without Compose."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

# Ensure env before importing app
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["OWNER_PASSWORD"] = "testpass"
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
os.environ["BUSINESS_NAME"] = "Harbor HVAC"
os.environ["CURRENCY"] = "USD"
os.environ["REQUIRE_TYPED_NAME"] = "false"
os.environ["MARKETING_URL"] = ""
os.environ.pop("SMTP_HOST", None)
os.environ.pop("OWNER_NOTIFY_EMAIL", None)
os.environ.pop("FROM_NAME", None)
os.environ.pop("FROM_EMAIL", None)


class ChangeSlipTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmpdir.name) / "test.db")
        os.environ["DATABASE_PATH"] = db_path
        os.environ["OWNER_PASSWORD"] = "testpass"
        os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
        os.environ["BUSINESS_NAME"] = "Harbor HVAC"
        os.environ["CURRENCY"] = "USD"
        os.environ["REQUIRE_TYPED_NAME"] = "false"
        os.environ["MARKETING_URL"] = ""
        os.environ.pop("SMTP_HOST", None)
os.environ.pop("OWNER_NOTIFY_EMAIL", None)

        import importlib
        import app as app_module

        importlib.reload(app_module)
        self.app_module = app_module
        self.app = app_module.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        with self.app.app_context():
            app_module.init_db()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _login(self) -> None:
        r = self.client.post(
            "/login",
            data={"password": "testpass", "next": "/"},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))

    def _create_slip(
        self,
        summary: str = "Add whole-house surge protector",
        amount_cents: int = 25000,
        customer_name: str = "Jordan Lee",
    ) -> tuple[int, str]:
        self._login()
        r = self.client.post(
            "/slips/new",
            data={
                "customer_name": customer_name,
                "customer_email": "jordan.lee@example.com",
                "customer_phone": "+1-555-0142",
                "job_ref": "WO-4821",
                "summary": summary,
                "details": "Mid-install add-on at the panel.",
                "amount_cents": str(amount_cents),
                "currency": "USD",
            },
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        loc = r.headers.get("Location", "")
        self.assertIn("/slips/", loc)
        slip_id = int(loc.rstrip("/").split("/")[-1])
        with self.app.app_context():
            slip = self.app_module.get_slip(slip_id)
            self.assertIsNotNone(slip)
            return slip_id, slip["token"]

    def test_health(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIs(data["smtp_configured"], False)

    def test_auth_gate(self) -> None:
        r = self.client.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.assertIn("/login", r.headers.get("Location", ""))

        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)

        slip_id, token = self._create_slip()
        self.client.get("/logout")
        r = self.client.get(f"/c/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Add whole-house surge protector", r.data)

        r = self.client.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_create_accept_receipt(self) -> None:
        slip_id, token = self._create_slip()

        r = self.client.get(f"/slips/{slip_id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"/c/{token}".encode(), r.data)
        self.assertIn(b"$250.00", r.data)

        r = self.client.get(f"/c/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Add whole-house surge protector", r.data)
        self.assertIn(b"$250.00", r.data)
        self.assertIn(b"Accept", r.data)
        self.assertIn(b"Decline", r.data)

        r = self.client.post(f"/c/{token}/accept", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Accepted", r.data)
        self.assertIn(b"Accepted by customer via magic link", r.data)

        self._login()
        r = self.client.get(f"/slips/{slip_id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"accepted", r.data)
        self.assertIn(b"created", r.data)
        body = r.data.decode("utf-8")
        self.assertIn("created", body)
        self.assertIn("accepted", body)

        r = self.client.get(f"/c/{token}/receipt")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"$250.00", r.data)
        with self.app.app_context():
            slip = self.app_module.get_slip(slip_id)
            self.assertEqual(slip["status"], "accepted")
            self.assertIsNotNone(slip["accepted_at"])
            self.assertIn(slip["accepted_at"].encode(), r.data)

    def test_accept_idempotent(self) -> None:
        slip_id, token = self._create_slip(summary="Idempotent accept test")
        self.client.post(f"/c/{token}/accept")
        self.client.post(f"/c/{token}/accept")
        with self.app.app_context():
            db = self.app_module.get_db()
            n = db.execute(
                "SELECT COUNT(*) AS c FROM events WHERE slip_id = ? AND kind = 'accepted'",
                (slip_id,),
            ).fetchone()["c"]
            self.assertEqual(n, 1)

    def test_decline(self) -> None:
        slip_id, token = self._create_slip(summary="Decline path test")
        r = self.client.post(f"/c/{token}/decline")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Declined", r.data)
        self.assertNotIn(b'action="/c/' + token.encode() + b'/accept"', r.data)

        with self.app.app_context():
            slip = self.app_module.get_slip(slip_id)
            self.assertEqual(slip["status"], "declined")
            db = self.app_module.get_db()
            n = db.execute(
                "SELECT COUNT(*) AS c FROM events WHERE slip_id = ? AND kind = 'declined'",
                (slip_id,),
            ).fetchone()["c"]
            self.assertEqual(n, 1)

        r = self.client.get(f"/c/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b">Accept</button>", r.data)

    def test_void(self) -> None:
        slip_id, token = self._create_slip(summary="Void path test")
        self._login()
        r = self.client.post(f"/slips/{slip_id}/void", follow_redirects=True)
        self.assertEqual(r.status_code, 200)

        r = self.client.get(f"/c/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"voided by the business", r.data)

        r = self.client.post(f"/c/{token}/accept")
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            slip = self.app_module.get_slip(slip_id)
            self.assertEqual(slip["status"], "void")
            db = self.app_module.get_db()
            n = db.execute(
                "SELECT COUNT(*) AS c FROM events WHERE slip_id = ? AND kind = 'accepted'",
                (slip_id,),
            ).fetchone()["c"]
            self.assertEqual(n, 0)

        slip_id2, token2 = self._create_slip(summary="Cannot void accepted")
        self.client.post(f"/c/{token2}/accept")
        self._login()
        r = self.client.post(f"/slips/{slip_id2}/void", follow_redirects=True)
        self.assertIn(b"cannot be voided", r.data.lower())
        with self.app.app_context():
            self.assertEqual(self.app_module.get_slip(slip_id2)["status"], "accepted")

    def test_no_payment_sms_drip(self) -> None:
        src = Path(self.app_module.__file__).read_text()
        for banned in ("stripe", "twilio", "SEQUENCE", "drip", "celery", "redis"):
            self.assertNotIn(banned.lower() if banned.islower() else banned, src.lower() if banned.islower() else src)
        with self.app.app_context():
            db = self.app_module.get_db()
            tables = {
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            tables.discard("sqlite_sequence")
            self.assertEqual(tables, {"slips", "events"})

    def test_no_powered_by_footer(self) -> None:
        self._login()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"Powered by ChangeSlip", r.data)

        r = self.client.get("/c/" + ("a" * 64))
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
