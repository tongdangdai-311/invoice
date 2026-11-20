"""Ask Claude for a short memo describing what an order's items are for."""
import os

import anthropic

MODEL = "claude-opus-5-5"

SYSTEM = (
    "You write memos for a construction company's supplier invoices. "
    "Given an order's line items, reply with only a memo of 4 to 6 words saying what "
    "the materials are for, in plain words a bookkeeper would understand. "
    "Example: PVC drain and copper water piping. "
    "No quotes, no trailing period, nothing else."
)


class MemoUnavailable(Exception):
    """The memo could not be written; the message is safe to show the user."""


def suggest_memo(order: dict) -> str:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise MemoUnavailable("Add ANTHROPIC_API_KEY to app/.env and restart the app.")

    lines = "\n".join(
        f"- {item['description']} (qty {item['qty']} {item['uom']})" for item in order["items"]
    )
    prompt = f"Job: {order['job_name'] or 'unknown'}\nItems:\n{lines}"

    try:
        response = anthropic.Anthropic().beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={"effort": "low"},  # A short label needs little reasoning.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # If Claude declines, the API retries on another model.
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        raise MemoUnavailable("The Claude API key was rejected. Check ANTHROPIC_API_KEY.") from None
    except anthropic.RateLimitError:
        raise MemoUnavailable("Claude is rate limited right now. Try again in a minute.") from None
    except anthropic.APIStatusError as error:
        raise MemoUnavailable(f"Claude API error ({error.status_code}). Try again.") from None
    except anthropic.APIConnectionError:
        raise MemoUnavailable("Could not reach the Claude API. Check the internet connection.") from None

    if response.stop_reason == "refusal":
        raise MemoUnavailable("Claude declined to write a memo for this order.")
    memo = "".join(block.text for block in response.content if block.type == "text").strip()
    if not memo:
        raise MemoUnavailable("Claude returned an empty memo. Try again.")
    return memo.strip("\"'").rstrip(".")
