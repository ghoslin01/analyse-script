"""Extract only Data Integrator API contracts and their direct callers."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
import hashlib
import json
from os import stat_result
from pathlib import Path
import re

from .config import DEFAULT_RULE_CONFIG, RuleConfig
from .store import ContractStore
from .xmlbytes import (
    AttributeRead,
    MalformedXMLStructure,
    TagScanLimitExceeded,
    UnsupportedIFPEncoding,
    XMLTagSpan,
    detect_xml_encoding,
    find_tag_span,
    iter_xml_visible_matches,
    read_tag_attributes,
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


@dataclass(frozen=True)
class ProgressUpdate:
    """Byte-accurate progress for the integrator and corpus scans."""

    path: Path
    scanned_bytes: int
    file_size: int
    file_index: int
    file_count: int
    completed_bytes: int
    total_bytes: int
    stage: str


ProgressCallback = Callable[[ProgressUpdate], None]


class InputChangedDuringScan(OSError):
    """An IFP changed after its cache identity was recorded."""


def _ensure_unchanged(path: Path, before: stat_result) -> None:
    after = path.stat()
    if (
        after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise InputChangedDuringScan(
            f"IFP changed while it was being scanned: {path}; rerun after writes stop"
        )


def is_contract_attribute(
    name: str, rule_config: RuleConfig = DEFAULT_RULE_CONFIG
) -> bool:
    lowered = name.casefold()
    if lowered in DIRECT_ATTRIBUTE_NAMES:
        return True
    if any(
        lowered == alias.casefold()
        for aliases in rule_config.aliases.values()
        for alias in aliases
    ):
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


def _is_rule(span: XMLTagSpan) -> bool:
    return span.name.rsplit(":", 1)[-1].casefold() == "rule"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "y", "on"}


def _source_name(
    attributes: dict[str, str], rule_config: RuleConfig = DEFAULT_RULE_CONFIG
) -> str | None:
    exact = _value(attributes, *rule_config.attribute_names("source"))
    return exact or _value_ending_with(attributes, "source")


def _api_path(
    attributes: dict[str, str], rule_config: RuleConfig = DEFAULT_RULE_CONFIG
) -> str | None:
    exact = _value(attributes, *rule_config.attribute_names("path"))
    return exact or _value_ending_with(attributes, "path", "url", "uri")


def _base_url(
    attributes: dict[str, str], rule_config: RuleConfig = DEFAULT_RULE_CONFIG
) -> str | None:
    exact = _value(attributes, *rule_config.attribute_names("base_url"))
    return exact or _value_ending_with(attributes, "url", "uri")


def _value_ending_with(attributes: dict[str, str], *suffixes: str) -> str | None:
    lowered_suffixes = tuple(suffix.casefold() for suffix in suffixes)
    for name, value in attributes.items():
        if value and name.casefold().endswith(lowered_suffixes):
            return value
    return None


def _api_classification(
    attributes: dict[str, str], rule_config: RuleConfig = DEFAULT_RULE_CONFIG
) -> str | None:
    rule_class = _rule_suffix(_value(attributes, "RuleClassName", "ClassType"))
    if rule_config.is_api_rule(rule_class):
        return "CONFIRMED"
    method = _value(attributes, *rule_config.attribute_names("method")) or _value_ending_with(
        attributes, "method"
    )
    path = _api_path(attributes, rule_config)
    source = _source_name(attributes, rule_config)
    output = _value(
        attributes,
        *rule_config.attribute_names("output"),
        *rule_config.attribute_names("result"),
    )
    if method and path:
        return "STRUCTURAL"
    if path and (source or output):
        return "CANDIDATE"
    return None


def _normalized_reference(value: str, *, ignore_case: bool) -> str:
    normalized = value.replace("\\", "/")
    return normalized.casefold() if ignore_case else normalized


def _reference_variants(references: Sequence[str]) -> tuple[str, ...]:
    variants: list[str] = []
    for reference in references:
        if not reference:
            continue
        for variant in (reference, reference.replace("\\", "/"), reference.replace("/", "\\")):
            if variant and variant not in variants:
                variants.append(variant)
    return tuple(variants)


def _matching_selector(
    attributes: dict[str, str],
    references: Sequence[str],
    *,
    ignore_case: bool,
    rule_config: RuleConfig = DEFAULT_RULE_CONFIG,
) -> str | None:
    normalized_references = tuple(
        _normalized_reference(reference, ignore_case=ignore_case) for reference in references
    )
    selector_names = {
        item.casefold() for item in rule_config.attribute_names("selector")
    }
    for name, value in attributes.items():
        normalized_value = _normalized_reference(value, ignore_case=ignore_case)
        if name.casefold() in selector_names and any(
            reference in normalized_value for reference in normalized_references
        ):
            return value
    return None


def _is_component_call(
    attributes: dict[str, str], rule_config: RuleConfig = DEFAULT_RULE_CONFIG
) -> bool:
    return (
        rule_config.is_component_rule(
            _rule_suffix(_value(attributes, "RuleClassName", "ClassType"))
        )
    )


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


def _attribute_diagnostics(
    read: AttributeRead,
    path: Path,
    span: XMLTagSpan,
    rule_class: str | None = None,
) -> list[dict[str, object]]:
    diagnostics: list[dict[str, object]] = []
    for item in read.truncated:
        diagnostics.append(
            _diagnostic(
                "WARNING",
                "ATTRIBUTE_VALUE_TRUNCATED",
                "Selected attribute exceeded the one-MiB capture limit; source offset is preserved.",
                path=path,
                offset=span.start,
                rule_class=rule_class,
                evidence={"attribute": item.name, "observed_bytes": item.observed_bytes},
            )
        )
    for name in sorted(set(read.duplicates)):
        diagnostics.append(
            _diagnostic(
                "WARNING",
                "DUPLICATE_XML_ATTRIBUTE",
                "Duplicate XML attribute encountered; the last value was retained.",
                path=path,
                offset=span.start,
                rule_class=rule_class,
                evidence={"attribute": name},
            )
        )
    return diagnostics


def _incompatible_rule(path: Path, offset: int, error: ValueError) -> dict[str, object]:
    return _diagnostic(
        "WARNING", "INCOMPATIBLE_RULE", "Rule could not be decoded; scanning continued.",
        path=path, offset=offset,
        evidence={"error_type": type(error).__name__, "reason": str(error)},
    )


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
    integrator_mappings: list[dict[str, object]] = field(default_factory=list)
    rule_classes: Counter[str] = field(default_factory=Counter)
    diagnostics: list[dict[str, object]] = field(default_factory=list)
    store: ContractStore | None = field(default=None, repr=False)

    def add(self, kind: str, row: dict[str, object]) -> None:
        if self.store is None:
            getattr(self, kind).append(row)
        else:
            writers = {
                "operations": self.store.add_operation,
                "data_sources": self.store.add_data_source,
                "integrator_mappings": self.store.add_integrator_mapping,
                "diagnostics": self.store.add_diagnostic,
            }
            writers[kind](row)

    def add_diagnostics(self, rows: list[dict[str, object]]) -> None:
        for row in rows:
            self.add("diagnostics", row)


def scan_integrator(
    path: str | Path,
    rule_config: RuleConfig = DEFAULT_RULE_CONFIG,
    *,
    on_bytes: Callable[[int], None] | None = None,
    store: ContractStore | None = None,
) -> IntegratorScan:
    """Scan only Rule and DataSource start tags in the designated DI file."""

    file_path = Path(path)
    encoding = detect_xml_encoding(file_path)
    scan = IntegratorScan(store=store)
    source_urls: dict[str, str] = {}
    api_shaped_unknown_classes: set[str] = set()
    product_stack: list[dict[str, str | None]] = []
    phase_stack: list[dict[str, str | None]] = []
    with file_path.open("rb") as random_handle:
        matches = iter_xml_visible_matches(
            file_path,
            ("<DataSource", "<Product", "</Product", "<Phase", "</Phase", "<Rule"),
            encoding=encoding,
            on_bytes=on_bytes,
        )
        for match in matches:
            try:
                span = find_tag_span(
                    file_path,
                    match.offset,
                    encoding=encoding,
                    handle=random_handle,
                    containing=False,
                )
            except (MalformedXMLStructure, TagScanLimitExceeded, UnicodeError) as error:
                if match.pattern != "<Rule":
                    raise
                scan.add("diagnostics", _incompatible_rule(file_path, match.offset, error))
                continue
            expected_name = match.pattern.removeprefix("</").removeprefix("<")
            if span is None or span.name != expected_name:
                continue
            if span.is_end:
                stack = product_stack if span.name == "Product" else phase_stack
                if stack:
                    stack.pop()
                else:
                    scan.add("diagnostics",
                        _diagnostic(
                            "WARNING",
                            "UNBALANCED_XML_STRUCTURE",
                            f"Closing </{span.name}> has no tracked opening tag.",
                            path=file_path,
                            offset=span.start,
                        )
                    )
                continue
            try:
                read = read_tag_attributes(
                    file_path,
                    span,
                    lambda name: is_contract_attribute(name, rule_config),
                    encoding=encoding,
                    handle=random_handle,
                )
            except (MalformedXMLStructure, UnicodeError) as error:
                if not _is_rule(span):
                    raise
                scan.add("diagnostics", _incompatible_rule(file_path, span.start, error))
                continue
            attributes = read.values
            scan.add_diagnostics(_attribute_diagnostics(read, file_path, span))
            if span.name == "Product":
                product = {
                    "name": _value(attributes, "Name"),
                    "eid": _value(attributes, "eid"),
                }
                for mapping in mappings_from_attributes(attributes):
                    scan.add("integrator_mappings",
                        {
                            "integrator_file": str(file_path),
                            "product_name": product["name"],
                            "product_eid": product["eid"],
                            "tag_offset": span.start,
                            "mapping_prefix": mapping["mapping_prefix"],
                            "exported_property": mapping["solution_data_item"],
                            "property_key": mapping["property_key"],
                            "direction": mapping["direction"],
                            "class_type": mapping["class_type"],
                            "attributes": mapping["attributes"],
                        }
                    )
                if not span.self_closing:
                    product_stack.append(product)
                continue
            if span.name == "Phase":
                if not span.self_closing:
                    phase_stack.append(
                        {"name": _value(attributes, "Name"), "eid": _value(attributes, "eid")}
                    )
                continue
            if span.name == "DataSource":
                name = _value(attributes, "Name", "SourceName", "DataSourceName")
                class_type = _value(attributes, "ClassType")
                base_url = _base_url(attributes, rule_config)
                marker = f"{class_type or ''} {base_url or ''}".casefold()
                if base_url or any(word in marker for word in ("odata", "swagger", "rest", "http")):
                    row = {
                        "integrator_file": str(file_path), "tag_offset": span.start,
                        "source_name": name, "class_type": class_type, "base_url": base_url,
                        "attributes": attributes,
                    }
                    scan.add("data_sources", row)
                    if store is None and name and base_url:
                        source_urls[name.casefold()] = base_url
                continue

            rule_class = _value(attributes, "RuleClassName", "ClassType") or "<missing>"
            scan.rule_classes[rule_class] += 1
            classification = _api_classification(attributes, rule_config)
            if classification is None:
                continue
            source_name = _source_name(attributes, rule_config)
            product = product_stack[-1] if product_stack else {}
            phase = phase_stack[-1] if phase_stack else {}
            scan.add("operations",
                {
                    "integrator_file": str(file_path), "tag_offset": span.start,
                    "rule_eid": _value(attributes, "eid"),
                    "rule_name": _value(attributes, "Name"), "rule_class": rule_class,
                    "product_name": product.get("name"),
                    "product_eid": product.get("eid"),
                    "phase_name": phase.get("name"),
                    "classification": classification,
                    "rule_type": _value(attributes, "RuleType"),
                    "disabled": int(_truthy(_value(attributes, "RuleDisabled"))),
                    "source_name": source_name,
                    "base_url": source_urls.get((source_name or "").casefold()),
                    "action": _value(attributes, *rule_config.attribute_names("method"))
                    or _value_ending_with(attributes, "method"),
                    "api_path": _api_path(attributes, rule_config),
                    "filter_expr": _value(
                        attributes, *rule_config.attribute_names("filter")
                    ),
                    "request_group": _value(
                        attributes, *rule_config.attribute_names("request")
                    ),
                    "target_group": _value(
                        attributes, *rule_config.attribute_names("target")
                    ),
                    "results_group": _value(
                        attributes, *rule_config.attribute_names("result")
                    ),
                    "output_group": _value(
                        attributes, *rule_config.attribute_names("output")
                    ),
                    "attributes": attributes,
                }
            )
            if classification != "CONFIRMED":
                api_shaped_unknown_classes.add(rule_class)
                scan.add("diagnostics",
                    _diagnostic(
                        "WARNING", "UNKNOWN_API_RULE_CLASS",
                        "API-shaped Rule has an unrecognized RuleClassName; preserved as evidence.",
                        path=file_path, offset=span.start, rule_class=rule_class,
                        evidence={
                            "classification": classification,
                            "method": _value(
                                attributes, *rule_config.attribute_names("method")
                            )
                            or _value_ending_with(attributes, "method"),
                            "path": _api_path(attributes, rule_config),
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
        if (
            rule_class != "<missing>"
            and rule_class not in api_shaped_unknown_classes
            and not rule_config.is_known_rule(rule_class)
        ):
            scan.add("diagnostics",
                _diagnostic(
                    "NOTICE", "UNKNOWN_RULE_CLASS",
                    f"Unrecognized RuleClassName occurred {count} time(s) in the integrator.",
                    path=file_path, rule_class=rule_class, evidence={"count": count},
                )
            )
    if product_stack or phase_stack:
        scan.add("diagnostics",
            _diagnostic(
                "WARNING",
                "UNCLOSED_XML_STRUCTURE",
                "The Data Integrator ended with an unclosed Product or Phase tag; "
                "captured operation ownership may be incomplete.",
                path=file_path,
                evidence={
                    "open_products": len(product_stack),
                    "open_phases": len(phase_stack),
                },
            )
        )
    return scan


@dataclass
class ReferenceEvidence:
    caller: dict[str, object] | None = None
    mappings: list[dict[str, object]] = field(default_factory=list)
    diagnostics: list[dict[str, object]] = field(default_factory=list)
    direct_reference: bool = True


def iter_caller_references(
    path: str | Path,
    references: Sequence[str],
    *,
    file_index: int = 0,
    file_count: int = 0,
    completed_bytes: int = 0,
    total_bytes: int = 0,
    progress: ProgressCallback | None = None,
    ignore_case: bool = False,
    include_dynamic: bool = False,
    rule_config: RuleConfig = DEFAULT_RULE_CONFIG,
) -> Iterator[ReferenceEvidence]:
    """Yield every start-tag reference hit, including unclassified evidence."""

    file_path = Path(path)
    encoding = detect_xml_encoding(file_path)
    reference_variants = _reference_variants(references)
    selector_names = {item.casefold() for item in rule_config.attribute_names("selector")}
    # Match the expression marker independently of attribute whitespace.
    # The attribute reader below verifies that it belongs to a selector.
    dynamic_patterns = ("$$",) if include_dynamic else ()
    search_patterns = reference_variants + dynamic_patterns
    file_size = file_path.stat().st_size
    last_tag_end = -1

    def on_bytes(scanned: int) -> None:
        if progress is not None:
            progress(
                ProgressUpdate(
                    path=file_path,
                    scanned_bytes=scanned,
                    file_size=file_size,
                    file_index=file_index,
                    file_count=file_count,
                    completed_bytes=completed_bytes,
                    total_bytes=total_bytes,
                    stage="caller",
                )
            )

    with file_path.open("rb") as random_handle:
        matches = iter_xml_visible_matches(
            file_path,
            search_patterns,
            encoding=encoding,
            ignore_case=ignore_case,
            on_bytes=on_bytes,
        )
        for match in matches:
            occurrence = match.offset
            if occurrence < last_tag_end:
                continue
            matched_reference = match.pattern
            is_dynamic = matched_reference in dynamic_patterns
            try:
                span = find_tag_span(
                    file_path, occurrence, encoding=encoding, handle=random_handle
                )
            except (MalformedXMLStructure, TagScanLimitExceeded, UnicodeError) as error:
                yield ReferenceEvidence(
                    diagnostics=[_incompatible_rule(file_path, occurrence, error)],
                    direct_reference=not is_dynamic,
                )
                continue
            if span is None:
                yield ReferenceEvidence(
                    diagnostics=[
                        _diagnostic(
                            "WARNING", "MALFORMED_REFERENCE_CONTEXT",
                            "Reference bytes were found but no containing XML start tag could be read.",
                            path=file_path, offset=occurrence,
                        )
                    ]
                )
                continue
            encoded_reference = encoding.encode(matched_reference)
            if occurrence + len(encoded_reference) > span.end:
                continue
            if not _is_rule(span):
                if is_dynamic:
                    continue
                last_tag_end = span.end
                yield ReferenceEvidence(
                    diagnostics=[
                        _diagnostic(
                            "NOTICE", "REFERENCE_ON_NON_RULE_TAG",
                            f"Direct reference was found on <{span.name}> rather than <Rule>.",
                            path=file_path, offset=span.start,
                            evidence={"tag": span.name, "reference": matched_reference},
                        )
                    ],
                    direct_reference=not is_dynamic,
                )
                continue

            last_tag_end = span.end
            try:
                read = read_tag_attributes(
                    file_path,
                    span,
                    lambda name: is_contract_attribute(name, rule_config),
                    encoding=encoding,
                    handle=random_handle,
                    force_capture_offsets=(occurrence,),
                    reference_patterns=reference_variants,
                    ignore_case=ignore_case,
                )
            except (MalformedXMLStructure, UnicodeError) as error:
                yield ReferenceEvidence(
                    diagnostics=[_incompatible_rule(file_path, span.start, error)],
                    direct_reference=not is_dynamic,
                )
                continue
            attributes = read.values
            selector = _matching_selector(
                attributes, reference_variants, ignore_case=ignore_case, rule_config=rule_config,
            )
            if is_dynamic and not selector and not read.matched_references:
                selector = next(
                    (value for name, value in attributes.items()
                     if name.casefold() in selector_names and value.lstrip().startswith("$$")),
                    None,
                )
                if selector is None:
                    continue
                yield ReferenceEvidence(
                    diagnostics=[
                        _diagnostic(
                            "NOTICE",
                            "DYNAMIC_REFERENCE_UNRESOLVED",
                            "Dynamic SelectComponent cannot be proven to target this Data Integrator.",
                            path=file_path,
                            offset=span.start,
                            rule_class=_value(attributes, "RuleClassName", "ClassType"),
                            evidence={"selector": selector or matched_reference},
                        )
                    ] + _attribute_diagnostics(read, file_path, span),
                    direct_reference=False,
                )
                continue
            if selector:
                matched_reference = next(
                    item for item in reference_variants
                    if _normalized_reference(item, ignore_case=ignore_case)
                    in _normalized_reference(selector, ignore_case=ignore_case)
                )
            elif read.matched_references:
                matched_reference = next(item for item in reference_variants if item in read.matched_references)
            known_component_rule = _is_component_call(attributes, rule_config)
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
                    evidence={"reference": matched_reference},
                )
            else:
                classification = "UNCLASSIFIED_REFERENCE"
                matching_attributes = {
                    name: value
                    for name, value in attributes.items()
                    if _normalized_reference(matched_reference, ignore_case=ignore_case)
                    in _normalized_reference(value, ignore_case=ignore_case)
                }
                diagnostic = _diagnostic(
                    "WARNING", "UNCLASSIFIED_REFERENCE_RULE",
                    "A Rule start tag contains the reference, but neither its class nor selector is recognized.",
                    path=file_path, offset=span.start, rule_class=rule_class,
                    evidence={
                        "reference": matched_reference,
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
                "selector": selector or matched_reference,
                "source_name": _value(attributes, "Source", "SourceName"),
                "component_list": _value(attributes, "ComponentList"),
                "attributes": attributes,
            }
            yield ReferenceEvidence(
                caller=caller, mappings=mappings_from_attributes(attributes),
                diagnostics=(
                    ([diagnostic] if diagnostic is not None else [])
                    + _attribute_diagnostics(read, file_path, span, rule_class)
                ),
            )


@dataclass
class BuildSummary:
    integrator_file: str
    reference: str
    files_examined: int = 0
    files_scanned: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    referencing_files: int = 0
    data_sources: int = 0
    integrator_mappings: int = 0
    odata_operations: int = 0
    caller_references: int = 0
    caller_mappings: int = 0
    operation_links: int = 0
    diagnostics: int = 0
    warnings: list[str] = field(default_factory=list)


def iter_ifp_files(root: str | Path) -> Iterator[Path]:
    root_path = Path(root)
    if root_path.is_file():
        if root_path.suffix.casefold() == ".ifp":
            yield root_path
        return
    for path in root_path.rglob("*"):
        if path.suffix.casefold() == ".ifp" and path.is_file():
            yield path


def _normalized_operation_names(value: str | None) -> set[str]:
    if not value:
        return set()
    names: set[str] = set()
    for item in re.split(r"[,;|]", value):
        candidate = item.strip().replace("\\", "/").rstrip("/")
        if not candidate:
            continue
        names.add(candidate.casefold())
        names.add(candidate.rsplit("/", 1)[-1].casefold())
    return names


class _OperationIndex:
    """Disk-backed, narrow Product lookup; never retain all API rows in RAM."""

    def __init__(self, store: ContractStore) -> None:
        self.store = store
        store.connection.execute("DROP TABLE IF EXISTS temp.operation_index")
        store.connection.execute(
            "CREATE TEMP TABLE operation_index AS SELECT id, product_name, "
            "casefold(product_name) AS product_key FROM odata_operations"
        )
        store.connection.execute("CREATE INDEX temp.idx_product_key ON operation_index(product_key)")
        self.operation_count = int(store.connection.execute("SELECT COUNT(*) FROM operation_index").fetchone()[0])
        self.product_count = int(store.connection.execute(
            "SELECT COUNT(DISTINCT product_key) FROM operation_index WHERE product_key != ''"
        ).fetchone()[0])


def _resolve_caller_operations(
    caller: dict[str, object],
    operations: _OperationIndex,
    path: Path,
) -> tuple[Iterator[tuple[int, str, dict[str, object]]], dict[str, object] | None]:
    targets = _normalized_operation_names(str(caller.get("component_list") or ""))
    targets.update(_normalized_operation_names(str(caller.get("source_name") or "")))
    store = operations.store
    # A temporary target table avoids SQLite's parameter-count limit for
    # large exported ComponentList values.
    store.connection.execute("CREATE TEMP TABLE IF NOT EXISTS caller_targets(name TEXT PRIMARY KEY)")
    store.connection.execute("DELETE FROM caller_targets")
    store.connection.executemany("INSERT INTO caller_targets VALUES (?)", ((name,) for name in targets))
    exact_sql = "SELECT id, product_name FROM operation_index WHERE product_key IN (SELECT name FROM caller_targets)"
    has_exact = store.connection.execute("SELECT EXISTS(" + exact_sql + ")").fetchone()[0]
    status = None
    query = "SELECT id, product_name FROM operation_index"
    if has_exact:
        status = "EXACT_PRODUCT"
        query = exact_sql
    elif not targets and operations.product_count == 1 and operations.operation_count:
        status = "SINGLE_PRODUCT_FALLBACK"
        query += " WHERE product_key != ''"
    elif not targets and operations.operation_count == 1:
        status = "SINGLE_OPERATION_FALLBACK"
    if status:
        evidence_targets = sorted(targets)
        return (
            ((int(row["id"]), status, {"targets": evidence_targets, "product": row["product_name"]})
             for row in store.iter_rows(query)),
            None,
        )
    products = [row[0] for row in store.iter_rows(
        "SELECT DISTINCT product_key FROM operation_index WHERE product_key != '' ORDER BY product_key LIMIT 100"
    )]
    code = "NO_API_OPERATION" if not operations.operation_count else "AMBIGUOUS_OPERATION_TARGET"
    message = (
        "The Data Integrator contains no recognized API operation."
        if not operations.operation_count
        else "Caller target could not be resolved to one Data Integrator Product."
    )
    evidence = {
        "targets": sorted(targets), "available_products": products,
        "operation_count": operations.operation_count,
    }
    if operations.product_count > len(products):
        evidence["available_products_truncated"] = True
        evidence["product_count"] = operations.product_count
    return (
        iter(()),
        _diagnostic(
            "WARNING", code, message, path=path,
            offset=int(caller.get("tag_offset") or 0),
            rule_class=str(caller.get("rule_class") or "") or None,
            evidence=evidence,
        ),
    )


def build_contracts(
    root: str | Path,
    integrator: str | Path,
    reference: str | Sequence[str],
    store: ContractStore,
    *,
    progress: ProgressCallback | None = None,
    ignore_case: bool = False,
    include_dynamic: bool = False,
    resume: bool = False,
    rule_config: RuleConfig = DEFAULT_RULE_CONFIG,
) -> BuildSummary:
    """Build a contract-only SQLite database with bounded-memory scans."""

    root_path = Path(root)
    integrator_path = Path(integrator)
    if not root_path.exists():
        raise FileNotFoundError(f"IFP corpus does not exist: {root_path}")
    if not integrator_path.is_file():
        raise FileNotFoundError(f"Data Integrator IFP does not exist: {integrator_path}")
    references = (reference,) if isinstance(reference, str) else tuple(reference)
    references = tuple(item for item in references if item)
    if not references:
        raise ValueError("reference must not be empty")

    summary = BuildSummary(
        integrator_file=str(integrator_path), reference=", ".join(references)
    )
    integrator_stat = integrator_path.stat()
    signature_payload = {
        # Invalidate callers extracted before rule recovery / selector fixes.
        "format": 4,
        "integrator": str(integrator_path.resolve()),
        "integrator_size": integrator_stat.st_size,
        "integrator_mtime_ns": integrator_stat.st_mtime_ns,
        "references": references,
        "ignore_case": ignore_case,
        "include_dynamic": include_dynamic,
        "rule_config": rule_config.signature_payload(),
    }
    reference_signature = hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    can_resume = resume and store.metadata_value("reference_signature") == reference_signature
    if can_resume:
        store.reset_integrator_results(str(integrator_path))
        store.connection.execute("DELETE FROM scan_files WHERE file_path = ?", (str(integrator_path),))
    else:
        store.reset()
    integrator_resolved = integrator_path.resolve()
    total_bytes = integrator_stat.st_size
    # The selected integrator counts even when it lives outside ``root``.
    duplicate_integrator_names = 1
    for candidate in iter_ifp_files(root_path):
        if candidate.resolve() == integrator_resolved:
            continue
        if candidate.name.casefold() == integrator_path.name.casefold():
            duplicate_integrator_names += 1
        summary.files_examined += 1
        try:
            total_bytes += candidate.stat().st_size
        except OSError:
            # Retry and record the failure during the scan below.
            pass

    total_files = summary.files_examined + 1

    def integrator_progress(scanned: int) -> None:
        if progress is not None:
            progress(
                ProgressUpdate(
                    path=integrator_path,
                    scanned_bytes=scanned,
                    file_size=integrator_stat.st_size,
                    file_index=1,
                    file_count=total_files,
                    completed_bytes=0,
                    total_bytes=total_bytes,
                    stage="integrator",
                )
            )

    try:
        integrator_scan = scan_integrator(
            integrator_path, rule_config, on_bytes=integrator_progress, store=store,
        )
        _ensure_unchanged(integrator_path, integrator_stat)
    except (OSError, UnicodeError, MalformedXMLStructure, TagScanLimitExceeded, UnsupportedIFPEncoding) as error:
        # The caller corpus can still yield useful evidence if the selected
        # integrator is unreadable. Publication remains blocked by files_failed.
        integrator_scan = IntegratorScan()
        summary.files_failed += 1
        message = f"Could not inspect {integrator_path}: {error}"
        summary.warnings.append(message)
        store.add_diagnostic(_diagnostic("ERROR", "FILE_SCAN_FAILED", message, path=integrator_path))
        store.mark_file_scanned(
            str(integrator_path), integrator_stat.st_size, integrator_stat.st_mtime_ns,
            reference_signature, "failed", 0, 0, 1,
        )
    store.connection.execute(
        "UPDATE odata_operations SET base_url = ("
        "SELECT base_url FROM data_sources WHERE casefold(data_sources.source_name) = "
        "casefold(odata_operations.source_name) AND base_url IS NOT NULL "
        "ORDER BY tag_offset DESC LIMIT 1) WHERE base_url IS NULL "
        "AND source_name IS NOT NULL AND source_name != ''"
    )
    summary.data_sources = int(store.connection.execute("SELECT COUNT(*) FROM data_sources").fetchone()[0])
    summary.integrator_mappings = int(store.connection.execute("SELECT COUNT(*) FROM integrator_mappings").fetchone()[0])
    summary.odata_operations = store.counts()["odata_operations"]
    store.put_rule_class_census(str(integrator_path), dict(integrator_scan.rule_classes))
    store.put_metadata(
        {
            "schema_version": "4",
            "database_kind": "ifp_contract_slicer",
            "scan_status": "running",
            "integrator_file": str(integrator_path),
            "reference": ", ".join(references),
            "reference_signature": reference_signature,
            "ignore_case": str(int(ignore_case)),
            "include_dynamic": str(int(include_dynamic)),
            "storage_policy": "reference_hits_only",
            "rule_config": json.dumps(rule_config.signature_payload(), sort_keys=True),
        }
    )
    store.commit()

    # Only previously stored evidence participates in stale-file cleanup.
    # A TEMP table also covers diagnostic-only files without publishing an
    # index of unrelated corpus files or keeping their paths in Python RAM.
    store.connection.execute("DROP TABLE IF EXISTS temp.pending_cleanup")
    store.connection.execute(
        "CREATE TEMP TABLE pending_cleanup(file_path TEXT PRIMARY KEY)"
    )
    if can_resume:
        store.connection.execute(
            "INSERT OR IGNORE INTO pending_cleanup SELECT file_path FROM scan_files "
            "UNION SELECT caller_file FROM caller_references "
            "UNION SELECT file_path FROM diagnostics WHERE file_path IS NOT NULL AND file_path != ?",
            (str(integrator_path),),
        )
        store.connection.execute("DELETE FROM pending_cleanup WHERE file_path = ?", (str(integrator_path),))
    basename_reference = any(
        "/" not in item and "\\" not in item and item.casefold() == integrator_path.name.casefold()
        for item in references
    )
    completed_bytes = integrator_stat.st_size
    candidates = (path for path in iter_ifp_files(root_path) if path.resolve() != integrator_resolved)
    for candidate_number, candidate in enumerate(candidates, start=2):
        candidate_text = str(candidate)
        store.connection.execute("DELETE FROM pending_cleanup WHERE file_path = ?", (candidate_text,))
        try:
            candidate_stat = candidate.stat()
        except OSError as error:
            message = f"Could not stat {candidate}: {error}"
            store.clear_file_results(candidate_text)
            store.add_diagnostic(_diagnostic("ERROR", "FILE_STAT_FAILED", message, path=candidate))
            summary.diagnostics += 1
            summary.files_failed += 1
            summary.warnings.append(message)
            store.mark_file_scanned(candidate_text, -1, -1, reference_signature, "failed", 0, 0, 1)
            store.commit()
            continue
        if can_resume and store.file_is_cached(
            candidate_text,
            candidate_stat.st_size,
            candidate_stat.st_mtime_ns,
            reference_signature,
        ):
            summary.files_skipped += 1
            if progress is not None:
                progress(
                    ProgressUpdate(
                        path=candidate,
                        scanned_bytes=candidate_stat.st_size,
                        file_size=candidate_stat.st_size,
                        file_index=candidate_number,
                        file_count=total_files,
                        completed_bytes=completed_bytes,
                        total_bytes=total_bytes,
                        stage="cached",
                    )
                )
            completed_bytes += candidate_stat.st_size
            continue
        store.clear_file_results(candidate_text)
        summary.files_scanned += 1
        saw_reference = False
        file_callers = 0
        file_diagnostics = 0
        try:
            for evidence in iter_caller_references(
                candidate,
                references,
                file_index=candidate_number,
                file_count=total_files,
                completed_bytes=completed_bytes,
                total_bytes=total_bytes,
                progress=progress,
                ignore_case=ignore_case,
                include_dynamic=include_dynamic,
                rule_config=rule_config,
            ):
                saw_reference = saw_reference or evidence.direct_reference
                if evidence.caller is not None:
                    store.add_reference(evidence.caller, evidence.mappings)
                    summary.caller_references += 1
                    file_callers += 1
                    summary.caller_mappings += len(evidence.mappings)
                for diagnostic in evidence.diagnostics:
                    store.add_diagnostic(diagnostic)
                    summary.diagnostics += 1
                    file_diagnostics += 1
            _ensure_unchanged(candidate, candidate_stat)
            if saw_reference:
                summary.referencing_files += 1
            # A completed miss is deliberately not persisted. The corpus is
            # streamed to discover references, but the published database is
            # a focused contract slice rather than a whole-project file index.
            # Matching files remain resumable; misses are cheaply rechecked on
            # a later build.
            if saw_reference:
                store.mark_file_scanned(
                    candidate_text,
                    candidate_stat.st_size,
                    candidate_stat.st_mtime_ns,
                    reference_signature,
                    "complete",
                    1,
                    file_callers,
                    file_diagnostics,
                )
            store.commit()
        except (
            OSError,
            UnicodeError,
            MalformedXMLStructure,
            TagScanLimitExceeded,
            UnsupportedIFPEncoding,
        ) as error:
            message = f"Could not inspect {candidate}: {error}"
            summary.warnings.append(message)
            store.add_diagnostic(_diagnostic("ERROR", "FILE_SCAN_FAILED", message, path=candidate))
            summary.diagnostics += 1
            summary.files_failed += 1
            store.mark_file_scanned(
                candidate_text,
                candidate_stat.st_size,
                candidate_stat.st_mtime_ns,
                reference_signature,
                "failed",
                int(saw_reference),
                file_callers,
                file_diagnostics + 1,
            )
            store.commit()
        finally:
            completed_bytes += candidate_stat.st_size

    for row in store.iter_rows("SELECT file_path FROM pending_cleanup"):
        store.clear_file_results(str(row[0]))
    store.connection.execute("DROP TABLE pending_cleanup")

    # Operations are refreshed every run, so rebuild these narrow links for
    # both newly scanned and resume-skipped caller rows.
    store.connection.execute("DELETE FROM caller_operation_links")
    store.delete_diagnostics_by_codes(("AMBIGUOUS_OPERATION_TARGET", "NO_API_OPERATION"))
    summary.operation_links = 0
    operation_records = _OperationIndex(store)
    for row in store.iter_rows("SELECT * FROM caller_references ORDER BY id"):
        caller = dict(row)
        links, link_diagnostic = _resolve_caller_operations(
            caller, operation_records, Path(str(caller["caller_file"]))
        )
        for operation_id, resolution_status, link_evidence in links:
            store.add_operation_link(
                int(caller["id"]), operation_id, resolution_status, link_evidence
            )
            summary.operation_links += 1
        if link_diagnostic is not None:
            store.add_diagnostic(link_diagnostic)

    if basename_reference and duplicate_integrator_names > 1:
        store.add_diagnostic(
            _diagnostic(
                "WARNING",
                "AMBIGUOUS_INTEGRATOR_FILENAME",
                "The corpus contains multiple IFP files with the selected Data Integrator filename.",
                path=integrator_path,
                evidence={
                    "filename": integrator_path.name,
                    "candidate_count": duplicate_integrator_names,
                },
            )
        )
        summary.diagnostics += 1

    counts = store.counts()
    summary.caller_references = counts["caller_references"]
    summary.caller_mappings = counts["caller_mappings"]
    summary.operation_links = store.operation_link_count()
    summary.diagnostics = store.diagnostic_count()
    summary.referencing_files = store.referencing_file_count(reference_signature)
    status = (
        "partial_with_errors"
        if summary.files_failed
        else "complete_with_diagnostics"
        if summary.diagnostics
        else "complete"
    )
    store.put_metadata(
        {
            "schema_version": "4", "scan_status": status,
            "database_kind": "ifp_contract_slicer",
            "integrator_file": str(integrator_path), "reference": ", ".join(references),
            "reference_signature": reference_signature,
            "ignore_case": str(int(ignore_case)),
            "include_dynamic": str(int(include_dynamic)),
            "storage_policy": "reference_hits_only",
            "rule_config": json.dumps(rule_config.signature_payload(), sort_keys=True),
            "files_examined": str(summary.files_examined),
            "files_scanned": str(summary.files_scanned),
            "files_skipped": str(summary.files_skipped),
            "files_failed": str(summary.files_failed),
            "referencing_files": str(summary.referencing_files),
        }
    )
    store.commit()
    store.compact_if_wasteful()
    return summary
