"""Safe persistence errors; no database details in messages."""


class PersistenceError(Exception):
    code = "persistence_error"
    transient = False

    def __init__(self) -> None:
        super().__init__(self.code)


class PersistenceUnavailableError(PersistenceError):
    code = "persistence_unavailable"
    transient = True


class DuplicateReceiptPersistenceError(PersistenceError):
    code = "duplicate_receipt"


class PendingReceiptConflictError(PersistenceError):
    code = "pending_receipt_conflict"


class InvalidPersistenceResponseError(PersistenceError):
    code = "invalid_persistence_response"


class PendingExpiredError(PersistenceError):
    code = "pending_expired"


class AmbiguousPersistenceError(PersistenceUnavailableError):
    code = "persistence_ambiguous"
