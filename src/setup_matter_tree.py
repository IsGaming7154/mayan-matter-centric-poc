"""Day 1 setup: configure Mayan to test IndexTemplate.expression.

Talks to Mayan's REST API (v4) and configures:
  - 3 MetadataType records: client_id, matter_id, doc_type
  - 1 DocumentType: "Legal Matter Document" with those 3 metadata types attached as required
  - 1 IndexTemplate: "Matters" with 3 nested IndexTemplateNodes whose `expression`
    fields render client_id / matter_id / doc_type via Django templating
  - Associates the DocumentType with the IndexTemplate
  - Triggers an index rebuild

API surface used (paths verified against
/.sources/mayan-edms/mayan/apps/{rest_api,metadata,documents,document_indexing}/urls.py):
  POST /api/v4/auth/token/obtain/                                       authenticate
  POST /api/v4/metadata_types/                                          create metadata type
  POST /api/v4/document_types/                                          create doc type
  POST /api/v4/document_types/{id}/metadata_types/                      attach metadata
  POST /api/v4/index_templates/                                         create index template
  POST /api/v4/index_templates/{id}/document_types/add/                 attach doc type
  POST /api/v4/index_templates/{id}/nodes/                              create node (via web routes)
  POST /api/v4/index_templates/{id}/rebuild/                            trigger rebuild

This script is idempotent on re-run: each create call swallows 4xx-duplicate errors
and looks up the existing record by name. State for downstream scripts is written
to ./state.json.
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
STATE_FILE = Path(__file__).parent / "state.json"

# The matter-centric primitive under test. Each node's `expression` field is
# rendered as a Django template against the document context. The available
# context variable for metadata is `document.metadata_value_of.<name>`.
INDEX_TEMPLATE_LABEL = "Matters"
INDEX_TEMPLATE_SLUG = "matters"
INDEX_NODES = [
    "{{ document.metadata_value_of.client_id }}",
    "{{ document.metadata_value_of.matter_id }}",
    "{{ document.metadata_value_of.doc_type }}",
]

METADATA_TYPES = [
    {"name": "client_id", "label": "Client ID"},
    {"name": "matter_id", "label": "Matter ID"},
    {"name": "doc_type", "label": "Document type"},
]

DOCUMENT_TYPE_LABEL = "Legal Matter Document"


REQUEST_DELAY = 1.2  # seconds between calls — Mayan's DRF throttle is ~1 req/s burst
MAX_429_RETRIES = 5


def _request(session, method, url, **kwargs):
    """HTTP helper that pauses on every call and retries 429 with backoff."""
    for attempt in range(MAX_429_RETRIES):
        r = session.request(method, url, timeout=30, **kwargs)
        if r.status_code != 429:
            time.sleep(REQUEST_DELAY)
            return r
        # Honor Retry-After if present, else exponential.
        retry_after = r.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else (2 ** attempt)
        print(f"  [throttled] sleeping {wait:.1f}s (attempt {attempt + 1}/{MAX_429_RETRIES})")
        time.sleep(wait)
    return r  # last response, still 429


def get_token(session: requests.Session) -> str:
    r = _request(
        session, "POST", f"{API}/auth/token/obtain/",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    r.raise_for_status()
    return r.json()["token"]


def find_by(session: requests.Session, path: str, key: str, value):
    """Page through a list endpoint, return first item whose key == value, else None.

    Supports nested keys: if the item's value is a dict and `value` is also a dict,
    compares only the keys present in the `value` dict.
    """
    url = f"{API}{path}"
    while url:
        r = _request(session, "GET", url)
        r.raise_for_status()
        data = r.json()
        for item in data.get("results", []):
            cell = item.get(key)
            if isinstance(value, dict) and isinstance(cell, dict):
                if all(cell.get(k) == v for k, v in value.items()):
                    return item
            elif cell == value:
                return item
        url = data.get("next")
    return None


def create_or_find(session, list_path, payload, match_key):
    existing = find_by(session, list_path, match_key, payload[match_key])
    if existing:
        print(f"  [exists] {list_path}: {match_key}={payload[match_key]} id={existing['id']}")
        return existing
    r = _request(session, "POST", f"{API}{list_path}", json=payload)
    if not r.ok:
        print(f"  [error] POST {list_path} {payload}: {r.status_code} {r.text}")
        r.raise_for_status()
    item = r.json()
    print(f"  [created] {list_path}: {match_key}={payload[match_key]} id={item['id']}")
    return item


def main() -> int:
    session = requests.Session()
    session.headers["Accept"] = "application/json"
    token = get_token(session)
    session.headers["Authorization"] = f"Token {token}"
    print(f"[1/6] Authenticated as {ADMIN_USERNAME}")

    print("[2/6] Creating metadata types")
    metadata_ids = {}
    for mt in METADATA_TYPES:
        item = create_or_find(session, "/metadata_types/", mt, "name")
        metadata_ids[mt["name"]] = item["id"]

    print("[3/6] Creating document type")
    doc_type = create_or_find(
        session, "/document_types/", {"label": DOCUMENT_TYPE_LABEL}, "label"
    )
    doc_type_id = doc_type["id"]

    print("[4/6] Attaching metadata types to document type (required=True)")
    for name, mid in metadata_ids.items():
        path = f"/document_types/{doc_type_id}/metadata_types/"
        existing = find_by(session, path, "metadata_type", {"id": mid})
        if existing:
            print(f"  [exists] {name} attached")
            continue
        r = _request(session, "POST", f"{API}{path}",
                     json={"metadata_type_id": mid, "required": True})
        if not r.ok:
            print(f"  [error] attach {name}: {r.status_code} {r.text}")
            r.raise_for_status()
        print(f"  [created] {name} attached to {DOCUMENT_TYPE_LABEL}")

    print("[5/6] Creating index template + nodes")
    index = create_or_find(
        session,
        "/index_templates/",
        {"label": INDEX_TEMPLATE_LABEL, "slug": INDEX_TEMPLATE_SLUG, "enabled": True},
        "label",
    )
    index_id = index["id"]

    # Get the auto-created root node, then add 3 children chained off it.
    # Mayan exposes the root node id as `index_template_root_node_id` on the
    # index template detail (a scalar, not a nested object).
    r = _request(session, "GET", f"{API}/index_templates/{index_id}/")
    r.raise_for_status()
    index_detail = r.json()
    root_node_id = index_detail.get("index_template_root_node_id")
    if not root_node_id:
        print(f"  [error] no root node id on index detail. keys: {list(index_detail.keys())}")
        print(f"  detail: {json.dumps(index_detail, indent=2)[:600]}")
        return 1
    print(f"  root_node_id={root_node_id}")

    # Chain three nodes. Each is a child of the previous; the deepest sets
    # link_documents=True so documents land at that leaf.
    #
    # Probe the endpoint shape with the FIRST node before iterating. If the
    # serializer rejects the payload, stop and print the response body so we
    # can adjust the shape once instead of generating three identical 400s.
    parent_id = root_node_id
    node_ids = []
    node_path = f"{API}/index_templates/{index_id}/nodes/"

    def post_node(depth, expression, parent, is_leaf):
        payload = {
            "parent": parent,
            "expression": expression,
            "enabled": True,
            "link_documents": is_leaf,
        }
        r = _request(session, "POST", node_path, json=payload)
        return r, payload

    # --- Probe: depth 1 only ---
    probe_depth = 1
    probe_expression = INDEX_NODES[0]
    probe_is_leaf = (len(INDEX_NODES) == 1)
    r, sent = post_node(probe_depth, probe_expression, parent_id, probe_is_leaf)
    if not r.ok:
        print()
        print("  [PROBE FAILED] node endpoint rejected the request shape.")
        print(f"  URL:       POST {node_path}")
        print(f"  Status:    {r.status_code}")
        print(f"  Sent:      {json.dumps(sent)}")
        print(f"  Response:  {r.text}")
        print()
        print("  Stopping before the other two nodes — adjust the payload shape once,")
        print("  not three times. Re-run setup after fixing.")
        return 1
    probe_node = r.json()
    node_ids.append(probe_node["id"])
    parent_id = probe_node["id"]
    print(f"  [created] depth 1 expression={probe_expression!r} id={probe_node['id']} leaf={probe_is_leaf}")

    # --- Remaining nodes only run after the probe passes ---
    for depth, expression in enumerate(INDEX_NODES[1:], start=2):
        is_leaf = depth == len(INDEX_NODES)
        r, sent = post_node(depth, expression, parent_id, is_leaf)
        if not r.ok:
            print(f"  [error] depth {depth} (probe passed, but this one failed):")
            print(f"  Status:   {r.status_code}")
            print(f"  Sent:     {json.dumps(sent)}")
            print(f"  Response: {r.text}")
            return 1
        node = r.json()
        node_ids.append(node["id"])
        print(f"  [created] depth {depth} expression={expression!r} id={node['id']} leaf={is_leaf}")
        parent_id = node["id"]

    # Attach the document type to the index template.
    r = _request(session, "POST",
                 f"{API}/index_templates/{index_id}/document_types/add/",
                 json={"document_type": doc_type_id})
    if r.status_code not in (200, 201, 204):
        print(f"  [warn] attach doc type: {r.status_code} {r.text[:200]}")
    else:
        print(f"  [ok] document type {doc_type_id} attached to index template {index_id}")

    # NOTE: rebuild is deliberately NOT triggered here. It's deferred to
    # the end of upload_test_docs.py so the rebuild runs against COMPLETE
    # metadata state. Day 1 observation: indexing fires on doc-create
    # before metadata-set (separate API calls), which leaves orphan "None"
    # value nodes in the tree from unresolved Django template expressions.
    # Deferring rebuild past the last metadata write produces a clean tree.
    print("[6/6] Skipping rebuild (deferred to upload_test_docs.py)")

    state = {
        "base_url": BASE_URL,
        "document_type_id": doc_type_id,
        "document_type_label": DOCUMENT_TYPE_LABEL,
        "metadata_type_ids": metadata_ids,
        "index_template_id": index_id,
        "index_node_ids": node_ids,
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print(f"\nState written to {STATE_FILE}")
    print("Next: python upload_test_docs.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
