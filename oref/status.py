"""Status enum for tracking skill and transaction execution state."""

from enum import StrEnum


class Status(StrEnum):
    """Execution state of a skill or transaction.

    This tracks *where* something is in its lifecycle, not *why* it failed.
    Exception classification (business vs system) lives on the exception itself.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    SKIPPED = "skipped"
