"""Extensible Rule and attribute vocabulary configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    "source": ("IRISSource", "SourceName", "Source", "DataSourceName"),
    "method": ("HTTPMethod", "Action", "IRISAction"),
    "path": (
        "ResourcePath", "OperationPath", "RelativePath", "IRISPath", "Path",
        "URL", "Uri", "Endpoint",
    ),
    "base_url": ("ServiceRootUri", "EndPointURL", "BaseURL", "BaseUri", "Endpoint"),
    "filter": ("Filter", "IRISFilter", "FilterDataItem", "Query"),
    "request": ("QueryInputDataGroup", "RequestDataGroup", "InputDataGroup", "Input"),
    "target": ("TargetDataGroup",),
    "result": ("ResultsDataGroup", "ResultDataGroup"),
    "output": ("OutputDataGroup", "Output"),
    "payload": ("Payload",),
    "error_code": ("HttpCodeDataItem", "ErrorCodeDataItem"),
    "error_message": ("HttpMessageDataItem", "ErrorMsgDataItem"),
    "context": (),
    "header_name": ("HTTPHeaderName",),
    "header_value": ("HTTPHeaderValue",),
    "language": ("AcceptLanguage",),
    "selector": (
        "SelectComponent", "ComponentList", "Component", "ComponentName",
        "ComponentPath", "CallComponent", "TargetComponent", "Source", "SourceName",
    ),
}


def _suffix(value: str) -> str:
    return value.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class RuleConfig:
    api_rule_classes: frozenset[str] = frozenset(
        {"InvokeIRISRule", "SwaggerIntegrationRule"}
    )
    component_rule_classes: frozenset[str] = frozenset(
        {"CallComponentRule", "BroadcastRule"}
    )
    non_api_rule_classes: frozenset[str] = frozenset(
        {
            "CompareMultiListValuesRule", "ContainerRule", "EvaluateRule",
            "ExpressionRule", "GotoRule", "RepeatRule", "SetValueRule",
            "AssignRule",
        }
    )
    aliases: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_ALIASES)
    )
    method_by_rule_class_suffix: dict[str, str] = field(default_factory=dict)
    exact_api_rule_classes: frozenset[str] = frozenset()
    method_by_rule_class: dict[str, str] = field(default_factory=dict)
    api_rule_attributes: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    exact_api_rule_attributes: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    api_rule_attribute_overrides: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    exact_api_rule_attribute_overrides: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)

    @property
    def known_rule_classes(self) -> frozenset[str]:
        return self.api_rule_classes | self.component_rule_classes | self.non_api_rule_classes

    def attribute_names(self, concept: str) -> tuple[str, ...]:
        return self.aliases.get(concept, ())

    def attribute_names_for_rule(self, rule_class: str | None, concept: str) -> tuple[str, ...]:
        """Return global aliases plus the declared attributes for this API class."""
        full = (rule_class or "").strip().casefold()
        override = self.exact_api_rule_attribute_overrides.get(full, {}).get(concept)
        if override is None:
            override = self.api_rule_attribute_overrides.get(
                _suffix(full).casefold(), {}
            ).get(concept)
        custom = self.exact_api_rule_attributes.get(full, {}).get(concept)
        if custom is None:
            custom = self.api_rule_attributes.get(_suffix(full).casefold(), {}).get(concept, ())
        if override is not None:
            return tuple(dict.fromkeys(override + self.attribute_names(concept) + custom))
        return tuple(dict.fromkeys(self.attribute_names(concept) + custom))

    def has_attribute_override(self, rule_class: str | None, concept: str) -> bool:
        full = (rule_class or "").strip().casefold()
        return (concept in self.exact_api_rule_attribute_overrides.get(full, {}) or
                concept in self.api_rule_attribute_overrides.get(_suffix(full).casefold(), {}))

    @property
    def all_attribute_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            name for aliases in (*self.aliases.values(),
                                  *(names for entry in self.api_rule_attributes.values()
                                    for names in entry.values()),
                                  *(names for entry in self.exact_api_rule_attributes.values()
                                    for names in entry.values()),
                                  *(names for entry in self.api_rule_attribute_overrides.values()
                                    for names in entry.values()),
                                  *(names for entry in self.exact_api_rule_attribute_overrides.values()
                                    for names in entry.values()))
            for name in aliases
        ))

    def method_for_rule_class(self, value: str | None) -> str | None:
        resolution = self.method_resolution(value)
        return resolution["method"] if resolution else None

    def method_resolution(self, value: str | None) -> dict[str, str] | None:
        full = (value or "").strip().casefold()
        if full in self.method_by_rule_class:
            return {"method": self.method_by_rule_class[full], "match": "exact", "key": full}
        suffix = _suffix(full)
        if suffix in self.method_by_rule_class_suffix:
            return {"method": self.method_by_rule_class_suffix[suffix],
                    "match": "suffix", "key": suffix}
        return None

    def api_rule_resolution(self, value: str | None) -> dict[str, str] | None:
        full = (value or "").strip().casefold()
        if full and any(item.casefold() == full for item in self.exact_api_rule_classes):
            return {"match": "exact", "key": full}
        suffix = _suffix(full)
        if suffix and any(item.casefold() == suffix for item in self.api_rule_classes):
            return {"match": "suffix", "key": suffix}
        return None

    def is_api_rule(self, value: str | None) -> bool:
        return self.api_rule_resolution(value) is not None

    def is_component_rule(self, value: str | None) -> bool:
        return self._contains_class(self.component_rule_classes, value)

    def is_known_rule(self, value: str | None) -> bool:
        return self.is_api_rule(value) or self._contains_class(
            self.component_rule_classes | self.non_api_rule_classes, value
        )

    @staticmethod
    def _contains_class(classes: frozenset[str], value: str | None) -> bool:
        candidate = _suffix(value or "").casefold()
        return any(item.casefold() == candidate for item in classes)

    def signature_payload(self) -> dict[str, object]:
        return {
            "api_rule_classes": sorted(self.api_rule_classes),
            "api_rule_classes_exact": sorted(self.exact_api_rule_classes),
            "component_rule_classes": sorted(self.component_rule_classes),
            "non_api_rule_classes": sorted(self.non_api_rule_classes),
            "aliases": {key: list(value) for key, value in sorted(self.aliases.items())},
            "method_by_rule_class_suffix": dict(
                sorted(self.method_by_rule_class_suffix.items())
            ),
            "method_by_rule_class": dict(sorted(self.method_by_rule_class.items())),
            "api_rule_attributes": {
                rule_class: {concept: list(names) for concept, names in sorted(concepts.items())}
                for rule_class, concepts in sorted(self.api_rule_attributes.items())
            },
            "api_rule_attributes_exact": {
                rule_class: {concept: list(names) for concept, names in sorted(concepts.items())}
                for rule_class, concepts in sorted(self.exact_api_rule_attributes.items())
            },
            "api_rule_attribute_overrides": {
                rule_class: {concept: list(names) for concept, names in sorted(concepts.items())}
                for rule_class, concepts in sorted(self.api_rule_attribute_overrides.items())
            },
            "api_rule_attribute_overrides_exact": {
                rule_class: {concept: list(names) for concept, names in sorted(concepts.items())}
                for rule_class, concepts in sorted(self.exact_api_rule_attribute_overrides.items())
            },
        }


DEFAULT_RULE_CONFIG = RuleConfig()


def load_rule_config(path: str | Path | None) -> RuleConfig:
    """Load a JSON extension file and merge it with built-in vocabulary."""

    if path is None:
        return DEFAULT_RULE_CONFIG
    config_path = Path(path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not load rules config {config_path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"Rules config must contain a JSON object: {config_path}")

    def classes(key: str, defaults: frozenset[str], *, keep_qualified: bool = False) -> frozenset[str]:
        values = payload.get(key, [])
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(f"{key} must be a JSON string array")
        if any(not item.strip() for item in values):
            raise ValueError(f"{key} must contain non-empty class names")
        normalize = (lambda item: item.strip()) if keep_qualified else (lambda item: _suffix(item).strip())
        result = set(defaults)
        seen: dict[str, str] = {}
        for item in values:
            normalized = normalize(item)
            folded = normalized.casefold()
            if folded in seen:
                raise ValueError(
                    f"{key} contains duplicate normalized class names: "
                    f"{seen[folded]!r}, {item!r}"
                )
            result.add(normalized)
            seen[folded] = item
        return frozenset(result)

    aliases = dict(DEFAULT_ALIASES)
    custom_aliases = payload.get("attribute_aliases", {})
    if not isinstance(custom_aliases, dict):
        raise ValueError("attribute_aliases must be a JSON object")
    for concept, values in custom_aliases.items():
        if concept not in DEFAULT_ALIASES:
            raise ValueError(f"Unknown attribute alias concept: {concept}")
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(f"attribute_aliases.{concept} must be a JSON string array")
        aliases[concept] = tuple(dict.fromkeys(DEFAULT_ALIASES[concept] + tuple(values)))

    def method_mapping(key: str, *, exact: bool) -> dict[str, str]:
        methods = payload.get(key, {})
        if not isinstance(methods, dict):
            raise ValueError(f"{key} must be a JSON object")
        result: dict[str, str] = {}
        origins: dict[str, str] = {}
        for rule_class, method in methods.items():
            if (not isinstance(rule_class, str) or not rule_class.strip() or
                    not isinstance(method, str) or not method.strip()):
                raise ValueError(f"{key} must map non-empty rule class names to non-empty HTTP method strings")
            if exact and '.' not in rule_class:
                raise ValueError(f"{key} requires qualified class names")
            normalized = rule_class.strip().casefold() if exact else _suffix(rule_class).strip().casefold()
            if normalized in result:
                raise ValueError(
                    f"{key} contains duplicate normalized class names: "
                    f"{origins[normalized]!r}, {rule_class!r}"
                )
            result[normalized] = method.strip().upper()
            origins[normalized] = rule_class
        return result

    suffix_methods = method_mapping("method_by_rule_class_suffix", exact=False)
    exact_methods = method_mapping("method_by_rule_class", exact=True)

    def api_attributes(key: str, *, exact: bool) -> dict[str, dict[str, tuple[str, ...]]]:
        api_attributes = payload.get(key, {})
        if not isinstance(api_attributes, dict):
            raise ValueError(f"{key} must be a JSON object")
        rule_attributes: dict[str, dict[str, tuple[str, ...]]] = {}
        origins: dict[str, str] = {}
        for rule_class, concepts in api_attributes.items():
            if not isinstance(rule_class, str) or not rule_class.strip() or not isinstance(concepts, dict):
                raise ValueError(f"{key} must map rule class names to objects")
            if exact and '.' not in rule_class:
                raise ValueError(f"{key} requires qualified class names")
            normalized_class = rule_class.strip().casefold() if exact else _suffix(rule_class).strip().casefold()
            if normalized_class in rule_attributes:
                raise ValueError(
                    f"{key} contains duplicate normalized class names: "
                    f"{origins[normalized_class]!r}, {rule_class!r}"
                )
            normalized: dict[str, tuple[str, ...]] = {}
            for concept, names in concepts.items():
                if concept not in DEFAULT_ALIASES:
                    raise ValueError(f"Unknown {key} concept: {concept}")
                if (not isinstance(names, list) or not names or
                        not all(isinstance(name, str) and name for name in names)):
                    raise ValueError(f"{key}.{rule_class}.{concept} must be a non-empty string array")
                normalized[concept] = tuple(dict.fromkeys(names))
            rule_attributes[normalized_class] = normalized
            origins[normalized_class] = rule_class
        return rule_attributes

    suffix_attributes = api_attributes("api_rule_attributes", exact=False)
    exact_attributes = api_attributes("api_rule_attributes_exact", exact=True)
    suffix_attribute_overrides = api_attributes("api_rule_attribute_overrides", exact=False)
    exact_attribute_overrides = api_attributes("api_rule_attribute_overrides_exact", exact=True)

    exact_classes = classes("api_rule_classes_exact", frozenset(), keep_qualified=True)
    suffix_classes = classes("api_rule_classes", DEFAULT_RULE_CONFIG.api_rule_classes)
    component_classes = classes(
        "component_rule_classes", DEFAULT_RULE_CONFIG.component_rule_classes
    )
    non_api_classes = classes(
        "non_api_rule_classes", DEFAULT_RULE_CONFIG.non_api_rule_classes
    )
    non_api_suffixes = {item.casefold() for item in component_classes | non_api_classes}
    conflicts = {item.casefold() for item in suffix_classes} & non_api_suffixes
    if conflicts:
        raise ValueError(f"Rule classes cannot be both API and non-API/component: {', '.join(sorted(conflicts))}")

    for exact_class in exact_classes:
        if '.' not in exact_class:
            raise ValueError(
                "api_rule_classes_exact requires qualified class names; use api_rule_classes for suffix matching"
            )

    return RuleConfig(
        api_rule_classes=suffix_classes,
        exact_api_rule_classes=exact_classes,
        component_rule_classes=component_classes,
        non_api_rule_classes=non_api_classes,
        aliases=aliases,
        method_by_rule_class_suffix=suffix_methods,
        method_by_rule_class=exact_methods,
        api_rule_attributes=suffix_attributes,
        exact_api_rule_attributes=exact_attributes,
        api_rule_attribute_overrides=suffix_attribute_overrides,
        exact_api_rule_attribute_overrides=exact_attribute_overrides,
    )
