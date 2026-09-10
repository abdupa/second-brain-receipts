"""Explicitly instantiated environment settings; importing this module has no I/O."""

from typing import Literal, Self

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from second_brain_receipts.domain.receipt_date import DateOrder
from second_brain_receipts.domain.storage import validate_bucket_name


class Settings(BaseSettings):
    """Runtime credentials are mandatory outside the isolated test environment."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="forbid", hide_input_in_errors=True, frozen=True
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    openai_model: str = Field(default="gpt-4o", min_length=1)
    openai_timeout_seconds: float = Field(default=30.0, gt=0, le=120, allow_inf_nan=False)
    openai_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_allowed_user_id: int | None = Field(default=None, gt=0, le=2**63 - 1)
    telegram_webhook_max_bytes: int = Field(default=65536, ge=1024, le=1048576)
    telegram_timeout_seconds: float = Field(default=10, gt=0, le=30, allow_inf_nan=False)
    supabase_url: HttpUrl | None = None
    supabase_service_role_key: SecretStr | None = None
    aws_region: str | None = Field(default=None, pattern=r"^[a-z]{2}(?:-[a-z]+)+-\d+$")
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_session_token: SecretStr | None = None
    s3_bucket_name: str | None = None
    s3_presigned_url_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    default_currency: str = Field(default="PHP", pattern=r"^[A-Z]{3}$")
    # How to read a printed date whose day and month are both 12 or less. Defaults to
    # day-first for consistency with the single configured locale: this application
    # already assumes one currency and presents every date as DD/MM/YYYY. Set
    # month_first for United States receipts, or auto to keep the model's own reading.
    receipt_date_order: DateOrder = "day_first"
    pending_receipt_ttl_minutes: int = Field(default=30, gt=0)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)

    image_max_long_edge: int = Field(default=1600, ge=128, le=4096)
    image_jpeg_quality: int = Field(default=85, ge=75, le=95)
    image_max_pixels: int = Field(default=40_000_000, ge=16_384, le=80_000_000)

    @field_validator("openai_model", mode="before")
    @classmethod
    def trim_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "openai_api_key",
        "telegram_bot_token",
        "telegram_webhook_secret",
        "supabase_service_role_key",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
    )
    @classmethod
    def reject_blank_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("secret must not be blank")
        return value

    @field_validator("s3_bucket_name")
    @classmethod
    def validate_bucket(cls, value: str | None) -> str | None:
        return validate_bucket_name(value) if value is not None else None

    @model_validator(mode="after")
    def require_runtime_configuration(self) -> Self:
        if (self.aws_access_key_id is None) != (self.aws_secret_access_key is None):
            raise ValueError("AWS access key and secret key must be supplied together")
        if self.aws_session_token is not None and self.aws_access_key_id is None:
            raise ValueError("explicit AWS session token requires an explicit credential pair")
        if self.app_env != "test":
            required = (
                "openai_api_key",
                "telegram_bot_token",
                "telegram_webhook_secret",
                "telegram_allowed_user_id",
                "supabase_url",
                "supabase_service_role_key",
                "aws_region",
                "s3_bucket_name",
            )
            missing = [name for name in required if getattr(self, name) is None]
            if missing:
                raise ValueError("missing runtime settings: " + ", ".join(missing))
        return self
