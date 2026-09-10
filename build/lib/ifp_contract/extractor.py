"""Extract only Data Integrator OData contracts and their direct callers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .store import ContractStore
from .stream import (
    TagSpan,
    iter_occurrence_offsets,
    iter_start_tags,
    read_selected_attributes,
    tag_span_containing,
)


DIRECT_ATTRIBUTE_NAMES = frozenset(
    name.casefold()
    for name in (
        "eid",
        "EID",
        "Name",
        "RuleClassName",
        "ClassType",
        "SelectComponent",
        "ComponentList",
        "Component",
        "ComponentName",
        "ComponentPath",
        "CallComponent",
        "TargetComponent",
        "Source",
        "IRISSource",
        "IRISAction",
        "Action",
        "HttpMethod",
        "URL",
        "Uri",
        "URI",
        "Endpoint",
        "Path",
        "ResourcePath",
        "OperationPath",
        "RelativePath",
        "IRISPath",
        "Filter",
        "IRISFilter",
        "Query",
        "TargetDataGroup",
        "ResultsDataGroup",
        "ResultDataGroup",
        "QueryInputDataGroup",
        "RequestDataGroup",
        "InputDataGroup",
        "OutputDataGroup",
        "Input",
        "Output",
        "InputDataItem",
        "AdditionalHTTPDataGroups",
        "ErrorCodeDataItem",
        "ErrorMsgDataItem",
        "DataSourceName",
        "MappingSetName",
    )
)
MAPPING_SUFFIXES = (
    "_ClassType",
    "_SolutionDataItemMapping",
    "_PropertyKey",
    "_In",
    "_Out",
    "_PubIn",
    "_PubOut",
    "_readMapping",
    "_writeMapping",
    "_Mapping",
)
REFERENCE_ATTRIBUTE_NAMES = frozenset(
    name.casefold()
    for name in (
        "SelectComponent",
        "ComponentList",
        "Component",
        "ComponentName",
        "ComponentPath",
        "CallComponent",
        "TargetComponent",
        "Source",
    )
)


def is_contract_attribute(name: str) -> bool:
    lowered = name.casefold()
    return lowered in DIRECT_ATTRIBUTE_NAMES or any(
        lowered.endswith(suffix.casefold()) for suffix in MAPPING_SUFFIXES
    )


def _value(attributes: dict[str, str], *names: str) -> str | None:
    by_lower = {key.casefold(): value for key, value in attributes.items()}
    for name in names:
        value = by_lower.get(name.casefold())
        if value not in (None, ""):
            return value
    return None


def _is_rule(span: TagSpan) -> bool:
    return span.name.rsplit(":", 1)[-1].casefold() == "rule"


def _is_odata_operation(attributes: dict[str, str]) -> bool:
    rule_class = _value(attributes, "RuleClassName", "ClassType") or ""
    source = _value(attributes, "IRISSource", "Source", "DataSourceName") or ""
    combined = f"{rule_class} {source}".casefold()
    return any(
        marker in combined
        for marker in ("invokeirisrule", "swaggerintegrationrule", "odata", "openapi")
    )


def _matching_selector(attributes: dict[str, str], reference: str) -> str | None:
    reference_folded = reference.casefold()
    for name, value in attributes.items():
        if name.casefold() in REFERENCE_ATTRIBUTE_NAMES and reference_folded in value.casefold():
            return value
    return None


def _is_component_call(attributes: dict[str, str]) -> bool:
    rule_class = _value(attributes, "RuleClassName", "ClassType") or ""
    return "component" in rule_class.casefold() or "call" in rule_class.casefold()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "y", "on"}


def mappings_from_attributes(attributes: dict[str, str]) -> list[dict[str, object]]:
    """Turn flattened ``*_ComponentMapping`` attributes into compact rows."""

    groups: dict[str, dict[str, str]] = defaultdict(dict)
    suffix_by_lower = sorted(
        ((suffix.casefold(), suffix[1:]) for suffix in MAPPING_SUFFIXES),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for name, value in attributes.items():
        lowered = name.casefold()
        for suffix, key in suffix_by_lower:
            if lowered.endswith(suffix):
                prefix = name[: len(name) - len(suffix)]
                if prefix:
                    groups[prefix][key] = value
                break

    result: list[dict[str, object]] = []
    for prefix in sorted(groups):
        group = groups[prefix]
        solution = group.get("SolutionDataItemMapping")
        property_key = group.get("PropertyKey")
        if not solution and not property_key:
            continue
        incoming = _truthy(group.get("In")) or _truthy(group.get("PubIn"))
        outgoing = _truthy(group.get("Out")) or _truthy(group.get("PubOut"))
        direction = "INOUT" if incoming and outgoing else "INPUT" if incoming else "OUTPUT" if outgoing else "UNKNOWN"
        result.append(
            {
                "mapping_prefix": prefix,
                "solution_data_item": solution,
                "property_key": property_key,
                "direction": direction,
                "class_type": group.get("ClassType"),
                "attributes": group,
            }
        )
    return result


def iter_odata_operations(path: str | Path) -> Iterator[dict[str, object]]:
    """Yield integration/OData Rule contracts from the designated DI IFP only."""

    file_path = Path(path)
    with file_path.open("rb") as attributes_handle:
        for span in iter_start_tags(file_path):
            if not _is_rule(span):
                continue
            attributes = read_selected_attributes(
                file_path, span.start, span.end, is_contract_attribute, handle=attributes_handle
            )
            if not _is_odata_operation(attributes):
                continue
            yield {
                "integrator_file": str(file_path),
                "tag_offset": span.start,
                "rule_eid": _value(attributes, "eid"),
                "rule_name": _value(attributes, "Name"),
                "rule_class": _value(attributes, "RuleClassName", "ClassType") or "",
                "source_name": _value(attributes, "IRISSource", "Source", "DataSourceName"),
                "action": _value(attributes, "IRISAction", "Action", "HttpMethod"),
                "api_path": _value(
                    attributes,
                    "URL",
                    "Uri",
                    "URI",
                    "Endpoint",
                    "Path",
                    "ResourcePath",
                    "OperationPath",
                    "RelativePath",
                    "IRISPath",
                ),
                "filter_expr": _value(attributes, "Filter", "IRISFilter", "Query"),
                "request_group": _value(
                    attributes, "QueryInputDataGroup", "RequestDataGroup", "InputDataGroup", "Input"
                ),
                "target_group": _value(attributes, "TargetDataGroup"),
                "results_group": _value(attributes, "ResultsDataGroup", "ResultDataGroup"),
                "output_group": _value(attributes, "OutputDataGroup", "Output"),
                "attributes": attributes,
            }


def iter_caller_references(
    path: str | Path, reference: str
) -> Iterator[tuple[dict[str, object], list[dict[str, object]]]]:
    """Yield direct component-call Rules that name the Data Integrator."""

    file_path = Path(path)
    needle = reference.encode("utf-8")
    seen_tag_offsets: set[int] = set()
    with file_path.open("rb") as attributes_handle:
        for occurrence in iter_occurrence_offsets(file_path, needle):
            span = tag_span_containing(file_path, occurrence)
            if span is None or span.start in seen_tag_offsets or not _is_rule(span):
                continue
            seen_tag_offsets.add(span.start)
            attributes = read_selected_attributes(
                file_path, span.start, span.end, is_contract_attribute, handle=attributes_handle
            )
            selector = _matching_selector(attributes, reference)
            if not selector and not _is_component_call(attributes):
                continue
            # A call rule can use a custom selector attribute.  The byte match
            # still proves it references this DI; preserve that fact instead of
            # silently losing the caller just because the selector is unusual.
            yield (
                {
                    "caller_file": str(file_path),
                    "tag_offset": span.start,
                    "rule_eid": _value(attributes, "eid"),
                    "rule_name": _value(attributes, "Name"),
                    "rule_class": _value(attributes, "RuleClassName", "ClassType"),
                    "selector": selector or reference,
                    "source_name": _value(attributes, "Source"),
                    "component_list": _value(attributes, "ComponentList"),
                    "attributes": attributes,
                },
                mappings_from_attributes(attributes),
            )


@dataclass
class BuildSummary:
    integrator_file: str
    reference: str
    files_examined: int = 0
    referencing_files: int = 0
    odata_operations: int = 0
    caller_references: int = 0
    caller_mappings: int = 0
    warnings: list[str] = field(default_factory=list)


def iter_ifp_files(root: str | Path) -> Iterator[Path]:
    root_path = Path(root)
    if root_path.is_file():
        if root_path.suffix.casefold() == ".ifp":
            yield root_path
        return
    for path in root_path.rglob("*"):
        if path.is_file() and path.suffix.casefold() == ".ifp":
            yield path


def build_contracts(
    root: str | Path,
    integrator: str | Path,
    reference: str,
    store: ContractStore,
) -> BuildSummary:
    """Build a contract-only SQLite database with bounded-memory scans."""

    root_path = Path(root)
    integrator_path = Path(integrator)
    summary = BuildSummary(integrator_file=str(integrator_path), reference=reference)
    store.reset()
    try:
        for operation in iter_odata_operations(integrator_path):
            store.add_operation(operation)
            summary.odata_operations += 1
    except (OSError, UnicodeError) as error:
        summary.warnings.append(f"Could not inspect integrator {integrator_path}: {error}")

    for candidate in iter_ifp_files(root_path):
        if candidate.resolve() == integrator_path.resolve():
            continue
        summary.files_examined += 1
        try:
            references = iter_caller_references(candidate, reference)
            saw_reference = False
            for caller, mappings in references:
                saw_reference = True
                store.add_reference(caller, mappings)
                summary.caller_references += 1
                summary.caller_mappings += len(mappings)
            # This count means "files with a direct Rule call", not merely a
            # stray mention in comments or text content.
            if saw_reference:
                summary.referencing_files += 1
        except (OSError, UnicodeError) as error:
            summary.warnings.append(f"Could not inspect {candidate}: {error}")

    store.put_metadata(
        {
            "integrator_file": str(integrator_path),
            "reference": reference,
            "files_examined": str(summary.files_examined),
            "referencing_files": str(summary.referencing_files),
        }
    )
    store.commit()
    return summary
