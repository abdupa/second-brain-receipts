"""Free-form confirmed categories shared by Python and the completion RPC."""

from typing import Annotated

from pydantic import BeforeValidator, Field

MAX_CATEGORY_LENGTH = 100
# Reject C0/C1 controls and bidi/zero-width formatting controls, even at the edges.
FORBIDDEN = "".join(chr(i) for i in range(32)) + "".join(chr(i) for i in range(127, 160))
FORBIDDEN += (
    "\u061c\u200b\u200c\u200d\u200e\u200f\u2028\u2029\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2066\u2067\u2068\u2069\ufeff"
)


def clean_category(value: object) -> object:
    if isinstance(value, str):
        if any(char in FORBIDDEN for char in value):
            raise ValueError("category contains unsupported control characters")
        return value.strip()
    return value


def validate_category(value: str) -> str:
    from pydantic import TypeAdapter

    return TypeAdapter(Category).validate_python(value)


Category = Annotated[str, BeforeValidator(clean_category), Field(min_length=1, max_length=100)]
