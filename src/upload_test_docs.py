"""Upload the 3 synthetic PDFs to Mayan with matter-relevant metadata.

Each document gets client_id / matter_id / doc_type metadata set. The
IndexTemplate configured by setup_matter_tree.py should then auto-populate
the index instance tree:

    Matters/
      ACME-CORP/
        2025-001/
          correspondence/
            acme-2025-001-correspondence.pdf
          pleadings/
            acme-2025-001-pleadings.pdf
      WAYNE-ENT/
        2025-007/
          correspondence/
            wayne-2025-007-correspondence.pdf

API surface used (paths verified against
/.sources/mayan-edms/mayan/apps/documents/urls.py line 522 and metadata/urls.py):
  POST /api/v4/documents/upload/                       multipart upload + document_type_id
  POST /api/v4/documents/{id}/metadata/                attach a single metadata value

State read from ./state.json (written by setup_matter_tree.py).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

BASE_URL = "http://127.0.0.1:8080"
API = f"{BASE_URL}/api/v4"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "pocadminpass"

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
STATE_FILE = Path(__file__).parent / "state.json"

DOCS = [
    {
        "file": "acme-2025-001-correspondence.pdf",
        "label": "Acme 2025-001 correspondence",
        "client_id": "ACME-CORP",
        "matter_id": "2025-001",
        "doc_type": "correspondence",
    },
    {
        "file": "acme-2025-001-pleadings.pdf",
        "label": "Acme 2025-001 pleadings",
        "client_id": "ACME-CORP",
        "matter_id": "2025-001",
        "doc_type": "pleadings",
    },
    {
        "file": "wayne-2025-007-correspondence.pdf",
        "label": "Wayne 2025-007 correspondence",
        "client_id": "WAYNE-ENT",
        "matter_id": "2025-007",
        "doc_type": "correspondence",
    },
]


REQUEST_DELAY = 1.2
MAX_429_RETRIES = 5


def _request(session, method, url, **kwargs):
    for attempt in range(MAX_429_RETRIES):
        r = session.request(method, url, timeout=60, **kwargs)
        if r.status_code != 429:
            time.sleep(REQUEST_DELAY)
            return r
        retry_after = r.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else (2 ** attempt)
        print(f"  [throttled] sleeping {wait:.1f}s (attempt {attempt + 1}/{MAX_429_RETRIES})")
        time.sleep(wait)
    return r


def get_token(session: requests.Session) -> str:
    r = _request(session, "POST", f"{API}/auth/token/obtain/",
                 data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    r.raise_for_status()
    return r.json()["token"]


def upload(session, doc_meta, doc_type_id, metadata_type_ids):
    sample = SAMPLES_DIR / doc_meta["file"]
    if not sample.exists():
        print(f"  [error] missing sample: {sample}")
        return None

    with sample.open("rb") as fh:
        files = {"file": (doc_meta["file"], fh, "application/pdf")}
        data = {"document_type_id": doc_type_id, "label": doc_meta["label"]}
        r = _request(session, "POST", f"{API}/documents/upload/", data=data, files=files)
    if not r.ok:
        print(f"  [error] upload {doc_meta['file']}: {r.status_code} {r.text[:300]}")
        return None
    doc = r.json()
    doc_id = doc.get("id") or doc.get("document_id")
    print(f"  [uploaded] {doc_meta['file']} -> document id {doc_id}")

    # Attach the three metadata values.
    for key in ("client_id", "matter_id", "doc_type"):
        mid = metadata_type_ids[key]
        r = _request(session, "POST", f"{API}/documents/{doc_id}/metadata/",
                     json={"metadata_type_id": mid, "value": doc_meta[key]})
        if r.status_code not in (200, 201):
            print(
                f"  [warn] metadata {key}={doc_meta[key]} on doc {doc_id}: "
                f"{r.status_code} {r.text[:200]}"
            )
        else:
            print(f"    metadata {key}={doc_meta[key]} set")
    return doc_id


def main() -> int:
    if not STATE_FILE.exists():
        print("[error] state.json not found — run setup_matter_tree.py first")
        return 1
    state = json.loads(STATE_FILE.read_text())
    doc_type_id = state["document_type_id"]
    metadata_type_ids = state["metadata_type_ids"]
    index_template_id = state["index_template_id"]

    session = requests.Session()
    session.headers["Accept"] = "application/json"
    token = get_token(session)
    session.headers["Authorization"] = f"Token {token}"

    print(f"Uploading {len(DOCS)} synthetic documents (doc_type_id={doc_type_id})")
    uploaded_ids = []
    for d in DOCS:
        did = upload(session, d, doc_type_id, metadata_type_ids)
        if did:
            uploaded_ids.append(did)

    # Persist uploaded ids for the verifier to find.
    state["uploaded_document_ids"] = uploaded_ids
    STATE_FILE.write_text(json.dumps(state, indent=2))

    # Trigger rebuild AFTER all docs have metadata attached. Doing this here
    # (rather than in setup_matter_tree.py) avoids the "None" orphan node
    # produced when indexing fires on doc-create before metadata-set.
    print()
    print(f"Triggering index rebuild on template {index_template_id} "
          f"(against complete metadata state)")
    r = _request(session, "POST", f"{API}/index_templates/{index_template_id}/rebuild/")
    if r.status_code not in (200, 202, 204):
        print(f"  [warn] rebuild: {r.status_code} {r.text[:200]}")
    else:
        print("  rebuild queued")

    print()
    print(f"Uploaded {len(uploaded_ids)}/{len(DOCS)} documents.")
    print("Wait ~30s for the celery indexing worker to run, then:")
    print("  python verify_tree.py")
    return 0 if len(uploaded_ids) == len(DOCS) else 1


if __name__ == "__main__":
    sys.exit(main())
