from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("EXPENSE_DB_PATH", ROOT / "data" / "ledger.sqlite3"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
SESSION_SECONDS = 60 * 60 * 24 * 30
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
CATEGORIES = [
    "Food & Dining",
    "Transport",
    "Housing",
    "Bills & Utilities",
    "Shopping",
    "Health",
    "Entertainment",
    "Education",
    "Travel",
    "Income",
    "Other",
]


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


@contextmanager
def database():
    db = connect_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    with database() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                whatsapp_phone TEXT NOT NULL DEFAULT '',
                whatsapp_opt_in INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                csrf_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('debit', 'credit')),
                amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                category TEXT NOT NULL,
                payee TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                transaction_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS transactions_user_date
                ON transactions(user_id, transaction_date DESC);
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                monthly_limit_minor INTEGER NOT NULL CHECK(monthly_limit_minor > 0),
                UNIQUE(user_id, category)
            );
            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                budget_id INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
                budget_month TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(user_id, budget_id, budget_month)
            );
            """
        )


def password_digest(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 310_000
    ).hex()


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def to_minor_units(value: object, label: str) -> int:
    try:
        amount = Decimal(str(value))
        quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Enter a valid {label}.") from None
    if not amount.is_finite() or amount != quantized:
        raise ValueError(f"{label.capitalize()} must use no more than two decimal places.")
    return int(quantized * 100)


def current_month() -> str:
    return date.today().strftime("%Y-%m")


def send_whatsapp(
    phone: str, category: str, spent: float, limit: float
) -> tuple[str, str]:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    sender = os.environ.get("TWILIO_WHATSAPP_FROM", "")
    content_sid = os.environ.get("TWILIO_WHATSAPP_CONTENT_SID", "")
    if not all((account_sid, auth_token, sender, content_sid, phone)):
        return "unconfigured", "WhatsApp sender, approved template, or recipient is not configured."

    endpoint = (
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    )
    recipient = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
    sender_address = sender if sender.startswith("whatsapp:") else f"whatsapp:{sender}"
    content_variables = json.dumps(
        {
            "1": category,
            "2": f"₹{spent:,.2f}",
            "3": f"₹{limit:,.2f}",
        }
    )
    form = urlencode(
        {
            "To": recipient,
            "From": sender_address,
            "ContentSid": content_sid,
            "ContentVariables": content_variables,
        }
    )
    request = Request(
        endpoint,
        data=form.encode("utf-8"),
        headers={
            "Authorization": "Basic "
            + __import__("base64").b64encode(
                f"{account_sid}:{auth_token}".encode("utf-8")
            ).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            if 200 <= response.status < 300:
                return "sent", "WhatsApp alert delivered."
            return "failed", f"WhatsApp provider returned HTTP {response.status}."
    except Exception as error:
        return "failed", f"WhatsApp alert could not be delivered: {error}"


def dispatch_pending_alerts(db: sqlite3.Connection, user_id: int) -> None:
    phone_row = db.execute(
        "SELECT whatsapp_phone, whatsapp_opt_in FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    phone = (
        phone_row["whatsapp_phone"]
        if phone_row and phone_row["whatsapp_opt_in"]
        else ""
    )
    period = current_month()
    budgets = db.execute(
        """
        SELECT b.id, b.category, b.monthly_limit_minor,
            COALESCE(SUM(t.amount_minor), 0) AS spent_minor
        FROM budgets b
        LEFT JOIN transactions t ON t.user_id = b.user_id
            AND t.category = b.category AND t.kind = 'debit'
            AND substr(t.transaction_date, 1, 7) = ?
        WHERE b.user_id = ?
        GROUP BY b.id
        HAVING spent_minor >= b.monthly_limit_minor
        """,
        (period, user_id),
    ).fetchall()
    for budget in budgets:
        exists = db.execute(
            """
            SELECT status FROM alert_events
            WHERE user_id = ? AND budget_id = ? AND budget_month = ?
            """,
            (user_id, budget["id"], period),
        ).fetchone()
        if exists and exists["status"] == "sent":
            continue
        if not exists:
            db.execute(
                """
                INSERT INTO alert_events
                    (user_id, budget_id, budget_month, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (user_id, budget["id"], period, datetime.now().isoformat()),
            )
        spent = budget["spent_minor"] / 100
        limit = budget["monthly_limit_minor"] / 100
        status, detail = send_whatsapp(
            phone, budget["category"], spent, limit
        )
        db.execute(
            """
            UPDATE alert_events SET status = ?, detail = ?
            WHERE user_id = ? AND budget_id = ? AND budget_month = ?
            """,
            (status, detail, user_id, budget["id"], period),
        )


class AppHandler(BaseHTTPRequestHandler):
    server_version = "LedgerHouse/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        if os.environ.get("APP_LOG", "0") == "1":
            super().log_message(fmt, *args)

    def respond(self, status: int, payload: object, headers: dict[str, str] | None = None):
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 64_000:
                raise ValueError("Request is too large.")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object.")
            return value
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise ValueError("Please submit valid form data.") from error

    def session(self, db: sqlite3.Connection) -> sqlite3.Row | None:
        cookie = self.headers.get("Cookie", "")
        match = re.search(r"(?:^|;\s*)ledger_session=([a-f0-9]{64})(?:;|$)", cookie)
        if not match:
            return None
        token_hash = hashlib.sha256(match.group(1).encode("ascii")).hexdigest()
        row = db.execute(
            """
            SELECT s.csrf_token, u.id, u.name, u.email, u.whatsapp_phone,
                u.whatsapp_opt_in
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, int(time.time())),
        ).fetchone()
        return row

    def require_session(self, db: sqlite3.Connection) -> sqlite3.Row | None:
        session = self.session(db)
        if not session:
            self.respond(HTTPStatus.UNAUTHORIZED, {"error": "Please sign in to continue."})
        return session

    def require_csrf(self, session: sqlite3.Row) -> bool:
        supplied = self.headers.get("X-CSRF-Token", "")
        if not supplied or not hmac.compare_digest(supplied, session["csrf_token"]):
            self.respond(HTTPStatus.FORBIDDEN, {"error": "Session expired. Refresh and try again."})
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.serve_file("index.html", "text/html; charset=utf-8")
            return
        if path in ("/app.css", "/app.js"):
            content_type = "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8"
            self.serve_file(path[1:], content_type)
            return
        if not path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        with database() as db:
            try:
                session = self.require_session(db)
                if not session:
                    return
                if path == "/api/me":
                    self.respond(
                        HTTPStatus.OK,
                        {
                            "user": {
                                "name": session["name"],
                                "email": session["email"],
                                "whatsapp_phone": session["whatsapp_phone"],
                                "whatsapp_opt_in": bool(session["whatsapp_opt_in"]),
                            },
                            "csrf": session["csrf_token"],
                            "categories": CATEGORIES,
                            "whatsapp_ready": all(
                                os.environ.get(key)
                                for key in (
                                    "TWILIO_ACCOUNT_SID",
                                    "TWILIO_AUTH_TOKEN",
                                    "TWILIO_WHATSAPP_FROM",
                                    "TWILIO_WHATSAPP_CONTENT_SID",
                                )
                            ),
                        },
                    )
                elif path == "/api/dashboard":
                    query = parse_qs(urlparse(self.path).query)
                    period = query.get("period", ["monthly"])[0]
                    self.respond(HTTPStatus.OK, self.dashboard(db, session["id"], period))
                elif path == "/api/transactions":
                    rows = db.execute(
                        """
                        SELECT id, kind, amount_minor, category, payee, note,
                            transaction_date
                        FROM transactions WHERE user_id = ?
                        ORDER BY transaction_date DESC, id DESC LIMIT 100
                        """,
                        (session["id"],),
                    ).fetchall()
                    transactions = []
                    for row in rows:
                        item = dict(row)
                        item["amount"] = item.pop("amount_minor") / 100
                        transactions.append(item)
                    self.respond(HTTPStatus.OK, {"transactions": transactions})
                else:
                    self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            except Exception as error:
                self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

    def dashboard(self, db: sqlite3.Connection, user_id: int, period: str) -> dict:
        period = period if period in ("daily", "monthly", "yearly") else "monthly"
        today = date.today()
        if period == "daily":
            start = today - timedelta(days=6)
            grouping = "transaction_date"
            labels = [(start + timedelta(days=i)).isoformat() for i in range(7)]
            sql_start = start.isoformat()
        elif period == "yearly":
            start = date(today.year - 4, 1, 1)
            grouping = "substr(transaction_date, 1, 4)"
            labels = [str(y) for y in range(today.year - 4, today.year + 1)]
            sql_start = start.isoformat()
        else:
            start = date(today.year, today.month, 1) - timedelta(days=150)
            start = date(start.year, start.month, 1)
            grouping = "substr(transaction_date, 1, 7)"
            labels = []
            cursor = date(start.year, start.month, 1)
            for _ in range(6):
                labels.append(cursor.strftime("%Y-%m"))
                cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
            sql_start = start.isoformat()
        points = db.execute(
            f"""
            SELECT {grouping} AS label,
                SUM(CASE WHEN kind = 'debit' THEN amount_minor ELSE 0 END) AS debit_minor,
                SUM(CASE WHEN kind = 'credit' THEN amount_minor ELSE 0 END) AS credit_minor
            FROM transactions
            WHERE user_id = ? AND transaction_date >= ? AND transaction_date <= ?
            GROUP BY label
            """,
            (user_id, sql_start, today.isoformat()),
        ).fetchall()
        point_map = {row["label"]: row for row in points}
        chart = [
            {
                "label": label,
                "debit": point_map[label]["debit_minor"] / 100 if label in point_map else 0,
                "credit": point_map[label]["credit_minor"] / 100 if label in point_map else 0,
            }
            for label in labels
        ]
        month = today.strftime("%Y-%m")
        totals = db.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN kind = 'credit' THEN amount_minor ELSE 0 END), 0) AS credit_minor,
                COALESCE(SUM(CASE WHEN kind = 'debit' THEN amount_minor ELSE 0 END), 0) AS debit_minor
            FROM transactions WHERE user_id = ? AND substr(transaction_date, 1, 7) = ?
            """,
            (user_id, month),
        ).fetchone()
        budget_rows = db.execute(
            """
            SELECT b.id, b.category, b.monthly_limit_minor,
                COALESCE(SUM(t.amount_minor), 0) AS spent_minor
            FROM budgets b
            LEFT JOIN transactions t ON t.user_id = b.user_id
                AND t.category = b.category AND t.kind = 'debit'
                AND substr(t.transaction_date, 1, 7) = ?
            WHERE b.user_id = ?
            GROUP BY b.id ORDER BY b.category
            """,
            (month, user_id),
        ).fetchall()
        latest = db.execute(
            """
            SELECT id, kind, amount_minor, category, payee, note, transaction_date
            FROM transactions WHERE user_id = ?
            ORDER BY transaction_date DESC, id DESC LIMIT 7
            """,
            (user_id,),
        ).fetchall()
        categories = db.execute(
            """
            SELECT category, SUM(amount_minor) AS spent_minor FROM transactions
            WHERE user_id = ? AND kind = 'debit'
                AND substr(transaction_date, 1, 7) = ?
            GROUP BY category ORDER BY spent_minor DESC LIMIT 5
            """,
            (user_id, month),
        ).fetchall()
        debit_minor = int(totals["debit_minor"])
        credit_minor = int(totals["credit_minor"])
        debit = debit_minor / 100
        credit = credit_minor / 100
        latest_transactions = []
        for row in latest:
            item = dict(row)
            item["amount"] = item.pop("amount_minor") / 100
            latest_transactions.append(item)
        return {
            "period": period,
            "totals": {
                "debit": round(debit, 2),
                "credit": round(credit, 2),
                "net": round(credit - debit, 2),
                "budget": sum(row["monthly_limit_minor"] for row in budget_rows) / 100,
            },
            "chart": chart,
            "budgets": [
                {
                    "id": row["id"],
                    "category": row["category"],
                    "limit": row["monthly_limit_minor"] / 100,
                    "spent": row["spent_minor"] / 100,
                    "ratio": round(row["spent_minor"] / row["monthly_limit_minor"], 4),
                }
                for row in budget_rows
            ],
            "transactions": latest_transactions,
            "categories": [
                {"category": row["category"], "spent": row["spent_minor"] / 100}
                for row in categories
            ],
        }

    def serve_file(self, filename: str, content_type: str):
        try:
            body = (ROOT / filename).read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        with database() as db:
            try:
                payload = self.read_json()
                if path in ("/api/register", "/api/login"):
                    self.authenticate(db, path, payload)
                    return
                session = self.require_session(db)
                if not session or not self.require_csrf(session):
                    return
                if path == "/api/logout":
                    cookie = self.headers.get("Cookie", "")
                    match = re.search(r"(?:^|;\s*)ledger_session=([a-f0-9]{64})(?:;|$)", cookie)
                    if match:
                        digest = hashlib.sha256(match.group(1).encode("ascii")).hexdigest()
                        db.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
                    self.respond(
                        HTTPStatus.OK,
                        {"ok": True},
                        {
                            "Set-Cookie": (
                                "ledger_session=; HttpOnly; SameSite=Lax; Path=/; "
                                f"Max-Age=0{'; Secure' if COOKIE_SECURE else ''}"
                            )
                        },
                    )
                elif path == "/api/transactions":
                    self.add_transaction(db, session["id"], payload)
                elif path == "/api/budgets":
                    self.save_budget(db, session["id"], payload)
                elif path == "/api/settings":
                    self.save_settings(db, session["id"], payload)
                else:
                    self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            except ValueError as error:
                self.respond(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception as error:
                self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

    def authenticate(self, db: sqlite3.Connection, path: str, payload: dict):
        email = str(payload.get("email", "")).strip().lower()
        password = str(payload.get("password", ""))
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("Enter a valid email address.")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters.")
        if path == "/api/register":
            name = str(payload.get("name", "")).strip()
            if not name or len(name) > 80:
                raise ValueError("Enter your name (up to 80 characters).")
            salt = secrets.token_hex(16)
            try:
                cursor = db.execute(
                    """
                    INSERT INTO users (name, email, password_hash, password_salt, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, email, password_digest(password, salt), salt, datetime.now().isoformat()),
                )
                user_id = cursor.lastrowid
            except sqlite3.IntegrityError as error:
                raise ValueError("An account with that email already exists.") from error
        else:
            user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if not user or not hmac.compare_digest(
                user["password_hash"], password_digest(password, user["password_salt"])
            ):
                raise ValueError("Email or password is incorrect.")
            user_id = user["id"]
        token = secrets.token_hex(32)
        csrf = secrets.token_urlsafe(24)
        db.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at) VALUES (?, ?, ?, ?)",
            (
                hashlib.sha256(token.encode("ascii")).hexdigest(),
                user_id,
                csrf,
                int(time.time()) + SESSION_SECONDS,
            ),
        )
        user = db.execute(
            """
            SELECT name, email, whatsapp_phone, whatsapp_opt_in
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        user_data = dict(user)
        user_data["whatsapp_opt_in"] = bool(user_data["whatsapp_opt_in"])
        self.respond(
            HTTPStatus.OK,
            {"user": user_data, "csrf": csrf, "categories": CATEGORIES},
            {
                "Set-Cookie": (
                    f"ledger_session={token}; HttpOnly; SameSite=Lax; Path=/; "
                    f"Max-Age={SESSION_SECONDS}{'; Secure' if COOKIE_SECURE else ''}"
                )
            },
        )

    def add_transaction(self, db: sqlite3.Connection, user_id: int, payload: dict):
        kind = payload.get("kind")
        category = str(payload.get("category", "")).strip()
        payee = str(payload.get("payee", "")).strip()
        note = str(payload.get("note", "")).strip()
        amount_value = payload.get("amount")
        transaction_date = str(payload.get("date", ""))
        if kind not in ("debit", "credit"):
            raise ValueError("Choose debit or credit.")
        if kind == "debit" and category == "Income":
            raise ValueError("Choose a spending category for a debit.")
        amount_minor = to_minor_units(amount_value, "amount")
        if not 0 < amount_minor <= 10_000_000_000:
            raise ValueError("Amount must be greater than zero and less than 100,000,000.")
        try:
            parsed_date = date.fromisoformat(transaction_date)
            if parsed_date > date.today():
                raise ValueError("Transaction date cannot be in the future.")
        except ValueError as error:
            if str(error) == "Transaction date cannot be in the future.":
                raise
            raise ValueError("Choose a valid transaction date.") from error
        if category not in CATEGORIES:
            raise ValueError("Choose a category from the list.")
        if not payee or len(payee) > 100:
            raise ValueError("Add a description (up to 100 characters).")
        if len(note) > 500:
            raise ValueError("Notes must be 500 characters or less.")
        db.execute(
            """
            INSERT INTO transactions
                (user_id, kind, amount_minor, category, payee, note, transaction_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, kind, amount_minor, category, payee, note, transaction_date, datetime.now().isoformat()),
        )
        if kind == "debit":
            dispatch_pending_alerts(db, user_id)
        self.respond(HTTPStatus.CREATED, {"ok": True})

    def save_budget(self, db: sqlite3.Connection, user_id: int, payload: dict):
        category = str(payload.get("category", "")).strip()
        limit_minor = to_minor_units(payload.get("limit"), "monthly limit")
        if category not in CATEGORIES or category == "Income":
            raise ValueError("Choose a spending category.")
        if not 0 < limit_minor <= 10_000_000_000:
            raise ValueError("Budget must be greater than zero.")
        db.execute(
            """
            INSERT INTO budgets (user_id, category, monthly_limit_minor) VALUES (?, ?, ?)
            ON CONFLICT(user_id, category) DO UPDATE SET monthly_limit_minor = excluded.monthly_limit_minor
            """,
            (user_id, category, limit_minor),
        )
        dispatch_pending_alerts(db, user_id)
        self.respond(HTTPStatus.CREATED, {"ok": True})

    def save_settings(self, db: sqlite3.Connection, user_id: int, payload: dict):
        phone = str(payload.get("whatsapp_phone", "")).strip()
        opt_in = payload.get("whatsapp_opt_in") is True
        if phone and not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
            raise ValueError("Use an international number, for example +14155550123.")
        if opt_in and not phone:
            raise ValueError("Enter a WhatsApp number before opting in.")
        if phone and not opt_in:
            raise ValueError("Please opt in before enabling WhatsApp budget alerts.")
        db.execute(
            "UPDATE users SET whatsapp_phone = ?, whatsapp_opt_in = ? WHERE id = ?",
            (phone, int(opt_in), user_id),
        )
        dispatch_pending_alerts(db, user_id)
        ready = all(
            os.environ.get(key)
            for key in (
                "TWILIO_ACCOUNT_SID",
                "TWILIO_AUTH_TOKEN",
                "TWILIO_WHATSAPP_FROM",
                "TWILIO_WHATSAPP_CONTENT_SID",
            )
        )
        self.respond(HTTPStatus.OK, {"ok": True, "whatsapp_ready": ready})


if __name__ == "__main__":
    init_db()
    print(f"Ledger House running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), AppHandler).serve_forever()
