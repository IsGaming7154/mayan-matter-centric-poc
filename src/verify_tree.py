"""Fetch the matter index instance and print the tree. Pass/fail gate for Day 1.

Pass criterion: the live IndexInstance for "Matters" contains the expected
client nodes (ACME-CORP, WAYNE-ENT), the right matter children below them,
and the correct doc_type leaves with the right uploaded documents attached.
Anything else is a fail and the Phase 2 decision flips.

API surface (verified against
/.sources/mayan-edms/mayan/apps/document_indexing/urls.py lines 160-200):
  GET /api/v4/index_instances/                         list instances
  GET /api/v4/index_instances/{id}/                    detail
  GET /api/v4/index_instances/{id}/nodes/              top-level (paginated)
  GET {node.children_url}                              direct children of node

Note (Day 1 finding): nodes carry `parent_id` (scalar int), not `parent`,
and `/nodes/` returns one tree level — full traversal requires following
each node's `children_url`. The earlier version of this script keyed on
`parent` and never saw the populated tree.

Three exit states:
  PASS    (0): tree shape matches expected
  FAIL    (2): tree populated but doesn't match expected layout
  TIMEOUT (3): tree never populated past root after 120s — debugging
              state, not a platform flip
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

EXPECTED_TREE = {
    "ACME-CORP": {"2025-001": {"correspondence": 1, "pleadings": 1}},
    "WAYNE-ENT": {"2025-007": {"correspondence": 1}},
}


REQUEST_DELAY = 1.0
MAX_429_RETRIES = 5


def _request(session, method, url, **kwargs):
    for attempt in range(MAX_429_RETRIES):
        r = session.request(method, url, timeout=30, **kwargs)
        if r.status_code != 429:
            time.sleep(REQUEST_DELAY)
            return r
        retry_after = r.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else (2 ** attempt)
        time.sleep(wait)
    return r


def get_token(session):
    r = _request(session, "POST", f"{API}/auth/token/obtain/",
                 data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    r.raise_for_status()
    return r.json()["token"]


def _paginate(session, url):
    """Page through a list endpoint, return all results."""
    out = []
    while url:
        r = _request(session, "GET", url)
        r.raise_for_status()
        page = r.json()
        out.extend(page.get("results", []))
        url = page.get("next")
    return out


def fetch_tree(session, instance_id):
    """Walk the instance tree via children_url. Return flat list of nodes.

    Strategy: start from /index_instances/{id}/nodes/ (which returns
    top-level nodes — children of the synthetic root), then for each
    node follow its `children_url` recursively. Each node carries its
    own `parent_id` so the caller can reconstruct the tree.
    """
    seen_ids = set()
    nodes = []
    queue = list(_paginate(session, f"{API}/index_instances/{instance_id}/nodes/"))
    while queue:
        node = queue.pop(0)
        nid = node.get("id")
        if nid in seen_ids:
            continue
        seen_ids.add(nid)
        nodes.append(node)
        child_url = node.get("children_url")
        if child_url:
            queue.extend(_paginate(session, child_url))
    return nodes


def render_tree(nodes, indent=0, parent_id=None):
    """Print a hierarchical view of the live index instance."""
    children = [n for n in nodes if n.get("parent_id") == parent_id]
    for ch in sorted(children, key=lambda n: n.get("value", "") or ""):
        label = ch.get("value") or "(root)"
        doc_count = ch.get("documents_count")
        if doc_count is None:
            doc_count = "?"
        print(f"{'  ' * indent}- {label}  [docs={doc_count}]")
        render_tree(nodes, indent + 1, parent_id=ch["id"])


def check_tree(nodes, expected):
    """Return (ok, problems) by comparing observed to expected.

    The observed top-level is the set of nodes whose parent_id points
    at a node not in `nodes` (i.e. the synthetic root, which /nodes/
    doesn't return). Any orphan "None" value nodes (from pre-metadata
    indexing races) are reported as problems too — the rebuild
    deferral in upload_test_docs.py should eliminate them.
    """
    problems = []
    by_id = {n["id"]: n for n in nodes}

    def children_of(parent_id):
        return {
            (n.get("value") or ""): n
            for n in nodes
            if n.get("parent_id") == parent_id
        }

    top_level_parent_ids = {
        n.get("parent_id") for n in nodes
        if n.get("parent_id") not in by_id
    }
    if len(top_level_parent_ids) != 1:
        problems.append(
            f"expected exactly 1 synthetic root, observed parent_ids "
            f"pointing outside the fetched set: {sorted(top_level_parent_ids)}"
        )
        return False, problems
    root_id = top_level_parent_ids.pop()

    client_layer = children_of(root_id)

    # Flag any orphan "None" or "" value nodes at the top level.
    for label in list(client_layer):
        if label in ("None", ""):
            problems.append(
                f"orphan top-level node with value={label!r} "
                f"(indicates a pre-metadata indexing race — should be gone "
                f"after deferred rebuild)"
            )

    for client, matters in expected.items():
        if client not in client_layer:
            problems.append(f"missing client node: {client!r}")
            continue
        matter_layer = children_of(client_layer[client]["id"])
        for matter, doc_types in matters.items():
            if matter not in matter_layer:
                problems.append(f"missing matter node under {client}: {matter!r}")
                continue
            doctype_layer = children_of(matter_layer[matter]["id"])
            for dt, expected_count in doc_types.items():
                if dt not in doctype_layer:
                    problems.append(
                        f"missing doc_type leaf under {client}/{matter}: {dt!r}"
                    )
                    continue
                node = doctype_layer[dt]
                observed = node.get("documents_count")
                if observed is not None and observed != expected_count:
                    problems.append(
                        f"{client}/{matter}/{dt} expected {expected_count} doc(s), "
                        f"observed {observed}"
                    )
    return (len(problems) == 0), problems


def main() -> int:
    if not STATE_FILE.exists():
        print("[error] state.json not found — run setup_matter_tree.py first")
        return 1
    state = json.loads(STATE_FILE.read_text())
    template_id = state["index_template_id"]

    session = requests.Session()
    session.headers["Accept"] = "application/json"
    token = get_token(session)
    session.headers["Authorization"] = f"Token {token}"

    r = _request(session, "GET", f"{API}/index_instances/")
    r.raise_for_status()
    instances = r.json().get("results", [])
    matching = [
        i for i in instances
        if i.get("index_template") == template_id
        or i.get("id") == template_id
        or (isinstance(i.get("index_template"), dict) and i["index_template"].get("id") == template_id)
    ]
    if not matching:
        print(f"[error] no IndexInstance found for template id {template_id}")
        print(f"  instances list: {json.dumps(instances, indent=2)[:600]}")
        return 1
    instance_id = matching[0]["id"]
    print(f"[1/3] Found index instance id={instance_id} for template {template_id}")

    POLL_BUDGET_SECONDS = 120
    deadline = time.time() + POLL_BUDGET_SECONDS
    timed_out = False
    nodes = []
    while True:
        nodes = fetch_tree(session, instance_id)
        if nodes:
            break
        if time.time() > deadline:
            timed_out = True
            break
        print("  ... waiting for rebuild")
        time.sleep(3)
    print(f"[2/3] Tree has {len(nodes)} total node(s) (excluding synthetic root)")
    print()
    # Render starting from each top-level node (parent_id outside the set).
    by_id = {n["id"]: n for n in nodes}
    top_parent_ids = {
        n.get("parent_id") for n in nodes if n.get("parent_id") not in by_id
    }
    for tpid in top_parent_ids:
        render_tree(nodes, indent=0, parent_id=tpid)
    print()

    if timed_out:
        print("==== TIMEOUT ====")
        print(f"Tree never populated after {POLL_BUDGET_SECONDS}s of polling.")
        print()
        print("This is NOT a platform flip. The rebuild task may still be queued, the")
        print("celery worker may be stuck, or the document upload may not have triggered")
        print("the index event. Per CLAUDE.md: STOP and report.")
        print()
        print("Suggested next checks (run by hand, do not flip platforms):")
        print("  docker compose logs --tail=100 app | grep -i 'celery\\|index\\|rebuild'")
        return 3

    print("[3/3] Comparing observed tree to expected layout")
    ok, problems = check_tree(nodes, EXPECTED_TREE)
    if ok:
        print()
        print("==== PASS ====")
        print("IndexTemplate.expression auto-populated the matter tree correctly.")
        print("The Phase 2 recommendation is validated. Day 1 gate cleared.")
        return 0
    else:
        print()
        print("==== FAIL ====")
        for p in problems:
            print(f"  - {p}")
        print()
        print("Phase 2 pass/fail gate: the IndexTemplate primitive did not behave as documented.")
        print("Per CLAUDE.md: STOP and message the operator. Do not debug around this.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
