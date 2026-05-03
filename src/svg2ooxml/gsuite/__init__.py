"""Google Workspace integration for svg2ooxml.

Thin Drive API wrapper that uploads OOXML files, optionally converts
them to native Google formats (Slides/Docs/Sheets), exports back to
OOXML, and cleans up — the four primitives any Workspace integration
needs.

Auth model: service account with domain-wide delegation. Service
accounts skip Google's OAuth consent-screen verification, so this
works headlessly in CI and tooling without a per-user browser flow.

The default credentials path and impersonation env var match
`openxml-audit` so a single Workspace setup serves both repos:

  - Creds:   ``~/.config/openxml-audit/google_service_account.json``
             (override via ``GSUITE_ORACLE_CREDS``)
  - Subject: ``GSUITE_ORACLE_SUBJECT`` (the Workspace user the SA
             impersonates)

Optional dependency: ``pip install -e ".[gsuite]"``.
"""

from svg2ooxml.gsuite.client import (
    DEFAULT_CREDS_PATH,
    DEFAULT_SCOPES,
    DOC_MIME,
    DOCX_MIME,
    PPTX_MIME,
    SHEET_MIME,
    SLIDES_MIME,
    XLSX_MIME,
    GSuiteAuthError,
    GSuiteClient,
    GSuiteError,
    native_mime_for,
    ooxml_mime_for,
)

__all__ = [
    "GSuiteClient",
    "GSuiteAuthError",
    "GSuiteError",
    "PPTX_MIME",
    "DOCX_MIME",
    "XLSX_MIME",
    "SLIDES_MIME",
    "DOC_MIME",
    "SHEET_MIME",
    "DEFAULT_CREDS_PATH",
    "DEFAULT_SCOPES",
    "native_mime_for",
    "ooxml_mime_for",
]
