"""relay.config.tag_mapping — Template resolution for catalog metadata values.

Catalog ``metadata`` entries can be literal strings or ``${tag:NAME}``
templates.  This module provides two pure functions that resolve those templates
against the resource tags that ``AlarmTagResolver`` stamps onto an incident at
parse time.

Design decisions
----------------
* **Never raise.** Both functions are called on the alarm-handling hot path and
  must be 100 % safe against bad inputs.  Missing tags, ``None`` arguments, and
  malformed templates all result in graceful degradation (skip the key, return
  ``{}``), never exceptions.
* **No AWS.** This module is cloud-agnostic and fully unit-testable with no
  credentials or mocks.
* **Literal fast path.** A value with no ``${tag:`` marker is returned unchanged
  without a regex scan, keeping the common case (literal metadata) cheap.

Typical usage (called by the Node alarm handler after parse + classify)::

    from relay.config.tag_mapping import resolve_deployment_metadata

    tag_map   = config.hierarchy.deployment_defaults.tag_map  # org-wide
    node_meta = org_tree.get(incident.deployment_id).metadata  # per-deployment
    resolved  = resolve_deployment_metadata(node_meta, tag_map, incident.tags)
    if resolved:
        incident.deployment_metadata = resolved
"""

from __future__ import annotations

import re

# Matches every ``${tag:NAME}`` placeholder in a metadata value string.
# The capture group is the tag name (everything between ``tag:`` and ``}``).
_TAG_REF = re.compile(r"\$\{tag:([^}]+)\}")


def resolve_template(value: object, tags: dict[str, str] | None) -> object:
    """Resolve a single metadata value against resource tags.

    A string with no ``${tag:NAME}`` marker is a literal and is returned
    unchanged (fast path).  Otherwise every ``${tag:NAME}`` placeholder is
    substituted with ``tags[NAME]``.  If **any** referenced tag is absent the
    entire value is unresolvable and ``None`` is returned — a half-substituted
    string never leaks into resolved metadata.

    Non-string values (ints, bools, dicts, …) are passed through unchanged
    because they cannot contain tag references.

    Args:
        value: The raw metadata value from the catalog (any type).
        tags:  Resource tags dict (may be ``None``; treated as ``{}``).

    Returns:
        The resolved string, the original non-string value, or ``None`` when
        a referenced tag is absent.
    """
    if not isinstance(value, str):
        return value
    # Fast path: no placeholder marker present — return as-is.
    if "${tag:" not in value:
        return value

    _tags: dict[str, str] = tags or {}
    missing: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in _tags:
            missing.append(name)
            return ""  # placeholder; will be discarded below
        return _tags[name]

    result = _TAG_REF.sub(_sub, value)
    if missing:
        return None
    return result


def resolve_deployment_metadata(
    node_metadata: dict[str, object] | None,
    tag_map: dict[str, str] | None,
    tags: dict[str, str] | None,
) -> dict[str, object]:
    """Build a deployment's resolved metadata dict.

    Resolution order — **later wins**:

    1. **Global tag_map** (``metadata_key`` → ``TAG_NAME``): for each mapping
       whose tag is present in *tags*, set ``out[key] = tags[TAG_NAME]``.
       Mappings whose tag is absent are silently skipped.

    2. **Node-level metadata overlay**: each key/value from *node_metadata* is
       processed via :func:`resolve_template`:

       * A string that resolves (all its tag references are present) replaces
         any tag_map-provided value for that key.
       * A string that does **not** resolve (a tag is missing) is **skipped** —
         the tag_map-provided value (if any) survives.
       * A non-string value is set as-is (e.g. a boolean ``enabled`` flag).

    All three arguments default-safe: ``None`` is treated as ``{}``.  The
    function never raises.

    Args:
        node_metadata: Per-deployment ``metadata`` dict from the catalog OrgNode.
        tag_map:       Org-wide ``deployment_defaults.tag_map`` from hierarchy.yaml.
        tags:          Incident resource tags (populated by ``AlarmTagResolver``).

    Returns:
        A new dict containing all successfully resolved metadata keys.  Empty
        dict when nothing resolves.
    """
    _node_meta: dict[str, object] = node_metadata or {}
    _tag_map: dict[str, str] = tag_map or {}
    _tags: dict[str, str] = tags or {}

    out: dict[str, object] = {}

    # Layer 1 — global tag_map: key → value sourced from a resource tag.
    for meta_key, tag_name in _tag_map.items():
        if tag_name in _tags:
            out[meta_key] = _tags[tag_name]

    # Layer 2 — per-deployment metadata overlay (later wins).
    for key, raw_value in _node_meta.items():
        resolved = resolve_template(raw_value, _tags)
        if resolved is None:
            # Template string with a missing tag — keep any tag_map value.
            continue
        out[key] = resolved

    return out
