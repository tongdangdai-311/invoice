"""Fill the database with fake invoices shaped like docs/sample/csv_sample.csv.

    python seed.py                # add 100 fake orders
    python seed.py --count 250    # add a different number
    python seed.py --dry-run      # print a few, write nothing
    python seed.py --clear        # delete every seeded order
    python seed.py --count 10 --due overdue   # due dates the page shows red
    python seed.py --count 20 --due soon      # due in 0-10 days (yellow)
    python seed.py --count 3 --due ok         # due in more than 10 days (green)

Seeded orders have ids starting with "SEED-", so they never collide with real
F.W. Webb order numbers and --clear removes only them.
"""
import argparse
import random
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from faker import Faker

from app.store import _cursor, parse_csv

SAMPLE = Path(__file__).parent / "docs" / "sample" / "csv_sample.csv"
PREFIX = "SEED-"

# Days from today for each due-date color on the F.W. Webb page.
DUE_RANGES = {"overdue": (-60, -1), "soon": (0, 10), "ok": (11, 60)}

JOBS = [
    "HARVARD STANDISH UNDERGROUND",
    "273 HIGHLANDS",
    "45 BEACON ST RENO",
    "MIT BLDG 46 BOILER ROOM",
    "SOMERVILLE AVE MIXED USE",
    "BROOKLINE CONDO ROUGH-IN",
    "QUINCY SCHOOL HVAC",
    "SHOP STOCK",
]
MEMOS = [
    "Underground plumbing rough-in",
    "Copper supply lines and fittings",
    "Hangers and riser clamps",
    "PVC drain, waste and vent",
    "Insulation for hot water lines",
    "Saw blades and shop consumables",
    "Fasteners and threaded rod",
    "",  # Some orders have no memo yet, like freshly scraped ones.
]


def load_catalog() -> list[dict]:
    """Real products from the sample export: product, description, price, uom."""
    orders = parse_csv(SAMPLE.read_bytes())
    return [
        {k: item[k] for k in ("product", "description", "unit_price", "uom")}
        for order in orders
        for item in order["items"]
    ]


def ext_cents(qty: int, unit_price: str, uom: str) -> int:
    """Ext. price the way the portal computes it: "100/EA" means price per 100."""
    per = int(uom.split("/")[0] or 1)
    amount = Decimal(unit_price) * qty / per
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def make_orders(count: int, fake: Faker, catalog: list[dict], due: str | None = None) -> list[dict]:
    orders = []
    for n in range(count):
        if due:
            due_on = date.today() + timedelta(days=random.randint(*DUE_RANGES[due]))
            purchased_on = due_on - timedelta(days=30)
        else:
            purchased_on = fake.date_between(start_date="-365d", end_date="today")
            # Net 30, but leave some blank since due dates are typed in by hand.
            due_on = purchased_on + timedelta(days=30) if random.random() < 0.7 else None
        items = []
        for product in random.sample(catalog, k=random.randint(1, min(12, len(catalog)))):
            qty = random.choice([1, 2, 3, 5, 6, 10, 15, 25, 40, 48, 100])
            items.append(
                {
                    **product,
                    "qty": str(qty),
                    "ext_cents": ext_cents(qty, product["unit_price"], product["uom"]),
                }
            )
        order_id = f"{PREFIX}{fake.unique.random_number(digits=8, fix_len=True)}"
        orders.append(
            {
                "order_id": order_id,
                "invoice_id": f"{order_id}-INV",
                "job_name": random.choice(JOBS),
                "memo": random.choice(MEMOS),
                "total_cents": sum(item["ext_cents"] for item in items),
                "purchased_on": purchased_on,
                "due_on": due_on,
                "items": items,
            }
        )
    return orders


def insert(orders: list[dict]) -> None:
    """One connection and one transaction for the whole batch."""
    with _cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO orders
                (order_id, invoice_id, job_name, memo, total_cents,
                 imported_at, purchased_on, due_on)
            VALUES (%(order_id)s, %(invoice_id)s, %(job_name)s, %(memo)s, %(total_cents)s,
                    UTC_TIMESTAMP(), %(purchased_on)s, %(due_on)s)
            """,
            [{k: v for k, v in o.items() if k != "items"} for o in orders],
        )
        cursor.executemany(
            """
            INSERT INTO items (order_id, product, description, qty, unit_price, uom, ext_cents)
            VALUES (%(order_id)s, %(product)s, %(description)s, %(qty)s,
                    %(unit_price)s, %(uom)s, %(ext_cents)s)
            """,
            [{**item, "order_id": o["order_id"]} for o in orders for item in o["items"]],
        )


def clear() -> int:
    with _cursor() as cursor:  # items go too, via ON DELETE CASCADE.
        return cursor.execute("DELETE FROM orders WHERE order_id LIKE %s", (PREFIX + "%",))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--seed", type=int, help="same seed, same data (default: random)")
    parser.add_argument("--due", choices=DUE_RANGES, help="give every order a due date in this range")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()

    if args.clear:
        print(f"Deleted {clear()} seeded orders")
        return

    random.seed(args.seed)
    Faker.seed(args.seed)
    orders = make_orders(args.count, Faker(), load_catalog(), args.due)

    if args.dry_run:
        for o in orders[:5]:
            print(o["order_id"], o["job_name"], o["purchased_on"], "due", o["due_on"],
                  f"${o['total_cents'] / 100:,.2f}", f"{len(o['items'])} items")
        print(f"... {len(orders)} orders, {sum(len(o['items']) for o in orders)} items (not saved)")
        return

    insert(orders)
    print(f"Inserted {len(orders)} orders")


if __name__ == "__main__":
    main()
