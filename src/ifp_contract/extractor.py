"""Extract only Data Integrator API contracts and their direct callers."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .store import ContractStore
from .stream import (
    TagSpan,
    ensure_supported_encoding,
    iter_occurrence_offsets,
    iter_target_start_tags,
    read_selected_attributes,
    TagScanLimitExceeded,
    UnsupportedIFPEncoding,
    tag_span_containing,
)


DIRECT_ATTRIBUTE_NAMES = frozenset(
    name.casefold()
    for name in (
        "eid", "Name", "RuleClassName", "ClassType", "RuleType", "RuleDisabled",
        "SelectComponent", "ComponentList", "Component", "ComponentName",
        "ComponentPath", "CallComponent", "TargetComponent", "Source", "SourceName",
        "IRISSource", "IRISAction", "Action", "HttpMethod", "URL", "Uri",
        "Endpoint", "EndPointURL", "BaseURL", "BaseUri", "ServiceRootUri", "Path",
        "ResourcePath", "OperationPath", "RelativePath", "IRISPath", "Filter",
        "IRISFilter", "FilterDataItem", "Query", "TargetDataGroup",
        "ResultsDataGroup", "ResultDataGroup", "QueryInputDataGroup",
        "RequestDataGroup", "InputDataGroup", "OutputDataGroup", "Input", "Output",
        "InputDataItem", "AdditionalHTTPDataGroups", "ErrorCodeDataItem",
        "ErrorMsgDataItem", "DataSourceName", "MappingSetName",
    )
)
MAPPING_SUFFIXES = (
    "_ClassType", "_SolutionDataItemMapping", "_DataItemMapping", "_ExportedProperty",
    "_PropertyKey", "_In", "_Out", "_PubIn", "_PubOut", "_readMapping",
    "_writeMapping", "_Mapping",
)
REFERENCE_ATTRIBUTE_NAMES = frozenset(
    name.casefold()
    for name in (
        "SelectComponent", "ComponentList", "Component", "ComponentName",
        "ComponentPath", "CallComponent", "TargetComponent", "Source", "SourceName",
    )
)
KNOWN_API_RULES = frozenset({"InvokeIRISRule", "SwaggerIntegrationRule"})
KNOWN_COMPONENT_RULES = frozenset({"CallComponentRule", "BroadcastRule"})
KNOWN_NON_API_RULES = frozenset(
    {
        "CompareMultiListValuesRule", "ContainerRule", "EvaluateRule", "ExpressionRule",
        "GotoRule", "RepeatRule", "SetValueRule",
    }
)
KNOWN_RULES = KNOWN_API_RULES | KNOWN_COMPONENT_RULES | KNOWN_NON_API_RULES

ProgressCallback = Callable[[Path, int, int, int], None]


def is_contract_attribute(name: str) -> bool:
    lowered = name.casefold()
    if lowered in DIRECT_ATTRIBUTE_NAMES:
        return True
    if any(lowered.endswith(suffix.casefold()) for suffix in MAPPING_SUFFIXES):
        return True
    # Custom HTTP rules often preserve the concept while renaming the prefix.
    return lowered.endswith(
        ("url", "uri", "path", "method", "source", "component", "module", "selector", "reference")
    )


def _value(attributes: dict[str, str], *names: str) -> str | None:
    by_lower = {key.casefold(): value for key, value in attributes.items()}
    for name in names:
        value = by_lower.get(name.casefold())
        if value not in (None, ""):
            return value
    return None


def _rule_suffix(rule_class: str | None) -> str:
    return (rule_class or "").rsplit(".", 1)[-1]


def _is_rule(span: TagSpan) -> bool:
    return span.name.rsplit(":", 1)[-1].casefold() == "rule"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "y", "on"}


def _source_name(attributes: dict[str, str]) -> str | None:
    exact = _value(attributes, "IRISSource", "SourceName", "Source", "DataSourceName")
    return exact or _value_ending_with(attributes, "source")


def _api_path(attributes: dict[str, str]) -> str | None:
    exact = _value(
        attributes, "ResourcePath", "OperationPath", "RelativePath", "IRISPath",
        "Path", "URL", "Uri", "Endpoint",
    )
    return exact or _value_ending_with(attributes, "path", "url", "uri")


def _base_url(attributes: dict[str, str]) -> str | None:
    exact = _value(
        attributes, "ServiceRootUri", "EndPointURL", "BaseURL", "BaseUri", "Endpoint"
    )
    return exact or _value_ending_with(attributes, "url", "uri")


def _value_ending_with(attributes: dict[str, str], *suffixes: str) -> str | None:
    lowered_suffixes = tuple(suffix.casefold() for suffix in suffixes)
    for name, value in attributes.items():
        if value and name.casefold().endswith(lowered_suffixes):
            return value
    return None


def _api_classification(attributes: dict[str, str]) -> str | None:
    rule_class = _rule_suffix(_value(attributes, "RuleClassName", "ClassType"))
    if rule_class in KNOWN_API_RULES:
        return "CONFIRMED"
    method = _value(attributes, "HTTPMethod", "Action", "IRISAction") or _value_ending_with(
        attributes, "method"
    )
    path = _api_path(attributes)
    source = _source_name(attributes)
    output = _value(attributes, "Output", "OutputDataGroup", "ResultsDataGroup")
    if method and path:
        return "STRUCTURAL"
    if path and (source or output):
        return "CANDIDATE"
    return None


def _matching_selector(attributes: dict[str, str], reference: str) -> str | None:
    reference_folded = reference.casefold()
    for name, value in attributes.items():
        if name.casefold() in REFERENCE_ATTRIBUTE_NAMES and reference_folded in value.casefold():
            return value
    return None


def _is_component_call(attributes: dict[str, str]) -> bool:
    return _rule_suffix(_value(attributes, "RuleClassName", "ClassType")) in KNOWN_COMPONENT_RULES


def _diagnostic(
    severity: str,
    code: str,
    message: str,
    *,
    path: Path | None = None,
    offset: int | None = None,
    rule_class: str | None = None,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "severity": severity,
        "code": code,
        "file_path": str(path) if path else None,
        "tag_offset": offset,
        "rule_class": rule_class,
        "message": message,
        "evidence": evidence or {},
    }


def mappings_from_attributes(attributes: dict[str, str]) -> list[dict[str, object]]:
    """Turn flattened ``*_ComponentMapping`` attributes into compact rows."""

    groups: dict[str, dict[str, str]] = defaultdict(dict)
    suffix_by_lower = sorted(
        ((suffix.casefold(), suffix[1:]) for suffix in MAPPING_SUFFIXES),
        key=lambda item: len(item[0]), reverse=True,
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
        solution = (
            group.get("SolutionDataItemMapping")
            or group.get("DataItemMapping")
            or group.get("ExportedProperty")
        )
        property_key = group.get("PropertyKey")
        if not solution and not property_key:
            continue
        incoming = _truthy(group.get("In")) or _truthy(group.get("PubIn"))
        outgoing = _truthy(group.get("Out")) or _truthy(group.get("PubOut"))
        direction = (
            "INOUT" if incoming and outgoing else "INPUT" if incoming
            else "OUTPUT" if outgoing else "UNKNOWN"
        )
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


@dataclass
class IntegratorScan:
    operations: list[dict[str, object]] = field(default_factory=list)
    data_sources: list[dict[str, object]] = field(default_factory=list)
    rule_classes: Counter[str] = field(default_factory=Counter)
    diagnostics: list[dict[str, object]] = field(default_factory=list)


def scan_integrator(path: str | Path) -> IntegratorScan:
    """Scan only Rule and DataSource start tags in the designated DI file."""

    file_path = Path(path)
    ensure_supported_encoding(file_path)
    scan = IntegratorScan()
    source_urls: dict[str, str] = {}
    with file_path.open("rb") as random_handle:
        spans = iter_target_start_tags(
            file_path, (b"DataSource", b"Rule"), handle=random_handle
        )
        for span in spans:
            attributes = read_selected_attributes(
                file_path, span.start, span.end, is_contract_attribute, handle=random_handle
            )
            if span.name == "DataSource":
                name = _value(attributes, "Name", "SourceName", "DataSourceName")
                class_type = _value(attributes, "ClassType")
                base_url = _base_url(attributes)
                marker = f"{class_type or ''} {base_url or ''}".casefold()
                if base_url or any(word in marker for word in ("odata", "swagger", "rest", "http")):
                    row = {
                        "integrator_file": str(file_path), "tag_offset": span.start,
                        "source_name": name, "class_type": class_type, "base_url": base_url,
                        "attributes": attributes,
                    }
                    scan.data_sources.append(row)
                    if name and base_url:
                        source_urls[name.casefold()] = base_url
                continue

            rule_class = _value(attributes, "RuleClassName", "ClassType") or "<missing>"
            scan.rule_classes[rule_class] += 1
            classification = _api_classification(attributes)
            if classification is None:
                continue
            source_name = _source_name(attributes)
            scan.operations.append(
                {
                    "integrator_file": str(file_path), "tag_offset": span.start,
                    "rule_eid": _value(attributes, "eid"),
                    "rule_name": _value(attributes, "Name"), "rule_class": rule_class,
                    "classification": classification,
                    "rule_type": _value(attributes, "RuleType"),
                    "disabled": int(_truthy(_value(attributes, "RuleDisabled"))),
                    "source_name": source_name,
                    "base_url": source_urls.get((source_name or "").casefold()),
                    "action": _value(attributes, "IRISAction", "Action", "HTTPMethod")
                    or _value_ending_with(attributes, "method"),
                    "api_path": _api_path(attributes),
                    "filter_expr": _value(
                        attributes, "Filter", "IRISFilter", "FilterDataItem", "Query"
                    ),
                    "request_group": _value(
                        attributes, "QueryInputDataGroup", "RequestDataGroup",
                        "InputDataGroup", "Input",
                    ),
                    "target_group": _value(attributes, "TargetDataGroup"),
                    "results_group": _value(
                        attributes, "ResultsDataGroup", "ResultDataGroup"
                    ),
                    "output_group": _value(attributes, "OutputDataGroup", "Output"),
                    "attributes": attributes,
                }
            )
            if classification != "CONFIRMED":
                scan.diagnostics.append(
                    _diagnostic(
                        "WARNING", "UNKNOWN_API_RULE_CLASS",
                        "API-shaped Rule has an unrecognized RuleClassName; preserved as evidence.",
                        path=file_path, offset=span.start, rule_class=rule_class,
                        evidence={
                            "classification": classification,
                            "method": _value(attributes, "HTTPMethod", "Action", "IRISAction")
                            or _value_ending_with(attributes, "method"),
                            "path": _api_path(attributes),
                        },
                    )
                )

    # DataSource declarations are usually before Rules, but exports are not
    # required to preserve that order.
    for operation in scan.operations:
        if not operation.get("base_url"):
            operation["base_url"] = source_urls.get(
                str(operation.get("source_name") or "").casefold()
            )

    for rule_class, count in scan.rule_classes.items():
        suffix = _rule_suffix(rule_class)
        if rule_class != "<missing>" and suffix not in KNOWN_RULES:
            scan.diagnostics.append(
                _diagnostic(
                    "NOTICE", "UNKNOWN_RULE_CLASS",
                    f"Unrecognized RuleClassName occurred {count} time(s) in the integrator.",
                    path=file_path, rule_class=rule_class, evidence={"count": count},
                )
            )
    return scan


@dataclass
class ReferenceEvidence:
    caller: dict[str, object] | None = None
    mappings: list[dict[str, object]] = field(default_factory=list)
    diagnostic: dict[str, object] | None = None


def iter_caller_references(
    path: str | Path,
    reference: str,
    *,
    file_index: int = 0,
    progress: ProgressCallback | None = None,
) -> Iterator[ReferenceEvidence]:
    """Yield every start-tag reference hit, including unclassified evidence."""

    file_path = Path(path)
    ensure_supported_encoding(file_path)
    needle = reference.encode("utf-8")
    file_size = file_path.stat().st_size
    seen_tag_offsets: set[int] = set()

    def on_bytes(scanned: int) -> None:
        if progress is not None:
            progress(file_path, scanned, file_size, file_index)

    with file_path.open("rb") as random_handle:
        for occurrence in iter_occurrence_offsets(file_path, needle, on_bytes=on_bytes):
            span = tag_span_containing(file_path, occurrence, handle=random_handle)
            if span is None:
                yield ReferenceEvidence(
                    diagnostic=_diagnostic(
                        "WARNING", "MALFORMED_REFERENCE_CONTEXT",
                        "Reference bytes were found but no containing XML start tag could be read.",
                        path=file_path, offset=occurrence,
                    )
                )
                continue
            if occurrence + len(needle) > span.end or span.start in seen_tag_offsets:
                continue
            seen_tag_offsets.add(span.start)
            if not _is_rule(span):
                yield ReferenceEvidence(
                    diagnostic=_diagnostic(
                        "NOTICE", "REFERENCE_ON_NON_RULE_TAG",
                        f"Direct reference was found on <{span.name}> rather than <Rule>.",
                        path=file_path, offset=span.start,
                        evidence={"tag": span.name, "reference": reference},
                    )
                )
                continue

            attributes = read_selected_attributes(
                file_path, span.start, span.end, is_contract_attribute, handle=random_handle
            )
            selector = _matching_selector(attributes, reference)
            known_component_rule = _is_component_call(attributes)
            rule_class = _value(attributes, "RuleClassName", "ClassType")
            if selector and known_component_rule:
                classification, diagnostic = "CONFIRMED", None
            elif selector:
                classification = "CONFIRMED_UNKNOWN_RULE"
                diagnostic = _diagnostic(
                    "WARNING", "UNKNOWN_CALL_RULE_CLASS",
                    "A known component selector directly references the integrator, but the Rule class is unknown.",
                    path=file_path, offset=span.start, rule_class=rule_class,
                    evidence={"selector": selector},
                )
            elif known_component_rule:
                classification = "INFERRED_UNKNOWN_SELECTOR"
                diagnostic = _diagnostic(
                    "WARNING", "UNKNOWN_REFERENCE_ATTRIBUTE",
                    "A component-call Rule contains the reference in an unrecognized attribute.",
                    path=file_path, offset=span.start, rule_class=rule_class,
                    evidence={"reference": reference},
                )
            else:
                classification = "UNCLASSIFIED_REFERENCE"
                matching_attributes = {
                    name: value
                    for name, value in attributes.items()
                    if reference.casefold() in value.casefold()
                }
                diagnostic = _diagnostic(
                    "WARNING", "UNCLASSIFIED_REFERENCE_RULE",
                    "A Rule start tag contains the reference, but neither its class nor selector is recognized.",
                    path=file_path, offset=span.start, rule_class=rule_class,
                    evidence={
                        "reference": reference,
                        "matching_attributes": matching_attributes,
                    },
                )
            caller = {
                "caller_file": str(file_path), "tag_offset": span.start,
                "rule_eid": _value(attributes, "eid"),
                "rule_name": _value(attributes, "Name"), "rule_class": rule_class,
                "classification": classification,
                "rule_type": _value(attributes, "RuleType"),
                "disabled": int(_truthy(_value(attributes, "RuleDisabled"))),
                "selector": selector or reference,
                "source_name": _value(attributes, "Source", "SourceName"),
                "component_list": _value(attributes, "ComponentList"),
                "attributes": attributes,
            }
            yield ReferenceEvidence(
                caller=caller, mappings=mappings_from_attributes(attributes),
                diagnostic=diagnostic,
            )


@dataclass
class BuildSummary:
    integrator_file: str
    reference: str
    files_examined: int = 0
    referencing_files: int = 0
    data_sources: int = 0
    odata_operations: int = 0
    caller_references: int = 0
    caller_mappings: int = 0
    diagnostics: int = 0
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
    *,
    progress: ProgressCallback | None = None,
) -> BuildSummary:
    """Build a contract-only SQLite database with bounded-memory scans."""

    root_path = Path(root)
    integrator_path = Path(integrator)
    if not root_path.exists():
        raise FileNotFoundError(f"IFP corpus does not exist: {root_path}")
    if not integrator_path.is_file():
        raise FileNotFoundError(f"Data Integrator IFP does not exist: {integrator_path}")
    if not reference:
        raise ValueError("reference must not be empty")

    summary = BuildSummary(integrator_file=str(integrator_path), reference=reference)
    store.reset()
    integrator_scan = scan_integrator(integrator_path)
    for data_source in integrator_scan.data_sources:
        store.add_data_source(data_source)
        summary.data_sources += 1
    for operation in integrator_scan.operations:
        store.add_operation(operation)
        summary.odata_operations += 1
    store.put_rule_class_census(str(integrator_path), dict(integrator_scan.rule_classes))
    for diagnostic in integrator_scan.diagnostics:
        store.add_diagnostic(diagnostic)
        summary.diagnostics += 1

    integrator_resolved = integrator_path.resolve()
    for candidate in iter_ifp_files(root_path):
        if candidate.resolve() == integrator_resolved:
            continue
        summary.files_examined += 1
        saw_reference = False
        try:
            for evidence in iter_caller_references(
                candidate, reference, file_index=summary.files_examined, progress=progress
            ):
                saw_reference = True
                if evidence.caller is not None:
                    store.add_reference(evidence.caller, evidence.mappings)
                    summary.caller_references += 1
                    summary.caller_mappings += len(evidence.mappings)
                if evidence.diagnostic is not None:
                    store.add_diagnostic(evidence.diagnostic)
                    summary.diagnostics += 1
            if saw_reference:
                summary.referencing_files += 1
        except (
            OSError,
            UnicodeError,
            TagScanLimitExceeded,
            UnsupportedIFPEncoding,
        ) as error:
            message = f"Could not inspect {candidate}: {error}"
            summary.warnings.append(message)
            store.add_diagnostic(_diagnostic("ERROR", "FILE_SCAN_FAILED", message, path=candidate))
            summary.diagnostics += 1

    status = "complete_with_diagnostics" if summary.diagnostics else "complete"
    store.put_metadata(
        {
            "schema_version": "2", "scan_status": status,
            "integrator_file": str(integrator_path), "reference": reference,
            "files_examined": str(summary.files_examined),
            "referencing_files": str(summary.referencing_files),
        }
    )
    store.commit()
    return summary
