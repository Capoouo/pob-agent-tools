"""Errors shared by the CLI and daemon request handlers."""


class DaemonRequestError(RuntimeError):
    """Raised when the PoB daemon rejects a request due to invalid arguments or build state."""
