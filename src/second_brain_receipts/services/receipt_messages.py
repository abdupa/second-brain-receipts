"""Plain-text presentation by default. No floats or storage URLs anywhere.

Only the execution-summary "card" (success_summary/remembered_summary) opts into
Telegram MarkdownV2 formatting, since that is the one message the assignment asks to
be a formatted card. Every value placed in that card, static label text included,
passes through escape_markdown_v2 so a vendor/category name containing MarkdownV2
syntax characters renders as literal text instead of being interpreted as formatting
or breaking the send (Telegram rejects an incorrectly escaped MarkdownV2 message).
"""

from typing import Final, Literal

from second_brain_receipts.schemas.receipts import Receipt, ReceiptExtraction

# Telegram MarkdownV2 requires these characters to be backslash-escaped everywhere
# they appear literally, even in ordinary prose (e.g. a plain "."  at a sentence end).
# https://core.telegram.org/bots/api#markdownv2-style
_MARKDOWN_V2_RESERVED = set("_*[]()~`>#+-=|{}.!\\")


def escape_markdown_v2(text: str) -> str:
    return "".join(f"\\{char}" if char in _MARKDOWN_V2_RESERVED else char for char in text)


def _bold(text: str) -> str:
    return f"*{escape_markdown_v2(text)}*"


UNREADABLE = (
    "I couldn't reliably read this receipt. Please send another photo with the full "
    "receipt visible, better lighting, and the vendor, date and total clearly readable."
)
SYSTEM_FAILURE = "I couldn't process this receipt right now. Please try again later."
UNSUPPORTED = "Please send a receipt photo or reply to the active category question."


def display_value(value: str) -> str:
    # Bound user/model supplied fields so a valid receipt always fits one message.
    # Collapse line breaks/control characters to prevent forged summary labels.
    clean = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    return clean if len(clean) <= 300 else clean[:299] + "…"


# Callers pass this as send_message's parse_mode for success_summary/remembered_summary
# only; every other message in this module stays plain text (see module docstring).
MARKDOWN_V2: Final[Literal["MarkdownV2"]] = "MarkdownV2"


def _field(label: str, value: str) -> str:
    # ":" is not a reserved MarkdownV2 character, so only the label and value need
    # escaping; the label is our own fixed text but is still routed through the
    # escaper (via _bold) for consistency and defense in depth.
    return f"{_bold(label)}: {escape_markdown_v2(value)}"


def success_summary(receipt: Receipt, currency: str) -> str:
    # Every dynamic field goes through escape_markdown_v2 (via _field) so a
    # vendor/category name that happens to contain MarkdownV2 syntax characters
    # (e.g. "_[Vendor]*") renders as literal text rather than formatting or breaking
    # the send. See the module docstring for why this message alone uses MarkdownV2.
    lines = [
        "✅ *Receipt Processed*",
        "",
        _field("Vendor", display_value(receipt.vendor_name)),
        _field("Date", f"{receipt.receipt_date:%d/%m/%Y}"),
        _field("Total", f"{currency} {receipt.total_amount:,.2f}"),
    ]
    if receipt.vat_amount is not None:
        lines.append(_field("VAT", f"{currency} {receipt.vat_amount:,.2f}"))
    lines.extend(
        (
            _field("Category", display_value(receipt.category)),
            _field("Confidence", receipt.confidence_score.value),
        )
    )
    return "\n".join(lines)


def duplicate_summary(extraction: ReceiptExtraction, currency: str) -> str:
    return (
        "This receipt appears to have already been recorded.\n\n"
        f"Vendor: {display_value(extraction.vendor_name)}\n"
        f"Date: {extraction.date:%d/%m/%Y}\n"
        f"Amount: {currency} {extraction.total_amount:,.2f}\n\n"
        "No duplicate entry was created."
    )


WAITING = (
    "You already have a receipt waiting for a category. "
    'Reply with its category or "cancel" before submitting another receipt.'
)
NO_PENDING = (
    "There is no receipt currently waiting for a category. Please send a receipt photo first."
)
INVALID_CATEGORY = (
    "Please reply with a category name of 1–100 characters, "
    "without control characters, or send cancel."
)
CANCELLED = "Pending receipt cancelled. No receipt was saved."
EXPIRED = "That pending receipt has expired. Please send the receipt photo again."


def category_prompt(vendor_name: str) -> str:
    return (
        f"Unrecognized Vendor: {display_value(vendor_name)}\n\n"
        "Which expense category should I assign this to?\n"
        "Examples: Food Supplies, Utilities, Maintenance, Office Supplies, Transportation.\n\n"
        'Reply with the category name, or "cancel" to discard this pending receipt.\n'
        "The receipt has not been recorded yet."
    )


def remembered_summary(receipt: Receipt, currency: str) -> str:
    # success_summary is already fully escaped; escape this sentence once as a whole
    # (its own punctuation included) rather than re-escaping already-escaped text.
    note = escape_markdown_v2(
        f"Saved category memory for {display_value(receipt.vendor_name)}: "
        f"{display_value(receipt.category)}."
    )
    return f"{success_summary(receipt, currency)}\n\n{note}"
