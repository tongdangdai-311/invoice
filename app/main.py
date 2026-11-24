"""Invoice Management web app. Run with: python -m uvicorn app.main:app"""
import hashlib
import hmac
import os
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pymysql
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import memo_ai, store
from app.scraper import SIGN_IN_NEEDED, SIGNED_IN

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
SESSION_COOKIE = "invoice_session"

app = FastAPI(title="Invoice Management")


class ScrapeJob:
    """Runs the scraper as a child process and keeps its output for the UI."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.exit_code: int | None = None
        self.log: list[str] = []
        self.needs_sign_in = False  # The scraper is waiting for you to sign in to F.W. Webb.
        self.process: subprocess.Popen | None = None
        self.cancelled = False

    def start(self) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.exit_code = None
            self.log = []
            self.needs_sign_in = False
            self.cancelled = False
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        code = 1
        try:
            process = self.process = subprocess.Popen(
                [sys.executable, "-u", "-m", "app.scraper"],
                cwd=ROOT,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in process.stdout:
                line = line.rstrip()
                self.log.append(line)
                if line == SIGN_IN_NEEDED:
                    self.needs_sign_in = True
                elif line == SIGNED_IN:
                    self.needs_sign_in = False
            code = process.wait()
        except OSError as error:
            self.log.append(f"Could not start the scraper: {error}")
        finally:
            with self.lock:
                self.exit_code = code
                self.running = False
                self.needs_sign_in = False
                self.process = None

    def cancel(self) -> bool:
        with self.lock:
            if not self.running or self.process is None:
                return False
            self.cancelled = True
            self.log.append("Scrape cancelled.")
            self.process.terminate()
            return True


job = ScrapeJob()


class MemoUpdate(BaseModel):
    memo: str


class Who(BaseModel):
    name: str = Field(min_length=1, max_length=100)  # Who clicked Add / Undo, for the import log.


class PickedUpByUpdate(BaseModel):
    picked_up_by: str = Field(max_length=100)


class DueDateUpdate(BaseModel):
    due_on: date | None  # null clears it


class Payment(BaseModel):
    paid_on: date
    card: str = Field(pattern=r"^\d{4}$")  # Last 4 digits only, never the full number.
    paid_by: str = Field(min_length=1, max_length=100)


def _session_token(password: str) -> bytes:
    # Derived from the password, so changing APP_PASSWORD signs everyone out.
    digest = hmac.new(password.encode(), b"invoice-session", hashlib.sha256).hexdigest()
    return digest.encode()


def _logged_in(request: Request) -> bool:
    password = os.environ.get("APP_PASSWORD")  # Unset means nobody can sign in.
    cookie = request.cookies.get(SESSION_COOKIE, "")
    return bool(password) and hmac.compare_digest(cookie.encode(), _session_token(password))


@app.exception_handler(store.DatabaseNotConfigured)
def database_not_configured(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse(
        {"detail": "The database is not connected yet: DATABASE_URL is not set on this server."},
        status_code=503,
    )


@app.exception_handler(pymysql.MySQLError)
def database_error(request: Request, error: Exception) -> JSONResponse:
    if isinstance(error, pymysql.err.DataError):
        return JSONResponse({"detail": "A value in this file is too long to save."}, status_code=400)
    return JSONResponse(
        {"detail": "Could not reach the database. Please try again shortly."}, status_code=503
    )


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path == "/login" or _logged_in(request):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Sign in required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return FileResponse(FRONTEND / "login.html")


@app.post("/login", include_in_schema=False)
def login(request: Request, password: str = Form("")) -> RedirectResponse:
    expected = os.environ.get("APP_PASSWORD")
    if not expected or not hmac.compare_digest(password.encode(), expected.encode()):
        time.sleep(1)  # Slows down password guessing.
        return RedirectResponse("/login?error=1", status_code=303)

    response = RedirectResponse("/", status_code=303)
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        SESSION_COOKIE,
        _session_token(expected).decode(),
        max_age=14 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=forwarded_proto == "https",
    )
    return response


@app.post("/logout", include_in_schema=False)
def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/fw-webb", include_in_schema=False)
def fw_webb() -> FileResponse:
    return FileResponse(FRONTEND / "fw_webb.html")


@app.get("/fw-webb/log", include_in_schema=False)
def import_log_page() -> FileResponse:
    return FileResponse(FRONTEND / "import_log.html")


@app.get("/fw-webb/paid", include_in_schema=False)
def fw_webb_paid() -> FileResponse:
    return FileResponse(FRONTEND / "fw_webb.html")  # Same page; it switches to paid mode by URL.


@app.get("/api/orders")
def get_orders(paid: bool = False, draft: bool = False) -> list[dict]:
    return store.list_orders(paid, draft)


@app.delete("/api/drafts/{order_id}")
def remove_draft(order_id: str) -> dict:
    if not store.remove_draft(order_id):
        raise HTTPException(404, "Draft not found")
    return {"removed": order_id}


@app.post("/api/drafts/add")
def add_drafts(who: Who) -> dict:
    name = who.name.strip()
    if not name:
        raise HTTPException(400, "Enter your name")
    return {"added": store.add_drafts(name)}


@app.get("/api/import-log")
def import_log() -> list[dict]:
    return store.list_batches()


@app.get("/api/import-log/{batch_id}/orders")
def import_log_orders(batch_id: int) -> list[dict]:
    return store.batch_orders(batch_id)


@app.post("/api/import-log/{batch_id}/undo")
def undo_import(batch_id: int, who: Who) -> dict:
    removed = store.undo_batch(batch_id, who.name.strip())
    if removed is None:
        raise HTTPException(409, "This import was already undone, or doesn't exist")
    return {"removed": removed}


@app.post("/api/drafts/discard")
def discard_drafts() -> dict:
    return {"discarded": store.discard_drafts()}


@app.put("/api/orders/{order_id}/payment")
def mark_paid(order_id: str, payment: Payment) -> dict:
    paid_by = payment.paid_by.strip()
    if not paid_by:
        raise HTTPException(400, "Enter who paid")
    if not store.mark_paid(order_id, payment.paid_on, payment.card, paid_by):
        raise HTTPException(404, "Order not found")
    return {"order_id": order_id, "paid_on": payment.paid_on, "card": payment.card, "paid_by": paid_by}


@app.delete("/api/orders/{order_id}/payment")
def mark_unpaid(order_id: str) -> dict:
    if not store.mark_unpaid(order_id):
        raise HTTPException(404, "Order not found")
    return {"order_id": order_id}


@app.post("/api/orders/{order_id}/suggest-memo")
def suggest_memo(order_id: str) -> dict:
    order = store.get_order(order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    if not order["items"]:
        raise HTTPException(400, "This order has no items to describe")
    try:
        return {"memo": memo_ai.suggest_memo(order)}
    except memo_ai.MemoUnavailable as error:
        raise HTTPException(503, str(error)) from None


@app.patch("/api/orders/{order_id}/due-date")
def update_due_date(order_id: str, update: DueDateUpdate) -> dict:
    if not store.set_due_date(order_id, update.due_on):
        raise HTTPException(404, "Order not found")
    return {"order_id": order_id, "due_on": update.due_on}


@app.patch("/api/orders/{order_id}/picked-up-by")
def update_picked_up_by(order_id: str, update: PickedUpByUpdate) -> dict:
    name = update.picked_up_by.strip()
    if not store.set_picked_up_by(order_id, name):
        raise HTTPException(404, "Order not found")
    return {"order_id": order_id, "picked_up_by": name or None}


@app.patch("/api/orders/{order_id}")
def update_memo(order_id: str, update: MemoUpdate) -> dict:
    if not store.set_memo(order_id, update.memo.strip()):
        raise HTTPException(404, "Order not found")
    memo = update.memo.strip()
    return {"order_id": order_id, "memo": memo, "job_type": store.job_type_of(memo)}


@app.post("/api/import")
def import_csv(file: UploadFile, added_by: str = Form("")) -> dict:
    try:
        orders = store.parse_csv(file.file.read())
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    new_ids = store.save_orders(orders)
    store.log_csv_import(new_ids, added_by.strip()[:100])
    return {
        "imported": [
            {
                "order_id": order["order_id"],
                "items": len(order["items"]),
                "total_cents": order["total_cents"],
            }
            for order in orders
        ]
    }


@app.post("/api/scrape", status_code=202)
def start_scrape() -> dict:
    if not job.start():
        raise HTTPException(409, "A scrape is already running")
    return {"running": True}


@app.post("/api/scrape/cancel")
def cancel_scrape() -> dict:
    if not job.cancel():
        raise HTTPException(409, "No scrape is running")
    return {"cancelled": True}


@app.get("/api/scrape")
def scrape_status() -> dict:
    return {
        "running": job.running,
        "exit_code": job.exit_code,
        "needs_sign_in": job.needs_sign_in,
        "cancelled": job.cancelled,
        "log": list(job.log),
    }
