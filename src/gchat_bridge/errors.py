"""Errors of the Chat API client, sorted by what the bridge does about them."""

from __future__ import annotations


class ConfigError(Exception):
    """A configuration error the bridge cannot recover from by retrying."""


class TransportError(Exception):
    """Google could not be reached or did not answer: reconnect with backoff.
    From a send it means the outcome is unknown (a send is safe to repeat: it
    carries a client-assigned message id)."""


class AuthError(Exception):
    """The credentials were refused: a configuration error."""


class ApiError(Exception):
    """Google answered with an error. From a send it means the message was not
    sent; the text is the reason."""

    def __init__(self, status: int, message: str, google_status: str | None = None) -> None:
        super().__init__(f"{status} {google_status or ''}: {message}".replace("  ", " "))
        self.status = status
        self.google_status = google_status


class SpaceAccessError(ApiError):
    """The account may not read or write the space (403/404 on it)."""
