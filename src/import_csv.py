"""Skeleton CSV importer for legacy ndWeb Export migration.

This is a SKELETON, not a production tool. Per CLAUDE.md:
  - Schema below is INFERRED from NetDocuments metadata conventions; it must
    be confirmed against the firm's actual ndWeb Export output during a real
    migration. The column names and shapes are best-guess.
  - No error handling for malformed rows. A bad row will raise and stop.
  - No duplicate detection. Re-running the importer creates a second copy
    of every document.
  - No progress reporting beyond per-row prints. No resumable checkpoints.
  - No idempotency. Run-it-twice = duplicated state.
  - No metadata-type schema migration. If the doc_type already has the
    required metadata fields, fine; if not, the importer adds them as
    OPTIONAL. It will not alter existing required/optional state.

Pipeline:
  1. Authenticate as admin via API token.
  2. Locate (or create) the "Legal Matter Document" document type and the
     8 metadata types referenced in the CSV.
  3. For each CSV row: upload the referenced PDF, then attach every
     metadata key as a metadata value.
  4. After all rows are processed, fire the index rebuild ONCE — the
     same deferred-rebuild pattern Day 1 used to avoid "None" orphan
     nodes from incomplete metadata state.

CSV schema (INFERRED, see CLAUDE.md):
  nd_doc_id        — NetDocuments document identifier (string, placeholder format)
  client_id        — short client code, e.g. ACME-CORP
  matter_id        — matter identifier under that client, e.g. 2025-001
  doc_type         — document classification (correspondence, pleadings, invoice, ...)
  author           — original creator name
  created_date     — ISO 8601 timestamp
  modified_date    — ISO 8601 timestamp
  filename         — file to ingest, resolved relative to ../samples/
  mime_type        — MIME of the file

The first 3 fields drive the matter index tree (same as Day 1). The
remaining 5 are stored as document metadata but do NOT influence the tree.
"""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

import requests

BASE_URL = 'http://127.0.0.1:8080'
API = f'{BASE_URL}/api/v4'
USERNAME = 'admin'
PASSWORD = 'pocadminpass'

SAMPLES = Path(__file__).resolve().parent.parent / 'samples'
CSV_PATH = Path(__file__).resolve().parent.parent / 'samples_csv' / 'legacy_export.csv'

# Day 1 document type, reused.
DOC_TYPE_LABEL = 'Legal Matter Document'

# Metadata types referenced in the CSV. The first 3 already exist from Day 1
# (created in setup_matter_tree.py); the others are added here as OPTIONAL
# so the doc type's required-field contract isn't changed.
METADATA_TYPES = [
    {'name': 'client_id', 'label': 'Client ID', 'required': True},
    {'name': 'matter_id', 'label': 'Matter ID', 'required': True},
    {'name': 'doc_type', 'label': 'Document type', 'required': True},
    {'name': 'nd_doc_id', 'label': 'ndWeb document id', 'required': False},
    {'name': 'author', 'label': 'Original author', 'required': False},
    {'name': 'created_date', 'label': 'Created date (legacy)', 'required': False},
    {'name': 'modified_date', 'label': 'Modified date (legacy)', 'required': False},
    {'name': 'mime_type', 'label': 'MIME type', 'required': False},
]


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


def find_or_create_metadata_type(session, spec):
    """Return the metadata_type id, creating if not present."""
    url = f'{API}/metadata_types/'
    while url:
        r = _request(session, 'GET', url)
        r.raise_for_status()
        page = r.json()
        for item in page.get('results', []):
            if item.get('name') == spec['name']:
                return item['id']
        url = page.get('next')
    r = _request(session, 'POST', f'{API}/metadata_types/',
                 json={'name': spec['name'], 'label': spec['label']})
    r.raise_for_status()
    return r.json()['id']


def find_or_create_doc_type(session):
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


def attach_metadata_type_if_missing(session, doc_type_id, mt_id, required):
    """Attach a metadata type to the doc type if not already attached.
    Skeleton limitation: does not alter `required` on an existing attachment."""
    path = f'/document_types/{doc_type_id}/metadata_types/'
    url = f'{API}{path}'
    while url:
        r = _request(session, 'GET', url)
        r.raise_for_status()
        page = r.json()
        for item in page.get('results', []):
            mt_obj = item.get('metadata_type') or {}
            if mt_obj.get('id') == mt_id:
                return
        url = page.get('next')
    r = _request(session, 'POST', f'{API}{path}',
                 json={'metadata_type_id': mt_id, 'required': required})
    if not r.ok:
        print(f'  [warn] attach mt {mt_id} required={required}: '
              f'{r.status_code} {r.text[:160]}')


def find_matters_index_id(session):
    """Return the IndexTemplate id for the Day 1 "Matters" index, or None."""
    url = f'{API}/index_templates/'
    while url:
        r = _request(session, 'GET', url)
        r.raise_for_status()
        page = r.json()
        for item in page.get('results', []):
            if item.get('label') == 'Matters':
                return item['id']
        url = page.get('next')
    return None


def upload_row(session, row, doc_type_id, mt_ids):
    sample = SAMPLES / row['filename']
    if not sample.exists():
        raise FileNotFoundError(f'CSV references missing file: {sample}')

    with sample.open('rb') as fh:
        files = {'file': (sample.name, fh, row['mime_type'])}
        data = {
            'document_type_id': doc_type_id,
            'label': f"{row['nd_doc_id']} - {row['client_id']}/{row['matter_id']}/{row['doc_type']}",
        }
        r = _request(session, 'POST', f'{API}/documents/upload/',
                     data=data, files=files)
    if not r.ok:
        raise RuntimeError(
            f"upload failed for {row['nd_doc_id']}: {r.status_code} {r.text[:200]}"
        )
    doc_id = r.json()['id']

    # Attach every metadata key the CSV row carries.
    metadata_keys = [
        'client_id', 'matter_id', 'doc_type', 'nd_doc_id',
        'author', 'created_date', 'modified_date', 'mime_type',
    ]
    for key in metadata_keys:
        value = row.get(key, '') or ''
        if not value:
            continue
        mid = mt_ids[key]
        r = _request(session, 'POST', f'{API}/documents/{doc_id}/metadata/',
                     json={'metadata_type_id': mid, 'value': value})
        if r.status_code not in (200, 201):
            print(f'  [warn] {row["nd_doc_id"]} metadata {key}={value!r}: '
                  f'{r.status_code} {r.text[:160]}')
    return doc_id


def main() -> int:
    if not CSV_PATH.exists():
        print(f'[error] CSV not found: {CSV_PATH}')
        return 1
    rows = list(csv.DictReader(CSV_PATH.open(encoding='utf-8')))
    print(f'[1/5] Read {len(rows)} rows from {CSV_PATH.name}')

    session = requests.Session()
    session.headers['Accept'] = 'application/json'
    token = get_token(session)
    session.headers['Authorization'] = f'Token {token}'
    print(f'[2/5] Authenticated as {USERNAME}')

    print('[3/5] Ensuring metadata types + document type + attachments')
    mt_ids = {}
    for spec in METADATA_TYPES:
        mt_ids[spec['name']] = find_or_create_metadata_type(session, spec)
    doc_type_id = find_or_create_doc_type(session)
    for spec in METADATA_TYPES:
        attach_metadata_type_if_missing(
            session, doc_type_id, mt_ids[spec['name']], spec['required']
        )
    print(f'  doc_type_id={doc_type_id}, metadata_type_ids={mt_ids}')

    print(f'[4/5] Uploading {len(rows)} rows + setting metadata')
    uploaded = []
    for row in rows:
        doc_id = upload_row(session, row, doc_type_id, mt_ids)
        uploaded.append((row['nd_doc_id'], doc_id))
        print(f'  [ok] {row["nd_doc_id"]} -> document id {doc_id} '
              f'({row["client_id"]}/{row["matter_id"]}/{row["doc_type"]})')

    print('[5/5] Triggering index rebuild (deferred — Day 1 pattern)')
    index_id = find_matters_index_id(session)
    if index_id is None:
        print('  [skip] "Matters" IndexTemplate not present; run setup_matter_tree.py first')
    else:
        r = _request(session, 'POST', f'{API}/index_templates/{index_id}/rebuild/')
        if r.status_code in (200, 202, 204):
            print(f'  rebuild queued on index template {index_id}')
        else:
            print(f'  [warn] rebuild: {r.status_code} {r.text[:160]}')

    print()
    print(f'Imported {len(uploaded)}/{len(rows)} rows.')
    print('Re-run verify_tree.py once Celery settles to see the expanded tree.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
