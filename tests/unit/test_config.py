import pytest
from pydantic import ValidationError

from second_brain_receipts.core.config import Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def test_defaults():
    settings = Settings(app_env="test")
    assert Settings.model_fields["app_env"].default == "development"
    assert settings.log_level == "INFO"
    assert settings.openai_model == "gpt-4o"
    assert settings.default_currency == "PHP"
    assert settings.pending_receipt_ttl_minutes == 30
    assert settings.max_upload_bytes == 10 * 1024 * 1024
    assert settings.openai_api_key is None


def test_environment_parsing(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "2048")
    monkeypatch.setenv("PENDING_RECEIPT_TTL_MINUTES", "10")
    monkeypatch.setenv("DEFAULT_CURRENCY", "USD")
    settings = Settings()
    assert settings.max_upload_bytes == 2048
    assert settings.pending_receipt_ttl_minutes == 10
    assert settings.default_currency == "USD"


def test_dotenv_loading_and_environment_precedence(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("APP_ENV=test\nMAX_UPLOAD_BYTES=4096\n")
    assert Settings().max_upload_bytes == 4096
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "8192")
    assert Settings().max_upload_bytes == 8192


@pytest.mark.parametrize("environment", ["development", "production"])
def test_runtime_requires_credentials(environment):
    with pytest.raises(ValidationError, match="missing runtime settings"):
        Settings(app_env=environment)


def test_fake_runtime_credentials_are_masked():
    fake = "test-only-not-a-real-secret"
    settings = Settings(
        openai_api_key=fake,
        telegram_bot_token=fake,
        telegram_allowed_user_id=123,
        telegram_webhook_secret=fake,
        supabase_service_role_key=fake,
        supabase_url="https://example.invalid",
        aws_region="ap-southeast-1",
        s3_bucket_name="test-bucket",
    )
    assert settings.app_env == "development"
    assert fake not in repr(settings)
    assert fake not in settings.model_dump_json()


@pytest.mark.parametrize(
    "field",
    [
        "openai_api_key",
        "telegram_bot_token",
        "telegram_webhook_secret",
        "supabase_service_role_key",
    ],
)
def test_blank_secrets_rejected_even_in_tests(field):
    with pytest.raises(ValidationError, match="secret must not be blank"):
        Settings(app_env="test", **{field: "   "})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_env", "unknown"),
        ("log_level", "verbose"),
        ("max_upload_bytes", 0),
        ("pending_receipt_ttl_minutes", -1),
        ("max_upload_bytes", "invalid"),
        ("default_currency", "php"),
        ("default_currency", "PESO"),
        ("openai_model", "  "),
        ("s3_bucket_name", " "),
        ("supabase_url", "not-a-url"),
    ],
)
def test_invalid_settings(field, value):
    with pytest.raises(ValidationError):
        Settings(**{"app_env": "test", field: value})


def test_validation_error_text_hides_inputs():
    with pytest.raises(ValidationError) as exc:
        Settings(openai_api_key="fake-secret-must-not-appear")
    assert "fake-secret-must-not-appear" not in str(exc.value)


def test_image_defaults_and_environment(monkeypatch):
    settings = Settings(app_env="test")
    assert settings.image_max_long_edge == 1600
    assert settings.image_jpeg_quality == 85
    assert settings.image_max_pixels == 40_000_000
    monkeypatch.setenv("IMAGE_MAX_LONG_EDGE", "1800")
    monkeypatch.setenv("IMAGE_JPEG_QUALITY", "82")
    monkeypatch.setenv("IMAGE_MAX_PIXELS", "24000000")
    settings = Settings(app_env="test")
    assert (
        settings.image_max_long_edge,
        settings.image_jpeg_quality,
        settings.image_max_pixels,
    ) == (1800, 82, 24_000_000)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_max_long_edge", 0),
        ("image_max_long_edge", 4097),
        ("image_jpeg_quality", 74),
        ("image_jpeg_quality", 96),
        ("image_max_pixels", 0),
        ("image_max_pixels", 80_000_001),
    ],
)
def test_invalid_image_settings(field, value):
    with pytest.raises(ValidationError):
        Settings(app_env="test", **{field: value})


def test_openai_timeout_default_and_environment(monkeypatch):
    assert Settings(app_env="test").openai_timeout_seconds == 30
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "12.5")
    assert Settings(app_env="test").openai_timeout_seconds == 12.5


@pytest.mark.parametrize("timeout", [0, -1, 121, "NaN", "Infinity"])
def test_invalid_openai_timeout(timeout):
    with pytest.raises(ValidationError):
        Settings(app_env="test", openai_timeout_seconds=timeout)


def test_s3_defaults_and_chain(monkeypatch):
    settings = Settings(app_env="test")
    assert settings.s3_presigned_url_ttl_seconds == 300
    assert settings.aws_access_key_id is None and settings.aws_secret_access_key is None
    assert "supabase_storage_bucket" not in Settings.model_fields
    monkeypatch.setenv("AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("S3_BUCKET_NAME", "private-receipts")
    monkeypatch.setenv("S3_PRESIGNED_URL_TTL_SECONDS", "60")
    settings = Settings(app_env="test")
    assert settings.aws_region == "ap-southeast-1" and settings.s3_bucket_name == "private-receipts"
    assert settings.s3_presigned_url_ttl_seconds == 60


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("s3_bucket_name", ""),
        ("s3_bucket_name", "bad bucket"),
        ("s3_bucket_name", "127.0.0.1"),
        ("s3_bucket_name", "bad..bucket"),
        ("aws_region", ""),
        ("s3_presigned_url_ttl_seconds", 0),
        ("s3_presigned_url_ttl_seconds", 3601),
        ("aws_access_key_id", "one-key-only"),
        ("aws_secret_access_key", "secret-only"),
        ("aws_session_token", "token-only"),
        ("aws_secret_access_key", " "),
    ],
)
def test_invalid_s3_settings(field, value):
    with pytest.raises(ValidationError):
        Settings(app_env="test", **{field: value})


def test_aws_credentials_masked():
    secret = "fake-aws-value"
    settings = Settings(
        app_env="test",
        aws_access_key_id=secret,
        aws_secret_access_key=secret,
        aws_session_token=secret,
    )
    assert secret not in repr(settings) and secret not in settings.model_dump_json()
