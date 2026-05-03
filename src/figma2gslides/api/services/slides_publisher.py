"""Upload PPTX bytes to Google Drive as a Google Slides presentation."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:  # pragma: no cover - optional dependency
    from google.oauth2.credentials import Credentials

    from svg2ooxml.gsuite import (
        PPTX_MIME,
        SLIDES_MIME,
        GSuiteClient,
        GSuiteError,
    )

    _GOOGLE_AVAILABLE = True
except ImportError:  # pragma: no cover
    _GOOGLE_AVAILABLE = False


def upload_to_google_slides(
    pptx_bytes: bytes,
    access_token: str,
    *,
    title: str = "Untitled",
) -> str:
    """Upload *pptx_bytes* to Google Drive, converting to Google Slides.

    Returns the URL of the created presentation.
    """
    if not _GOOGLE_AVAILABLE:
        raise RuntimeError("Google API client libraries are not installed.")

    creds = Credentials(token=access_token)
    client = GSuiteClient(creds)

    try:
        file_id = client.upload(
            pptx_bytes,
            mime_type=PPTX_MIME,
            target_mime=SLIDES_MIME,
            name=title,
        )
    except GSuiteError as exc:
        raise RuntimeError(f"Google Slides upload failed: {exc}") from exc

    logger.info("Uploaded presentation %s as Google Slides (%s)", title, file_id)
    return f"https://docs.google.com/presentation/d/{file_id}/edit"


__all__ = ["upload_to_google_slides"]
