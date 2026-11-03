"""Download invoice exports from the F.W. Webb portal.

Run by the web app as a separate process, or directly: python -m app.scraper

You sign in yourself in the browser window, so no password is stored. The
scraper only reads: on the bill page it just expands the invoice lists and
never clicks a payment control.
"""
import os
import re
import sys
from urllib.parse import urlparse

from pymysql import MySQLError
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from app.store import DATA_DIR, known_invoice_ids, parse_csv, save_orders

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
EXPORT_LABEL_RE = re.compile(r"^\s*(export|download)\s*$", re.I)


def log(message: str) -> None:
    print(message, flush=True)


def wait_for_sign_in(page) -> None:
    """Open the login page; an existing session redirects straight to the site."""
    site_host = urlparse(BILL_URL).hostname
    page.goto(LOGIN_URL)
    if urlparse(page.url).hostname != site_host:
        log("Please sign in using the browser window (waiting up to 5 minutes)...")
    page.wait_for_url(lambda url: urlparse(url).hostname == site_host, timeout=SIGN_IN_TIMEOUT_MS)
    log("Signed in.")


def find_invoice_ids(page) -> list[str]:
    page.goto(BILL_URL)
    for label in ("Future", "Current"):
        section = page.get_by_text(label, exact=True).first
        try:
            section.click(timeout=3000)
        except PlaywrightError:
            pass  # Not every account has every section.

    ids: list[str] = []
    for _ in range(20):  # The list can load after the page does.
        text = page.evaluate("document.body.textContent")
        ids = list(dict.fromkeys(INVOICE_ID_RE.findall(text)))
        if ids:
            break
        page.wait_for_timeout(1000)
    return ids


def download_invoice(page, invoice_id: str) -> None:
    page.goto(INVOICE_URL.format(id=invoice_id))
    controls = page.locator("a, button, [role=button]")
    export = controls.filter(has_text=EXPORT_LABEL_RE).or_(
        page.locator('[title="Export" i], [title="Download" i]')
    )
    with page.expect_download(timeout=PAGE_TIMEOUT_MS) as download_info:
        export.first.click(timeout=PAGE_TIMEOUT_MS)

    target = DATA_DIR / "downloads" / f"{invoice_id}.csv"
    download_info.value.save_as(target)
    orders = parse_csv(target.read_bytes())
    save_orders(orders, invoice_id=invoice_id)
    for order in orders:
        log(f"  {invoice_id}: order {order['order_id']}, {len(order['items'])} items, "
            f"${order['total_cents'] / 100:,.2f}, job: {order['job_name'] or '(none)'}")


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
            invoice_ids = find_invoice_ids(page)
            if not invoice_ids:
                page.screenshot(path=DATA_DIR / "no_invoices_found.png")
                log(f"No invoices found on {page.url}. "
                    f"Screenshot saved to {DATA_DIR / 'no_invoices_found.png'}")
                return 1

            known = known_invoice_ids()
            new_ids = [i for i in invoice_ids if i not in known]
            log(f"Found {len(invoice_ids)} invoices, {len(new_ids)} new.")

            failed = []
            for invoice_id in new_ids:
                try:
                    download_invoice(page, invoice_id)
                except (PlaywrightError, ValueError, MySQLError) as error:
                    failed.append(invoice_id)
                    log(f"  {invoice_id}: FAILED, {str(error).splitlines()[0]}")

            log(f"Done. Saved {len(new_ids) - len(failed)}, failed {len(failed)}.")
            return 1 if failed else 0
        finally:
            context.close()


if __name__ == "__main__":
    try:
        sys.exit(scrape())
    except (PlaywrightError, MySQLError, RuntimeError) as error:
        log(f"Stopped: {str(error).splitlines()[0]}")
        sys.exit(1)
