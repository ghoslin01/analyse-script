"""Conservative evidence for high-volume rules not yet in the trace engine.

This module deliberately describes declared evidence only.  It does not turn
these rules into executable semantics or infer datastore writes from names.
"""
from __future__ import annotations

from typing import Any

from .templates import field_references


_SUPPORTED = {"AddToListRule", "CompareMultiListValuesRule", "SetQuestionStatus"}
_DATA_ITEM = "data item"


def _class_name(full_class: str) -> str:
    return full_class.rsplit(".", 1)[-1] if isinstance(full_class, str) else ""


def _template_refs(value: Any) -> list[str]:
    """Return only $$ template dependencies, preserving first-seen order."""
    if not isinstance(value, str):
        return []
    return field_references(value)


def _append_unique(items: list[str], values: list[str]) -> None:
    for value in values:
        if value and value not in items:
            items.append(value)


def _add_to_list(raw: dict[str, Any]) -> dict[str, Any]:
    reads: list[str] = []
    writes: list[str] = []
    list_type = raw.get('ListType', '').strip().upper()
    list_attribute = {'STATIC': 'StaticListToAddTo', 'DYNAMIC': 'DynamicListToAddTo'}.get(list_type)
    evidence: dict[str, Any] = {
        "rule_class": "AddToListRule",
        "list_target": {
            "type": list_type.lower() if list_attribute else 'unknown',
            "name": raw.get(list_attribute) if list_attribute else None,
        },
        "list_target_is_datastore_write": False,
    }
    # PropertyName-like attributes are intentionally ignored.  The mode on
    # each side decides whether its declared source is a datastore item.
    for side, type_key, property_key, value_key in (
        ("key", "KeyType", "KeyPropertyName", "KeyValue"),
        ("value", "ValueType", "ValuePropertyName", "Value"),
        ("group", "GroupValueType", "GroupValuePropertyName", "GroupValue"),
    ):
        mode = str(raw.get(type_key, "")).strip().casefold()
        if mode == _DATA_ITEM:
            value = raw.get(property_key)
            if isinstance(value, str) and value.strip():
                _append_unique(reads, [value.strip()])
                evidence[f"{side}_source"] = {"mode": "Data Item", "path": value.strip()}
        elif mode == "value":
            refs = _template_refs(raw.get(value_key))
            _append_unique(reads, refs)
            evidence[f"{side}_source"] = {
                "mode": "Value",
                "template": raw.get(value_key, ""),
                "template_dependencies": refs,
            }
    for key in ("ErrorCodeDataItem", "ErrorMsgDataItem"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            _append_unique(writes, [value.strip()])
    if writes:
        evidence["error_outputs"] = writes.copy()
    return {"read_paths": reads, "write_paths": writes, "evidence": evidence}


def _compare(raw: dict[str, Any]) -> dict[str, Any]:
    reads: list[str] = []
    current = raw.get("CurrentListValue")
    if isinstance(current, str) and current.strip():
        reads.append(current.strip())
    new_values = raw.get("NewListValues")
    return {
        "read_paths": reads,
        "write_paths": [],
        "evidence": {
            "rule_class": "CompareMultiListValuesRule",
            "current_list_value": current,
            "new_list_values": new_values,
            "new_list_values_role": "ambiguous",
            "new_list_values_write_proven": False,
            "compare_values": raw.get("CompareValues"),
            "value_separator": raw.get("ValueSeparator"),
            "process_empty": raw.get("ProcessEmpty"),
        },
    }


def _question_status(raw: dict[str, Any]) -> dict[str, Any]:
    question = raw.get("Question")
    targets = [item.strip() for item in question.split(",") if item.strip()] if isinstance(question, str) else []
    flags = {
        key: raw[key] for key in (
            "ReadOnly", "Mandatory", "ChangeMandatoryStatus", "ChangeReadOnlyStatus",
            "UndoRequired", "IgnoreQuestions", "IgnoreMandatoryQuestions", "RuleType", "RuleDisabled",
        ) if key in raw
    }
    return {
        "read_paths": [],
        "write_paths": [],
        "evidence": {
            "rule_class": "SetQuestionStatus",
            "ui_targets": targets,
            "question_raw": question,
            "status_flags": flags,
            "read_only_effect_proven": False,
            "mandatory_effect_proven": False,
        },
    }


def partial_rule_evidence(full_class: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    """Return bounded evidence for one supported rule, or ``None``.

    ``read_paths`` and ``write_paths`` contain datastore paths only.  UI
    targets and named runtime lists remain evidence, not datastore edges.
    """
    if not isinstance(raw, dict):
        return None
    name = _class_name(full_class)
    if name not in _SUPPORTED:
        return None
    if name == "AddToListRule":
        return _add_to_list(raw)
    if name == "CompareMultiListValuesRule":
        return _compare(raw)
    return _question_status(raw)
