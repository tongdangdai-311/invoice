"""Parse F.W. Webb invoice exports and store them in MySQL."""
import csv
import io
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymysql
from dotenv import load_dotenv
from pymysql.constants import CLIENT
from pymysql.cursors import DictCursor

# Kept outside OneDrive: this folder holds the database password, downloaded
# invoices and the browser profile with portal login cookies.
DATA_DIR = Path(
    os.environ.get("INVOICE_DATA_DIR")
    or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "InvoiceManagement"
)
load_dotenv(DATA_DIR / ".env")  # A real DATABASE_URL environment variable takes priority.

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id    VARCHAR(64) PRIMARY KEY,
        invoice_id  VARCHAR(64),
        job_name    VARCHAR(255) NOT NULL DEFAULT '',
        memo        TEXT NOT NULL,
        total_cents BIGINT NOT NULL,
        imported_at DATETIME NOT NULL,
        INDEX idx_invoice_id (invoice_id)
    ) CHARACTER SET utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS items (
        id          BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_id    VARCHAR(64) NOT NULL,
        product     VARCHAR(255) NOT NULL,
        description TEXT NOT NULL,
        qty         VARCHAR(32) NOT NULL,
        unit_price  VARCHAR(32) NOT NULL,
        uom         VARCHAR(32) NOT NULL,
        ext_cents   BIGINT NOT NULL,
        FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE CASCADE
    ) CHARACTER SET utf8mb4
    """,
)

_schema_ready = False


@contextmanager
def _cursor():
    """Yield a cursor in one transaction: commit on success, roll back on error."""
    global _schema_ready
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(f"DATABASE_URL is not set. Add it to {DATA_DIR / '.env'}")
    parsed = urlparse(url)
    conn = pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=parsed.path.lstrip("/"),
        charset="utf8mb4",
        cursorclass=DictCursor,
        # Encrypted, matching the URI's ssl-mode=REQUIRED (certificate not verified).
        ssl={"check_hostname": False},
        connect_timeout=15,
        # Make rowcount mean "rows matched", so re-saving an unchanged memo isn't a miss.
        client_flag=CLIENT.FOUND_ROWS,
    )
    try:
        with conn.cursor() as cursor:
            if not _schema_ready:
                for statement in SCHEMA:
                    cursor.execute(statement)
                _schema_ready = True
            yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def _to_cents(value: str) -> int:
    text = re.sub(r"[$,\s]", "", value)
    if not text:
        return 0
    negative = text.startswith("(") and text.endswith(")")
    try:
        amount = Decimal(text.strip("()"))
    except InvalidOperation:
        raise ValueError(f"Could not read price {value!r}") from None
    cents = int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return -cents if negative else cents


def parse_csv(raw: bytes) -> list[dict]:
    """Turn an exported file into one dict per order, with its line items."""
    text = _decode(raw)
    first_line = text.split("\n", 1)[0]
    delimiter = "\t" if "\t" in first_line else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    reader.fieldnames = [(name or "").strip().lower() for name in reader.fieldnames or []]

    missing = {"order #", "ext. price"} - set(reader.fieldnames)
    if missing:
        raise ValueError(f"Not an invoice export, missing columns: {sorted(missing)}")

    orders: dict[str, dict] = {}
    for row in reader:
        get = lambda col: (row.get(col) or "").strip()
        order_id = get("order #")
        if not order_id:
            continue
        order = orders.setdefault(
            order_id, {"order_id": order_id, "job_name": "", "total_cents": 0, "items": []}
        )
        # The portal puts the job in "PO"; "Jobname" is usually blank and can
        # hold a contact name instead, so it is only a fallback.
        order["job_name"] = order["job_name"] or get("po") or get("jobname")
        ext_cents = _to_cents(get("ext. price"))
        order["total_cents"] += ext_cents
        order["items"].append(
            {
                "product": get("product"),
                "description": get("product description"),
                "qty": get("qty"),
                "unit_price": get("prices"),
                "uom": get("per/uom"),
                "ext_cents": ext_cents,
            }
        )
    if not orders:
        raise ValueError("The file has no invoice lines")
    return list(orders.values())


def save_orders(orders: list[dict], invoice_id: str | None = None) -> None:
    """Insert or refresh orders. A memo the user already wrote is never overwritten."""
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    with _cursor() as cursor:
        for order in orders:
            cursor.execute(
                """
                INSERT INTO orders (order_id, invoice_id, job_name, memo, total_cents, imported_at)
                VALUES (%s, %s, %s, '', %s, %s) AS new
                ON DUPLICATE KEY UPDATE
                    invoice_id = COALESCE(new.invoice_id, orders.invoice_id),
                    job_name = new.job_name,
                    total_cents = new.total_cents,
                    imported_at = new.imported_at
                """,
                (order["order_id"], invoice_id, order["job_name"], order["total_cents"], now),
            )
            cursor.execute("DELETE FROM items WHERE order_id = %s", (order["order_id"],))
            cursor.executemany(
                """
                INSERT INTO items (order_id, product, description, qty, unit_price, uom, ext_cents)
                VALUES (%(order_id)s, %(product)s, %(description)s, %(qty)s,
                        %(unit_price)s, %(uom)s, %(ext_cents)s)
                """,
                [{**item, "order_id": order["order_id"]} for item in order["items"]],
            )


def known_invoice_ids() -> set[str]:
    with _cursor() as cursor:
        cursor.execute("SELECT invoice_id FROM orders WHERE invoice_id IS NOT NULL")
        return {row["invoice_id"] for row in cursor.fetchall()}


def list_orders() -> list[dict]:
    with _cursor() as cursor:
        cursor.execute("SELECT * FROM orders ORDER BY imported_at DESC, order_id DESC")
        orders = {row["order_id"]: {**row, "items": []} for row in cursor.fetchall()}
        cursor.execute("SELECT * FROM items ORDER BY id")
        for row in cursor.fetchall():
            orders[row["order_id"]]["items"].append(row)
    return list(orders.values())


def set_memo(order_id: str, memo: str) -> bool:
    with _cursor() as cursor:
        return cursor.execute("UPDATE orders SET memo = %s WHERE order_id = %s", (memo, order_id)) > 0
