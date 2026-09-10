"""Safe exceptions: never retain URLs, provider descriptions or response objects."""


class TelegramError(Exception):
    code = "telegram_error"
    transient = False

    def __init__(self) -> None:
        super().__init__(self.code)


class TelegramAuthenticationError(TelegramError):
    code = "telegram_authentication"


class TelegramRateLimitError(TelegramError):
    code = "telegram_rate_limit"
    transient = True

    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__()
        self.retry_after = retry_after


class TelegramTimeoutError(TelegramError):
    code = "telegram_timeout"
    transient = True


class TelegramUnavailableError(TelegramError):
    code = "telegram_unavailable"
    transient = True


class TelegramDownloadError(TelegramError):
    code = "telegram_download"


class TelegramResponseError(TelegramError):
    code = "telegram_response"


class TelegramRejectedError(TelegramUnavailableError):
    """A validated API response explicitly says ok=false; safe to retry."""

    code = "telegram_rejected"


class TelegramAmbiguousSendError(TelegramError):
    code = "telegram_send_ambiguous"
