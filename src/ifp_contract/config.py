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
    api_rule_attributes: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)

    @property
    def known_rule_classes(self) -> frozenset[str]:
        return self.api_rule_classes | self.component_rule_classes | self.non_api_rule_classes

    def attribute_names(self, concept: str) -> tuple[str, ...]:
        return self.aliases.get(concept, ())

    def attribute_names_for_rule(self, rule_class: str | None, concept: str) -> tuple[str, ...]:
        """Return global aliases plus the declared attributes for this API class."""
        custom = self.api_rule_attributes.get(_suffix(rule_class or "").casefold(), {}).get(concept, ())
        return tuple(dict.fromkeys(self.attribute_names(concept) + custom))

    @property
    def all_attribute_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            name for aliases in (*self.aliases.values(),
                                  *(names for entry in self.api_rule_attributes.values()
                                    for names in entry.values()))
            for name in aliases
        ))

    def method_for_rule_class(self, value: str | None) -> str | None:
        candidate = _suffix(value or "").casefold()
        for rule_class, method in self.method_by_rule_class_suffix.items():
            if _suffix(rule_class).casefold() == candidate:
                return method.strip().upper()
        return None

    def is_api_rule(self, value: str | None) -> bool:
        return self._contains_class(self.api_rule_classes, value)

    def is_component_rule(self, value: str | None) -> bool:
        return self._contains_class(self.component_rule_classes, value)

    def is_known_rule(self, value: str | None) -> bool:
        return self._contains_class(self.known_rule_classes, value)

    @staticmethod
    def _contains_class(classes: frozenset[str], value: str | None) -> bool:
        candidate = _suffix(value or "").casefold()
        return any(item.casefold() == candidate for item in classes)

    def signature_payload(self) -> dict[str, object]:
        return {
            "api_rule_classes": sorted(self.api_rule_classes),
            "component_rule_classes": sorted(self.component_rule_classes),
            "non_api_rule_classes": sorted(self.non_api_rule_classes),
            "aliases": {key: list(value) for key, value in sorted(self.aliases.items())},
            "method_by_rule_class_suffix": dict(
                sorted(self.method_by_rule_class_suffix.items())
            ),
            "api_rule_attributes": {
                rule_class: {concept: list(names) for concept, names in sorted(concepts.items())}
                for rule_class, concepts in sorted(self.api_rule_attributes.items())
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

    def classes(key: str, defaults: frozenset[str]) -> frozenset[str]:
        values = payload.get(key, [])
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(f"{key} must be a JSON string array")
        return defaults | frozenset(_suffix(item) for item in values)

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

    methods = payload.get("method_by_rule_class_suffix", {})
    if not isinstance(methods, dict):
        raise ValueError("method_by_rule_class_suffix must be a JSON object")
    for rule_class, method in methods.items():
        if not _suffix(rule_class).strip() or not isinstance(method, str) or not method.strip():
            raise ValueError(
                "method_by_rule_class_suffix must map non-empty rule class names "
                "to non-empty HTTP method strings"
            )

    api_attributes = payload.get("api_rule_attributes", {})
    if not isinstance(api_attributes, dict):
        raise ValueError("api_rule_attributes must be a JSON object")
    rule_attributes: dict[str, dict[str, tuple[str, ...]]] = {}
    for rule_class, concepts in api_attributes.items():
        if not isinstance(rule_class, str) or not _suffix(rule_class).strip() or not isinstance(concepts, dict):
            raise ValueError("api_rule_attributes must map rule class names to objects")
        normalized: dict[str, tuple[str, ...]] = {}
        for concept, names in concepts.items():
            if concept not in DEFAULT_ALIASES:
                raise ValueError(f"Unknown api_rule_attributes concept: {concept}")
            if not isinstance(names, list) or not names or not all(isinstance(name, str) and name for name in names):
                raise ValueError(f"api_rule_attributes.{rule_class}.{concept} must be a non-empty string array")
            normalized[concept] = tuple(dict.fromkeys(names))
        rule_attributes[_suffix(rule_class).casefold()] = normalized

    return RuleConfig(
        api_rule_classes=classes("api_rule_classes", DEFAULT_RULE_CONFIG.api_rule_classes),
        component_rule_classes=classes(
            "component_rule_classes", DEFAULT_RULE_CONFIG.component_rule_classes
        ),
        non_api_rule_classes=classes(
            "non_api_rule_classes", DEFAULT_RULE_CONFIG.non_api_rule_classes
        ),
        aliases=aliases,
        method_by_rule_class_suffix={
            _suffix(key).casefold(): value.strip().upper()
            for key, value in methods.items()
        },
        api_rule_attributes=rule_attributes,
    )
