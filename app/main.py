"""Invoice Management web app. Run with: python -m uvicorn app.main:app"""
import os
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import store

ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="Invoice Management")


class ScrapeJob:
    """Runs the scraper as a child process and keeps its output for the UI."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.exit_code: int | None = None
        self.log: list[str] = []

    def start(self) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.exit_code = None
            self.log = []
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        code = 1
        try:
            process = subprocess.Popen(
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
                self.log.append(line.rstrip())
            code = process.wait()
        except OSError as error:
            self.log.append(f"Could not start the scraper: {error}")
        finally:
            with self.lock:
                self.exit_code = code
                self.running = False


job = ScrapeJob()


class MemoUpdate(BaseModel):
    memo: str


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/fw-webb", include_in_schema=False)
def fw_webb() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "fw_webb.html")


@app.get("/api/orders")
def get_orders() -> list[dict]:
    return store.list_orders()


@app.patch("/api/orders/{order_id}")
def update_memo(order_id: str, update: MemoUpdate) -> dict:
    if not store.set_memo(order_id, update.memo.strip()):
        raise HTTPException(404, "Order not found")
    return {"order_id": order_id, "memo": update.memo.strip()}


@app.post("/api/import")
def import_csv(file: UploadFile) -> dict:
    try:
        orders = store.parse_csv(file.file.read())
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    store.save_orders(orders)
    return {"imported": [order["order_id"] for order in orders]}


@app.post("/api/scrape", status_code=202)
def start_scrape() -> dict:
    if not job.start():
        raise HTTPException(409, "A scrape is already running")
    return {"running": True}


@app.get("/api/scrape")
def scrape_status() -> dict:
    return {"running": job.running, "exit_code": job.exit_code, "log": list(job.log)}
