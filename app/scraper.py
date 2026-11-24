"""Download invoice exports from the F.W. Webb portal.

Run by the web app as a separate process, or directly: python -m app.scraper

You sign in yourself in the browser window, so no password is stored. The
scraper only reads: on the bill page it just expands the invoice lists and
never clicks a payment control.
"""
import os
import re
import sys
import time
from datetime import date
from urllib.parse import urlparse

from pymysql import MySQLError
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from app.store import (
    DATA_DIR, fill_picked_up_by, known_invoice_ids, parse_csv, person_from_job_name, save_orders,
)

LOGIN_URL = os.environ.get(
    "FW_LOGIN_URL",
    "https://app.prod.fwwebb.com/auth?client_id=da8a4d0d-e71a-496e-8936-831d9d114b9c"
    "&scope=openid+offline_access&redirect_uri=https%3A%2F%2Fwww.fwwebb.com%2Fwobf%2Flogin.oidc"
    "&state=Lw..&post_redirect=true",
)
BILL_URL = os.environ.get("FW_BILL_URL", "https://www.fwwebb.com/wobf/payyourbill")
INVOICE_URL = os.environ.get(
    "FW_INVOICE_URL", "https://www.fwwebb.com/wobf/order.search.display+@ID={id}&OTYPE=INVOICE"
)
HEADLESS = os.environ.get("FW_HEADLESS") == "1"

SIGN_IN_TIMEOUT_MS = 5 * 60 * 1000
PAGE_TIMEOUT_MS = 30 * 1000
INVOICE_ID_RE = re.compile(r"Invoice ID:\s*(\d+)")
# On Pay My Bill each invoice reads "Invoice ID: 1 Balance: ... Job Name: VU PO #: HARVARD STANDISH".
BILL_FIELD_RE = r"{}\s*(.*?)\s*(?={}|$)"
EXPORT_LABEL_RE = re.compile(r"^\s*(export|download)\s*$", re.I)

# The invoice page shows "Order Date: 07/21/2026" (when it was bought) and "Invoice Date: ...".
ORDER_DATE_RE = re.compile(r"Order Date:?\s*(\d{1,2})/(\d{1,2})/(\d{4})", re.I)
INVOICE_DATE_RE = re.compile(r"Invoice Date:?\s*(\d{1,2})/(\d{1,2})/(\d{4})", re.I)

# The web app watches for these exact lines to know when to ask you to sign in.
SIGN_IN_NEEDED = "Please sign in using the browser window (waiting up to 5 minutes)..."
SIGNED_IN = "Signed in."


STARTED = time.monotonic()


def log(message: str) -> None:
    # The sign-in marker lines must stay exact (the web app matches them); others get a timer.
    if message in (SIGN_IN_NEEDED, SIGNED_IN):
        print(message, flush=True)
    else:
        print(f"[{time.monotonic() - STARTED:5.1f}s] {message}", flush=True)


class Blocked(RuntimeError):
    """F.W. Webb (Cloudflare) refused this browser; waiting will not help."""


# Pages F.W. Webb's Cloudflare shows instead of the site.
BLOCKED_RE = re.compile(r"you have been blocked|access denied", re.I)
CHALLENGE_RE = re.compile(r"verify you are human|checking your browser|just a moment", re.I)


def page_state(page, site_host: str) -> str:
    """'signed_in', 'sign_in' (login form showing), 'blocked', 'challenge' or 'loading'."""
    try:
        text = page.evaluate("document.body ? document.body.innerText : ''")
    except PlaywrightError:
        return "loading"  # The page is mid-redirect.
    if BLOCKED_RE.search(text) and "Ray ID" in text:
        return "blocked"
    if CHALLENGE_RE.search(text):
        return "challenge"
    if urlparse(page.url).hostname == site_host:
        return "signed_in"  # The login link only sends you on to the site once you're signed in.
    if page.locator("input[type=password]").count():
        return "sign_in"
    return "loading"


def wait_for(page, site_host: str, done: set[str], timeout_s: float) -> str:
    """Check the page twice a second until it reaches one of `done`, or time runs out."""
    deadline = time.monotonic() + timeout_s
    state = page_state(page, site_host)
    while state not in done and time.monotonic() < deadline:
        page.wait_for_timeout(500)
        state = page_state(page, site_host)
    return state


def wait_for_sign_in(page) -> None:
    """Open the login link and decide within seconds: signed in already -> go on;
    login form -> ask you to sign in; Cloudflare block -> stop with a clear message."""
    site_host = urlparse(BILL_URL).hostname
    log("Checking whether you're signed in to F.W. Webb...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    state = wait_for(page, site_host, {"signed_in", "sign_in", "blocked"}, timeout_s=10)
    if state == "challenge":
        log("F.W. Webb is checking the browser (Cloudflare). Waiting up to 30 seconds...")
        page.bring_to_front()
        state = wait_for(page, site_host, {"signed_in", "sign_in", "blocked"}, timeout_s=30)
    if state == "blocked":
        raise Blocked("F.W. Webb blocked this browser (Cloudflare). Try again later, "
                      "or contact onlinesolutions@fwwebb.com if it keeps happening.")
    if state == "signed_in":
        log(SIGNED_IN)
        return
    if state != "sign_in":
        page.screenshot(path=DATA_DIR / "sign_in_unclear.png")
        log(f"Couldn't tell whether you're signed in (page: {page.url}). Asking you to sign in; "
            f"screenshot saved to {DATA_DIR / 'sign_in_unclear.png'}")
    log(SIGN_IN_NEEDED)
    page.bring_to_front()  # So the window isn't hidden behind others.
    state = wait_for(page, site_host, {"signed_in", "blocked"}, timeout_s=SIGN_IN_TIMEOUT_MS / 1000)
    if state == "blocked":
        raise Blocked("F.W. Webb blocked this browser (Cloudflare) after sign-in. Try again later.")
    if state != "signed_in":
        raise RuntimeError("Sign-in wasn't finished within 5 minutes.")
    log(SIGNED_IN)


def picked_up_by_from_bill(text: str) -> dict[str, str]:
    """Invoice ID -> who ordered or picked it up, read from the Pay My Bill text. Only clear
    names are kept (see person_from_job_name); unsure ones are left out."""
    text = re.sub(r"\s+", " ", text)
    names = {}
    for chunk in text.split("Invoice ID:")[1:]:
        invoice_id = chunk.split()[0] if chunk.split() else ""
        job_name = re.search(BILL_FIELD_RE.format("Job Name:", "PO #:"), chunk)
        po = re.search(BILL_FIELD_RE.format("PO #:", r"Current Total|Future Total|Select all|Total:"), chunk)
        name = person_from_job_name(job_name[1] if job_name else "", po[1] if po else "")
        if invoice_id.isdigit() and name:
            names[invoice_id] = name
    return names


def find_invoice_ids(page) -> tuple[list[str], dict[str, str]]:
    page.goto(BILL_URL, wait_until="domcontentloaded")
    if page_state(page, urlparse(BILL_URL).hostname) == "blocked":
        raise Blocked("F.W. Webb blocked this browser (Cloudflare) on Pay My Bill. Try again later.")
    # Future / Current are accordion sections; open only the closed ones (clicking an
    # open one would close it). Their invoices are in the page either way.
    closed = page.locator('button.accordion-button[aria-expanded="false"]')
    for i in range(closed.count()):
        try:
            closed.nth(i).click(timeout=2000)
        except PlaywrightError:
            pass

    ids: list[str] = []
    text = ""
    for _ in range(20):  # The list can load after the page does.
        text = page.evaluate("document.body.textContent")
        ids = list(dict.fromkeys(INVOICE_ID_RE.findall(text)))
        if ids:
            break
        page.wait_for_timeout(1000)
    return ids, picked_up_by_from_bill(text)


def find_purchase_date(text: str) -> date | None:
    """Order Date if shown, otherwise Invoice Date."""
    for pattern in (ORDER_DATE_RE, INVOICE_DATE_RE):
        match = pattern.search(text)
        if match:
            month, day, year = (int(part) for part in match.groups())
            try:
                return date(year, month, day)
            except ValueError:
                continue
    return None


def download_invoice(page, invoice_id: str, picked_up_by: str = "") -> None:
    page.goto(INVOICE_URL.format(id=invoice_id), wait_until="domcontentloaded")
    if page_state(page, urlparse(BILL_URL).hostname) == "blocked":
        raise Blocked("F.W. Webb blocked this browser (Cloudflare) partway through. Try again later.")
    purchased_on = find_purchase_date(page.evaluate("document.body.textContent"))
    controls = page.locator("a, button, [role=button]")
    export = controls.filter(has_text=EXPORT_LABEL_RE).or_(
        page.locator('[title="Export" i], [title="Download" i]')
    )
    with page.expect_download(timeout=PAGE_TIMEOUT_MS) as download_info:
        export.first.click(timeout=PAGE_TIMEOUT_MS)

    target = DATA_DIR / "downloads" / f"{invoice_id}.csv"
    download_info.value.save_as(target)
    orders = parse_csv(target.read_bytes())
    # Saved as drafts: they wait in the app's draft list until you check them and click Add.
    save_orders(orders, invoice_id=invoice_id, purchased_on=purchased_on,
                picked_up_by=picked_up_by, draft=True)
    for order in orders:
        log(f"  {invoice_id}: order {order['order_id']}, {len(order['items'])} items, "
            f"${order['total_cents'] / 100:,.2f}, job: {order['job_name'] or '(none)'}, "
            f"purchased: {purchased_on or 'date not found'}, "
            f"picked up by: {picked_up_by or order.get('picked_up_by') or '(unknown)'}")


def scrape() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            DATA_DIR / "browser_profile", headless=HEADLESS, accept_downloads=True
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            wait_for_sign_in(page)

            log("Reading the invoice list...")
            invoice_ids, picked_up_by = find_invoice_ids(page)
            if not invoice_ids:
                page.screenshot(path=DATA_DIR / "no_invoices_found.png")
                log(f"No invoices found on {page.url}. "
                    f"Screenshot saved to {DATA_DIR / 'no_invoices_found.png'}")
                return 1

            known = known_invoice_ids()
            new_ids = [i for i in invoice_ids if i not in known]
            log(f"Found {len(invoice_ids)} invoices, {len(new_ids)} new.")
            # Invoices saved before this was read still get their name, if it's clear.
            for invoice_id in invoice_ids:
                if invoice_id in known and invoice_id in picked_up_by:
                    fill_picked_up_by(invoice_id, picked_up_by[invoice_id])

            failed = []
            for invoice_id in new_ids:
                try:
                    download_invoice(page, invoice_id, picked_up_by.get(invoice_id, ""))
                except (PlaywrightError, ValueError, MySQLError) as error:
                    failed.append(invoice_id)
                    log(f"  {invoice_id}: FAILED, {str(error).splitlines()[0]}")

            saved = len(new_ids) - len(failed)
            log(f"Done. {saved} new invoice{'' if saved == 1 else 's'} in the draft list "
                f"for review, failed {len(failed)}.")
            return 1 if failed else 0
        finally:
            context.close()


if __name__ == "__main__":
    try:
        sys.exit(scrape())
    except (PlaywrightError, MySQLError, RuntimeError) as error:
        log(f"Stopped: {str(error).splitlines()[0]}")
        sys.exit(1)
