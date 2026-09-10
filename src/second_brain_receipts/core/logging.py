"""Application startup logging policy for secret-bearing provider traffic."""

import logging

from second_brain_receipts.core.config import Settings


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(level=settings.log_level)
    logging.getLogger("second_brain_receipts").setLevel(settings.log_level)
    # HTTPX INFO URLs contain PostgREST financial filters; SDK DEBUG can contain
    # messages, images and signed requests. Do not enable these in deployment.
    for name in (
        "httpx",
        "httpx2",
        "httpcore",
        "openai",
        "supabase",
        "postgrest",
        "boto3",
        "botocore",
        "s3transfer",
        "PIL",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)
