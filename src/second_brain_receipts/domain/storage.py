"""Validation of stable S3 references, never bearer URLs."""

import ipaddress
import re
from urllib.parse import urlsplit


def validate_bucket_name(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value):
        raise ValueError("invalid S3 bucket name")
    if any(part in value for part in ("..", ".-", "-.")):
        raise ValueError("invalid S3 bucket name")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("invalid S3 bucket name")


def validate_storage_reference(key: str, uri: str) -> None:
    parts = urlsplit(uri)
    if (
        parts.scheme != "s3"
        or parts.query
        or parts.fragment
        or parts.path != f"/{key}"
        or not key
        or any(char.isspace() for char in key)
        or any(part in ("", ".", "..") for part in key.split("/"))
    ):
        raise ValueError("invalid stable S3 reference")
    validate_bucket_name(parts.netloc)
