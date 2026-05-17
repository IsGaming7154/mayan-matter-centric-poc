"""Template tag exposing the live matter-tree IndexInstance to dashboard
templates. Reads from the `IndexInstanceNode` MPTT table directly — no
HTTP round-trip from inside Django, no cache layer.

Usage in a template:
    {% load matter_tree %}
    {% get_matter_tree as tree %}
    {% for client in tree %}
      ... {{ client.label }} ...
      {% for matter in client.children %}
        ... {{ matter.label }} ...
      {% endfor %}
    {% endfor %}

Each yielded node is a small dict (not the model instance) so the template
stays decoupled from Mayan's ORM internals.
"""

from __future__ import annotations

import logging

from django import template
from django.db import DatabaseError

logger = logging.getLogger(__name__)
register = template.Library()

# The matter tree was created on Day 1 with this exact label.
MATTER_INDEX_LABEL = 'Matters'


def _node_to_dict(node, document_count: int) -> dict:
    """Shape a tree-node into a small dict for the template."""
    return {
        'id': node.id,
        'label': node.value or '(root)',
        'document_count': document_count,
        'children': [],
    }


@register.simple_tag
def get_matter_tree(index_label: str = MATTER_INDEX_LABEL):
    """Return the matter tree as a nested list of dicts, or [] on any failure.

    Returns the top-level (client) nodes, each with `children` populated to
    one level of nesting. The dashboard only needs 2 visible levels for the
    overlay — deeper drill-down is left for a future iteration.
    """
    try:
        # Imports happen inside the tag so a misconfigured Mayan install
        # (template tag loaded before models migrated) doesn't break the
        # whole dashboard.
        from mayan.apps.document_indexing.models.index_instance_models import (
            IndexInstance, IndexInstanceNode,
        )

        try:
            instance = IndexInstance.objects.get(label=index_label)
        except IndexInstance.DoesNotExist:
            logger.info(
                'matter_ui_overlay: no IndexInstance with label=%r yet — '
                'the dashboard overlay will show empty until the index is '
                'created and a document is uploaded.', index_label
            )
            return []

        # The synthetic root node is the parent of all client-level nodes.
        # Look it up via the template root pointer on the instance.
        root_template_node = instance.index_template_root_node
        try:
            root_instance_node = IndexInstanceNode.objects.get(
                index_template_node=root_template_node, parent__isnull=True
            )
        except IndexInstanceNode.DoesNotExist:
            return []

        def _doc_count_descendants(node) -> int:
            """Sum docs across this node and all descendants.

            Documents only attach at LEAF nodes in Mayan's index tree
            (the doc_type level: correspondence, pleadings). Showing the
            literal `node.documents.count()` on client/matter nodes
            always reads 0, which is technically true but misleading —
            for a navigation surface the meaningful number is total
            docs in subtree.
            """
            total = node.documents.count()
            for descendant in node.get_descendants():
                total += descendant.documents.count()
            return total

        result = []
        # Client level — direct children of root.
        for client_node in root_instance_node.get_children():
            client_dict = _node_to_dict(
                client_node, document_count=_doc_count_descendants(client_node)
            )
            # Matter level — children of client.
            for matter_node in client_node.get_children():
                client_dict['children'].append(
                    _node_to_dict(
                        matter_node,
                        document_count=_doc_count_descendants(matter_node)
                    )
                )
            result.append(client_dict)
        return result
    except DatabaseError as exc:
        logger.warning('matter_ui_overlay: DB error reading tree: %s', exc)
        return []
    except Exception as exc:
        # Defensive: dashboard rendering must not crash because of overlay.
        logger.exception('matter_ui_overlay: unexpected error: %s', exc)
        return []
