"""Mayan file_metadata driver: surface body, date, attachments from .msg files.

Phase 1 notes (`/research/mayan/notes.md`) identified that the in-tree driver
at `mayan/apps/file_metadata_msg/drivers.py:25` reads only cc/sender/subject/to
off extract-msg, while the library itself exposes body/htmlBody/date/attachments
and more. This driver covers the gap.

Registration: Mayan's `FileMetadataDriverMetaclass`
(`mayan/apps/file_metadata/classes.py:72`) auto-registers any subclass of
`FileMetadataDriver` discovered via `AppsModuleLoaderMixin` walking
`<app>/drivers.py` modules. Putting this class here + listing the app in
`MAYAN_COMMON_EXTRA_APPS` is the documented Mayan extension pattern.

This driver runs ALONGSIDE the in-tree driver (different `internal_name`),
not as a replacement. The in-tree driver continues to surface its four fields;
this driver adds three more keys to the document's file metadata.

HTML sanitization: email HTML is treated as hostile. When `message.htmlBody`
is the only available body, it is passed through `nh3.clean()` with a tight
whitelist (no scripts, no event handlers, no styles, no external resources)
before being collapsed to plain text. nh3 is the Rust-backed sanitizer
already present in the Mayan container (replaces bleach as of nh3 0.3.x).
"""

from __future__ import annotations

import logging
import re

from django.utils.translation import gettext_lazy as _

from mayan.apps.file_metadata.classes import FileMetadataDriver
from mayan.apps.storage.literals import MSG_MIME_TYPES

logger = logging.getLogger(__name__)

# nh3 is bundled with the Mayan image; the fallback path keeps the driver
# operational if the dep is ever dropped from the base image.
try:
    import nh3
    _HAVE_NH3 = True
except ImportError:
    _HAVE_NH3 = False
    logger.warning(
        'nh3 not available; HTML bodies will be stripped via regex fallback. '
        'Install nh3 in the Mayan image to enable whitelist-based sanitization.'
    )


_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')

_NH3_TAG_WHITELIST = {
    'a', 'b', 'br', 'em', 'i', 'li', 'ol', 'p', 'strong', 'ul',
}
_NH3_ATTRIBUTE_WHITELIST = {'a': {'href'}}

# Cap stored metadata body at this length. Mayan's metadata table stores text;
# multi-MB HTML email bodies would balloon the file_metadata index. 16 KB is
# enough for full-text matching the body of any realistic email.
_BODY_MAX_LENGTH = 16 * 1024


def _sanitize_html(html: str) -> str:
    """Run hostile HTML through a tight whitelist and collapse to text.

    Removes scripts, styles, event handlers, external resources, and all
    tags outside `_NH3_TAG_WHITELIST`. Then strips remaining tags so the
    stored value is plain text suitable for Mayan's metadata field.
    """
    if not html:
        return ''
    if _HAVE_NH3:
        cleaned = nh3.clean(
            html,
            tags=_NH3_TAG_WHITELIST,
            attributes=_NH3_ATTRIBUTE_WHITELIST,
            strip_comments=True,
        )
    else:
        cleaned = html
    # Collapse to plain text — Mayan renders metadata fields as text, not HTML.
    text = _TAG_RE.sub(' ', cleaned)
    text = _WS_RE.sub(' ', text).strip()
    return text


def _resolve_body(message) -> str:
    """Return a plain-text body. Prefer the message's plain-text body;
    fall back to sanitizing htmlBody when only HTML is present."""
    body = getattr(message, 'body', None)
    if body:
        # extract-msg returns bytes for some encodings; normalize to str.
        if isinstance(body, bytes):
            try:
                body = body.decode('utf-8', errors='replace')
            except Exception:
                body = body.decode('latin-1', errors='replace')
        return body

    html_body = getattr(message, 'htmlBody', None)
    if html_body:
        if isinstance(html_body, bytes):
            try:
                html_body = html_body.decode('utf-8', errors='replace')
            except Exception:
                html_body = html_body.decode('latin-1', errors='replace')
        return _sanitize_html(html_body)

    return ''


def _resolve_date(message) -> str:
    """ISO-8601 date string, or empty if not present."""
    date = getattr(message, 'date', None)
    if not date:
        return ''
    # extract-msg returns either a string or a datetime depending on version.
    if hasattr(date, 'isoformat'):
        try:
            return date.isoformat()
        except Exception:
            return str(date)
    return str(date)


def _resolve_attachments(message):
    """Return (count_str, names_csv).

    Mayan's metadata field is a string; the count goes into one key, the
    comma-joined long names into another. This is enough for indexing and
    matter-tree exposure without forcing us to model attachments as a
    separate relation in this POC.
    """
    attachments = getattr(message, 'attachments', None) or []
    names = []
    for att in attachments:
        # extract-msg attachment objects have `longFilename` / `shortFilename`.
        name = getattr(att, 'longFilename', None) or getattr(att, 'shortFilename', None)
        if name:
            names.append(str(name))
    return str(len(attachments)), ', '.join(names)


class FileMetadataDriverExtractMSGToolExtended(FileMetadataDriver):
    description = _(
        message='Surfaces .msg body, date, and attachment metadata not '
        'covered by the in-tree extract_msg driver. HTML bodies are '
        'sanitized via nh3 whitelist.'
    )
    internal_name = 'extract_msg_extended'
    label = _(message='Extract msg (extended)')
    mime_type_list = list(MSG_MIME_TYPES)

    def _process(self, document_file):
        import extract_msg
        message = extract_msg.Message(
            path=document_file.open()
        )

        result = {}

        body = _resolve_body(message)
        if body:
            if len(body) > _BODY_MAX_LENGTH:
                body = body[:_BODY_MAX_LENGTH] + '... [truncated]'
            result['body'] = body

        date = _resolve_date(message)
        if date:
            result['date'] = date

        count, names = _resolve_attachments(message)
        result['attachments_count'] = count
        if names:
            result['attachments'] = names

        logger.debug('extended .msg metadata: keys=%s', list(result.keys()))
        return result
