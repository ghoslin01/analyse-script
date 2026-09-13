"""Standard trace semantics with explicit, validated project extensions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


STANDARD_KINDS = {
    'ContainerRule': 'container', 'EvaluateRule': 'evaluate', 'SetValueRule': 'set',
    'ExpressionRule': 'expression', 'RepeatRule': 'repeat', 'IncrementorRule': 'increment',
    'ResetDataRule': 'reset', 'CallComponentRule': 'call', 'GotoRule': 'goto',
}
ATTRIBUTES = frozenset('''Expression OutputProperty Type FromType PropertyName VariableName
PropertyGroupName PropertyGroupInstanceName FromPropertyName FromVariableName
FromPropertyGroupName FromPropertyGroupInstanceName FromValue ResetProperty ResetPropertyGroup
ResetVariable IncrementBy EndInstance DataGroupName Phase OperationType SelectComponent Trim
ComponentList Source LinkReference RuleDisabled'''.split())


class TraceConfig:
    """Custom mappings use canonical attribute -> project attribute; raw XML stays intact."""

    def __init__(self, path: Path | None = None):
        payload = json.loads(path.read_text(encoding='utf-8')) if path else {}
        self.rules = {name.casefold(): {'kind': kind, 'attributes': {}, 'defaults': {},
                                      'origin': 'standard'} for name, kind in STANDARD_KINDS.items()}
        legacy = payload.get('trace_rule_kinds', {})
        if not isinstance(legacy, dict):
            raise ValueError('trace_rule_kinds must be an object')
        for name, kind in legacy.items():
            if not name or kind not in STANDARD_KINDS.values():
                raise ValueError('trace_rule_kinds must map class names to supported trace kinds')
            key = name.rsplit('.', 1)[-1].casefold()
            if key in self.rules and kind != self.rules[key]['kind']:
                raise ValueError(f'Use trace.rules with override=true to override {name}')
            self.rules[key] = {'kind': kind, 'attributes': {}, 'defaults': {}, 'origin': 'legacy_extension'}
        extension = payload.get('trace', {})
        if not isinstance(extension, dict) or set(extension) - {'schema_version', 'rules'}:
            raise ValueError('trace supports only schema_version and rules')
        if extension.get('schema_version', 1) != 1:
            raise ValueError('Unsupported trace configuration schema_version')
        specs = extension.get('rules', [])
        if not isinstance(specs, list):
            raise ValueError('trace.rules must be an array')
        declared = {s['class'].casefold() for s in specs if isinstance(s, dict) and isinstance(s.get('class'), str)}
        seen = set()
        for spec in specs:
            if not isinstance(spec, dict) or set(spec) - {'class', 'kind', 'attributes', 'defaults', 'branches', 'branch_attribute', 'override'}:
                raise ValueError('Invalid trace rule configuration keys')
            name, kind = spec.get('class'), spec.get('kind')
            if not isinstance(name, str) or not name.strip() or kind not in STANDARD_KINDS.values():
                raise ValueError('Each trace rule needs a class and supported kind')
            # Qualified classes match exactly; bare names match the class suffix.
            key = name.casefold()
            if key in seen:
                raise ValueError(f'Duplicate trace rule: {name}')
            seen.add(key)
            if 'override' in spec and not isinstance(spec['override'], bool):
                raise ValueError('override must be boolean')
            shadows = '.' in key and key.rsplit('.', 1)[-1] in (self.rules.keys() | declared)
            if (key in self.rules or shadows) and not spec.get('override', False):
                raise ValueError(f'Overriding {name} requires override=true')
            attrs, defaults = spec.get('attributes', {}), spec.get('defaults', {})
            for label, values in [('attributes', attrs), ('defaults', defaults)]:
                if (not isinstance(values, dict) or set(values) - ATTRIBUTES or
                    any(not isinstance(v, str) for v in values.values())):
                    raise ValueError(f'{name}.{label} needs supported canonical keys and string values')
            if any(not value for value in attrs.values()) or len(set(attrs.values())) != len(attrs):
                raise ValueError(f'{name}: attribute mappings must be nonempty and unambiguous')
            branches = spec.get('branches', {'True': True, 'False': False})
            if (not isinstance(branches, dict) or not branches or
                any(not k or not isinstance(v, bool) for k, v in branches.items())):
                raise ValueError(f'{name}: branches must map labels to booleans')
            branch_attribute = spec.get('branch_attribute', 'RuleType')
            if not isinstance(branch_attribute, str) or not branch_attribute:
                raise ValueError('branch_attribute must be a nonempty string')
            self.rules[key] = {'kind': kind, 'attributes': attrs, 'defaults': defaults,
                               'branches': branches, 'branch_attribute': branch_attribute,
                               'origin': 'project_extension'}
        self.extra_metadata = {s.get('branch_attribute', 'RuleType') for s in self.rules.values()}
        self.signature = hashlib.sha256(json.dumps(self.rules, sort_keys=True).encode()).hexdigest()

    def resolve(self, rule_class: str) -> dict:
        return self.rules.get(rule_class.casefold(), self.rules.get(rule_class.rsplit('.', 1)[-1].casefold(),
                             {'kind': 'unknown', 'attributes': {}, 'defaults': {}, 'origin': 'unknown'}))

    @staticmethod
    def normalize(raw: dict, spec: dict) -> dict:
        result = dict(raw)
        for canonical, actual in spec['attributes'].items():
            # An explicit alias replaces the standard attribute, including its absence.
            result.pop(canonical, None)
            if actual in raw:
                result[canonical] = raw[actual]
        for key, value in spec['defaults'].items():
            result.setdefault(key, value)
        return result
