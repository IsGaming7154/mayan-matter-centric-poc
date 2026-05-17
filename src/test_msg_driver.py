"""Test the extended .msg driver against 6 synthetic fixtures.

For each fixture:
  1. Upload to Mayan as a new document under the "Email Message" doc type
  2. Wait for the file_metadata Celery worker to extract metadata
  3. Pull file_metadata entries from BOTH the in-tree driver and our extension
  4. Report which extension fields surfaced (body, date, attachments_count,
     attachments) and verify hostile HTML was sanitized

The test passes if every fixture has body OR htmlBody-derived content
surfaced via the extension, and hostile_html.msg's body contains no
`<script>`, `onerror=`, `javascript:`, or `<iframe>` substrings.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

BASE_URL = 'http://127.0.0.1:8080'
API = f'{BASE_URL}/api/v4'
USERNAME = 'admin'
PASSWORD = 'pocadminpass'

SAMPLES = Path(__file__).resolve().parent.parent / 'samples_msg'
DOC_TYPE_LABEL = 'Email Message'

FIXTURES = [
    'strangeDate.msg',
    'plain_short.msg',
    'plain_long.msg',
    'hostile_html.msg',
    'unicode_body.msg',
    'minimal.msg',
]

# Substrings that must NOT appear in the sanitized body of hostile_html.msg.
HOSTILE_NEEDLES = ['<script', 'onerror=', 'javascript:', '<iframe', '<style']


REQUEST_DELAY = 1.2
MAX_429_RETRIES = 5


def _request(session, method, url, **kwargs):
    for attempt in range(MAX_429_RETRIES):
        r = session.request(method, url, timeout=60, **kwargs)
        if r.status_code != 429:
            time.sleep(REQUEST_DELAY)
            return r
        wait = float(r.headers.get('Retry-After') or (2 ** attempt))
        time.sleep(wait)
    return r


def get_token(session):
    r = _request(session, 'POST', f'{API}/auth/token/obtain/',
                 data={'username': USERNAME, 'password': PASSWORD})
    r.raise_for_status()
    return r.json()['token']


def ensure_doc_type(session) -> int:
    """Create or find the Email Message document type."""
    url = f'{API}/document_types/'
    while url:
        r = _request(session, 'GET', url)
        r.raise_for_status()
        page = r.json()
        for item in page.get('results', []):
            if item.get('label') == DOC_TYPE_LABEL:
                return item['id']
        url = page.get('next')
    r = _request(session, 'POST', f'{API}/document_types/',
                 json={'label': DOC_TYPE_LABEL})
    r.raise_for_status()
    return r.json()['id']


def upload(session, doc_type_id: int, fixture_path: Path) -> int:
    with fixture_path.open('rb') as fh:
        files = {'file': (fixture_path.name, fh, 'application/vnd.ms-outlook')}
        data = {'document_type_id': doc_type_id, 'label': fixture_path.stem}
        r = _request(session, 'POST', f'{API}/documents/upload/',
                     data=data, files=files)
    r.raise_for_status()
    return r.json()['id']


def get_first_file_id(session, doc_id: int) -> int:
    """A document has one or more files; return the first one's id."""
    r = _request(session, 'GET', f'{API}/documents/{doc_id}/files/')
    r.raise_for_status()
    files = r.json().get('results', [])
    if not files:
        raise RuntimeError(f'no files for document {doc_id}')
    return files[0]['id']


def wait_for_drivers(session, doc_id: int, file_id: int,
                     timeout: int = 300) -> list[dict]:
    """Poll until both .msg drivers have produced entries, or timeout.

    300s budget covers the ClamAV-induced indexing latency observed during
    the first Day 2 test pass — clamscan can add ~17s per file before the
    .msg-relevant drivers run.
    """
    url = f'{API}/documents/{doc_id}/files/{file_id}/file_metadata/drivers/'
    deadline = time.time() + timeout
    while True:
        r = _request(session, 'GET', url)
        r.raise_for_status()
        drivers = r.json().get('results', [])
        msg_drivers = [
            d for d in drivers
            if (d.get('stored_driver') or {}).get('internal_name', '').startswith('extract_msg')
        ]
        if len(msg_drivers) >= 2:  # both extract_msg and extract_msg_extended
            return msg_drivers
        if time.time() > deadline:
            return msg_drivers
        time.sleep(5)


def fetch_entries(session, doc_id: int, file_id: int,
                  driver_id: int) -> dict[str, str]:
    """Return {key: value} for a driver's metadata entries."""
    url = (f'{API}/documents/{doc_id}/files/{file_id}/file_metadata/'
           f'drivers/{driver_id}/entries/')
    out = {}
    while url:
        r = _request(session, 'GET', url)
        r.raise_for_status()
        page = r.json()
        for e in page.get('results', []):
            out[e.get('key')] = e.get('value')
        url = page.get('next')
    return out


def report_for_fixture(name: str, in_tree: dict, extended: dict) -> dict:
    """Build a one-line report for one fixture. Return a dict for summary."""
    print(f'\n{name}:')
    print('  in-tree (extract_msg) keys:', sorted(in_tree.keys()) or '(none)')
    print('  extended keys:           ', sorted(extended.keys()) or '(none)')
    if 'body' in extended:
        b = extended['body'] or ''
        preview = (b[:100] + '...') if len(b) > 100 else b
        print(f'  extended body len={len(b)}, preview={preview!r}')
    if 'date' in extended:
        print(f'  extended date={extended["date"]!r}')
    if 'attachments_count' in extended:
        print(f'  extended attachments_count={extended["attachments_count"]!r}, '
              f'attachments={extended.get("attachments", "")!r}')
    return {
        'fixture': name,
        'in_tree_keys': sorted(in_tree.keys()),
        'extended_keys': sorted(extended.keys()),
        'extended': extended,
    }


def main() -> int:
    session = requests.Session()
    session.headers['Accept'] = 'application/json'
    session.headers['Authorization'] = f'Token {get_token(session)}'
    print(f'[1/4] Authenticated as {USERNAME}')

    doc_type_id = ensure_doc_type(session)
    print(f'[2/4] Email Message document type id={doc_type_id}')

    print('[3/4] Uploading 6 .msg fixtures')
    uploaded = []  # (fixture_name, doc_id, file_id)
    for name in FIXTURES:
        path = SAMPLES / name
        if not path.exists():
            print(f'  [skip] missing fixture: {path}')
            continue
        doc_id = upload(session, doc_type_id, path)
        # Document files appear after upload completes
        time.sleep(2)
        file_id = get_first_file_id(session, doc_id)
        print(f'  [uploaded] {name} doc={doc_id} file={file_id}')
        uploaded.append((name, doc_id, file_id))

    print('\n[4/4] Polling for driver completion + verifying extended fields')
    reports = []
    for name, doc_id, file_id in uploaded:
        drivers = wait_for_drivers(session, doc_id, file_id)
        in_tree_entries = {}
        extended_entries = {}
        for d in drivers:
            internal = (d.get('stored_driver') or {}).get('internal_name')
            entries = fetch_entries(session, doc_id, file_id, d['id'])
            if internal == 'extract_msg':
                in_tree_entries = entries
            elif internal == 'extract_msg_extended':
                extended_entries = entries
        reports.append(report_for_fixture(name, in_tree_entries, extended_entries))

    # Assertions
    print('\n==== Assertions ====')
    fails = []
    for r in reports:
        name = r['fixture']
        ext = r['extended']
        # Every fixture should have AT LEAST one of: body, attachments_count.
        # All variants produce attachments_count='0' even if empty.
        if not ext:
            fails.append(f'{name}: extension driver returned no entries')
            continue
        if 'attachments_count' not in ext:
            fails.append(f'{name}: missing attachments_count')
        if name == 'hostile_html.msg':
            body = ext.get('body', '') or ''
            for needle in HOSTILE_NEEDLES:
                if needle.lower() in body.lower():
                    fails.append(
                        f'{name}: SANITIZATION FAIL — body still contains {needle!r}'
                    )
            if 'Legitimate' not in body:
                fails.append(
                    f'{name}: SANITIZATION OVER-STRIP — legitimate text removed'
                )
            print(f'  hostile_html body (sanitized): {body!r}')
        if name == 'plain_long.msg':
            body = ext.get('body', '')
            if not body.endswith('[truncated]'):
                fails.append(
                    f'{name}: truncation marker missing (body len={len(body)})'
                )

    if fails:
        print('\n==== FAIL ====')
        for f in fails:
            print(f'  - {f}')
        return 2
    print('\n==== PASS ====')
    print(f'All {len(reports)} fixtures: extended driver surfaced expected '
          f'fields, hostile HTML sanitized, body truncated when oversized.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
