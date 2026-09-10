from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from second_brain_receipts.domain.vendor import normalize_vendor_name
from second_brain_receipts.schemas.receipts import Confidence, Receipt
from second_brain_receipts.services.receipt_messages import (
    MARKDOWN_V2,
    escape_markdown_v2,
    remembered_summary,
    success_summary,
)

# The exact reserved-character set from Telegram's MarkdownV2 documentation:
# https://core.telegram.org/bots/api#markdownv2-style
RESERVED = "_*[]()~`>#+-=|{}.!\\"


def _receipt(**overrides):
    values = dict(
        id=uuid4(),
        telegram_chat_id=123,
        vendor_name="ABC Hardware",
        normalized_vendor_name="abc hardware",
        receipt_date=date(2026, 9, 10),
        total_amount=Decimal("1280.00"),
        vat_amount=Decimal("137.14"),
        category="Maintenance",
        confidence_score=Confidence.HIGH,
        image_storage_key="receipts/2026/09/opaque.jpg",
        image_storage_uri="s3://test-bucket/receipts/2026/09/opaque.jpg",
        created_at=datetime.now(UTC),
    )
    values.update(overrides)
    # Receipt validates that the normalized name matches the display name.
    if "vendor_name" in overrides and "normalized_vendor_name" not in overrides:
        values["normalized_vendor_name"] = normalize_vendor_name(values["vendor_name"])
    return Receipt(**values)


def test_escape_markdown_v2_escapes_every_reserved_character():
    for char in RESERVED:
        assert escape_markdown_v2(char) == f"\\{char}"
    # Ordinary text, including characters MarkdownV2 does not reserve, is untouched.
    assert escape_markdown_v2("Café / 費用, 100% ok") == "Café / 費用, 100% ok"


def test_escape_markdown_v2_neutralizes_every_reserved_character_at_once():
    # A vendor name crafted from every reserved character must render as literal
    # text: every occurrence gets exactly one preceding backslash, never zero (which
    # would let it be interpreted as formatting).
    escaped = escape_markdown_v2(RESERVED)
    assert escaped == "".join(f"\\{char}" for char in RESERVED)


def test_success_summary_card_has_bold_labels_and_code_span_values():
    message = success_summary(_receipt(), "PHP")
    assert message.startswith("✅ *Receipt Processed*\n\n")
    assert "*Vendor*: `ABC Hardware`" in message
    assert "*Date*: `10/09/2026`" in message
    assert "*Total*: `PHP 1,280.00`" in message
    assert "*VAT*: `PHP 137.14`" in message
    assert "*Category*: `Maintenance`" in message
    assert "*Confidence*: `High`" in message


def test_success_summary_omits_vat_line_when_absent():
    message = success_summary(_receipt(vat_amount=None), "PHP")
    assert "VAT" not in message


def test_every_value_is_inside_a_code_span():
    # Telegram scans message text for URLs, hashtags and mentions regardless of
    # parse_mode, and escaping does not prevent it because it is not Markdown. Text
    # inside a code entity is exempt, so no value may sit outside one. Backticks must
    # therefore pair up across the whole message.
    for message in (remembered_summary(_receipt(), "PHP"), success_summary(_receipt(), "PHP")):
        unescaped = message.replace("\\`", "")
        assert unescaped.count("`") % 2 == 0


def test_injected_link_and_hashtag_cannot_be_auto_linked():
    # A crafted receipt image could carry a URL the model transcribes verbatim. Inside
    # a code span it stays inert, so the bot never renders a live attacker-supplied
    # link in a message it vouches for.
    receipt = _receipt(
        vendor_name="PayPal verify at https://evil.invalid",
        category="#urgent @admin",
    )
    message = success_summary(receipt, "PHP")
    for value in ("https://evil.invalid", "#urgent @admin"):
        # Present verbatim, and enclosed by the code span rather than loose in the text.
        start = message.index(value)
        assert message.rindex("`", 0, start) < start
        assert message.index("`", start + len(value)) >= start + len(value)


def test_success_summary_neutralizes_formatting_and_forged_labels():
    # An embedded newline could otherwise forge an extra field line, and MarkdownV2
    # syntax could otherwise become real formatting.
    receipt = _receipt(vendor_name="_[Vendor]*\nTotal: 0.01", category="`Category`~test")
    message = success_summary(receipt, "PHP")
    # The embedded newline never survives to create a new, forged line.
    assert "\nTotal: 0.01" not in message
    # Inside a code span the value is literal, so MarkdownV2 syntax needs no escaping
    # and stays exactly as printed on the receipt.
    assert "`_[Vendor]* Total: 0.01`" in message
    # A backtick in the value would otherwise close the span early, so it is escaped.
    assert "`\\`Category\\`~test`" in message
    # The real total, not the injected one, is the only "Total" figure shown.
    assert "*Total*: `PHP 1,280.00`" in message


def test_remembered_summary_includes_success_card_and_memory_note():
    message = remembered_summary(_receipt(), "PHP")
    assert message.startswith(success_summary(_receipt(), "PHP"))
    assert "Saved category memory for `ABC Hardware`: `Maintenance`\\." in message


def test_worst_case_escaping_still_fits_telegram_message_limit():
    # Every reserved character doubles in length once escaped. Verify that even an
    # all-reserved-character vendor name past the 300-char display_value truncation
    # point, plus a category at the schema's 100-character ceiling, still leaves both
    # summaries under Telegram's 4096-character send_message cap.
    vendor = RESERVED * 30  # 570 characters, truncated to 300 by display_value
    category = (RESERVED * 6)[:100]
    receipt = _receipt(vendor_name=vendor, category=category)
    assert len(success_summary(receipt, "PHP")) < 4096
    assert len(remembered_summary(receipt, "PHP")) < 4096


def test_markdown_v2_constant_matches_telegram_parse_mode_name():
    assert MARKDOWN_V2 == "MarkdownV2"
