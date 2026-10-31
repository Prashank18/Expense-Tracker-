import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["EXPENSE_DB_PATH"] = os.path.join(TEST_DATA.name, "test-ledger.sqlite3")

import server  # noqa: E402


class LedgerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.init_db()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.account_number = 0

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        TEST_DATA.cleanup()

    def setUp(self):
        type(self).account_number += 1
        self.cookie = ""
        self.csrf = ""
        self.email = f"tester{self.account_number}@example.test"
        result, status, headers = self.request(
            "/api/register",
            {
                "name": "Ledger Tester",
                "email": self.email,
                "password": "a-long-test-password",
            },
            method="POST",
        )
        self.assertEqual(status, 200)
        self.csrf = result["csrf"]
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]

    def request(self, path, payload=None, method="GET"):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        request = Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        return json.loads(response.read()), response.status, response.headers

    def add_transaction(self, kind, amount, category, payee):
        return self.request(
            "/api/transactions",
            {
                "kind": kind,
                "amount": amount,
                "category": category,
                "payee": payee,
                "date": server.date.today().isoformat(),
            },
            method="POST",
        )

    def test_dashboard_assets_are_served(self):
        assets = (
            ("/", b"Expense Tracker"),
            ("/app.css", b"--forest"),
            ("/app.js", b"renderOverview"),
            ("/supabase-config.js", b"EXPENSE_TRACKER_SUPABASE"),
        )
        for path, expected in assets:
            with urlopen(self.base_url + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(expected, response.read())

    def test_registered_user_can_sign_in(self):
        self.cookie = ""
        self.csrf = ""
        result, status, headers = self.request(
            "/api/login",
            {"email": self.email, "password": "a-long-test-password"},
            method="POST",
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["csrf"])
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = result["csrf"]
        profile, status, _ = self.request("/api/me")
        self.assertEqual(status, 200)
        self.assertEqual(profile["user"]["email"], self.email)

    def test_entries_budget_chart_and_alert_are_persisted(self):
        settings, status, _ = self.request(
            "/api/settings",
            {"whatsapp_phone": "+14155550123", "whatsapp_opt_in": True},
            method="POST",
        )
        self.assertEqual(status, 200)
        self.assertTrue(settings["ok"])

        with patch.object(server, "send_whatsapp", return_value=("sent", "test delivery")) as send:
            _, status, _ = self.request(
                "/api/budgets",
                {"category": "Food & Dining", "limit": "1000"},
                method="POST",
            )
            self.assertEqual(status, 201)
            _, status, _ = self.add_transaction("debit", "999.99", "Food & Dining", "Groceries")
            self.assertEqual(status, 201)
            self.assertEqual(send.call_count, 0)
            _, status, _ = self.add_transaction("credit", "5000", "Income", "Payday")
            self.assertEqual(status, 201)
            _, status, _ = self.add_transaction("debit", "0.51", "Food & Dining", "Market")
            self.assertEqual(status, 201)
            self.assertEqual(send.call_count, 1)
            _, status, _ = self.add_transaction("debit", "20", "Food & Dining", "Coffee")
            self.assertEqual(status, 201)
            self.assertEqual(send.call_count, 1)
            send.assert_called_once_with("+14155550123", "Food & Dining", 1000.5, 1000.0)

        for period in ("daily", "monthly", "yearly"):
            dashboard, status, _ = self.request(f"/api/dashboard?period={period}")
            self.assertEqual(status, 200)
            self.assertEqual(dashboard["period"], period)
            self.assertEqual(dashboard["totals"]["credit"], 5000)
            self.assertEqual(dashboard["totals"]["debit"], 1020.5)
            self.assertEqual(dashboard["budgets"][0]["limit"], 1000)
            self.assertEqual(dashboard["budgets"][0]["spent"], 1020.5)

        transactions, status, _ = self.request("/api/transactions")
        self.assertEqual(status, 200)
        self.assertEqual(len(transactions["transactions"]), 4)
        with server.database() as db:
            event = db.execute("SELECT status FROM alert_events").fetchone()
            debit_total = db.execute(
                "SELECT SUM(amount_minor) FROM transactions WHERE kind = 'debit'"
            ).fetchone()[0]
        self.assertEqual(event["status"], "sent")
        self.assertEqual(debit_total, 102050)

    def test_users_cannot_read_each_others_transactions(self):
        _, status, _ = self.add_transaction("debit", "42", "Transport", "Bus fare")
        self.assertEqual(status, 201)
        first_cookie, first_csrf = self.cookie, self.csrf

        self.cookie = ""
        self.csrf = ""
        _, status, headers = self.request(
            "/api/register",
            {"name": "Another User", "email": "another@example.test", "password": "another-password"},
            method="POST",
        )
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = self.request("/api/me")[0]["csrf"]
        dashboard, status, _ = self.request("/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["totals"]["debit"], 0)
        self.assertEqual(dashboard["transactions"], [])
        self.assertNotEqual(first_cookie, self.cookie)
        self.assertNotEqual(first_csrf, self.csrf)

    def test_debit_cannot_use_income_category(self):
        result, status, _ = self.add_transaction("debit", "12", "Income", "Invalid entry")
        self.assertEqual(status, 400)
        self.assertIn("spending category", result["error"])
        result, status, _ = self.add_transaction("debit", "12.001", "Transport", "Invalid precision")
        self.assertEqual(status, 400)
        self.assertIn("two decimal places", result["error"])


if __name__ == "__main__":
    unittest.main()
