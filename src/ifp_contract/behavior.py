"""Small shared readers for trace and compatibility inspection behavior."""
from __future__ import annotations

from collections.abc import Callable


TRUTHY_VALUES = frozenset({'1', 'true', 'yes', 'y', 'on'})


def parse_bool(value: str | None) -> bool:
    """Parse the boolean spellings used by IFP source attributes."""
    return (value or '').strip().casefold() in TRUTHY_VALUES


def configured_value(raw: dict[str, str], names: tuple[str, ...]) -> str | None:
    """Return the first non-empty value, comparing XML attribute names loosely."""
    values = {key.casefold(): value for key, value in raw.items()}
    for name in names:
        value = values.get(name.casefold())
        if value is not None and value != '':
            return value
    return None


def selector_value(raw: dict[str, str], spec: dict, configured_names: tuple[str, ...],
                   normalize: Callable | None = None) -> str | None:
    """Resolve a component selector with explicit trace mappings taking precedence.

    An explicit ``SelectComponent`` mapping is authoritative even when its source
    attribute is absent; falling back to a legacy alias in that case produces a
    false selector and makes trace and inspect disagree.
    """
    normalized = normalize(raw, spec) if normalize else dict(raw)
    attributes = spec.get('attributes', {})
    if 'SelectComponent' in attributes or 'SelectComponent' in spec.get('defaults', {}):
        return normalized.get('SelectComponent')
    return configured_value(raw, configured_names)


def effective_disabled(node, read_attributes: Callable, resolve: Callable,
                       normalize: Callable, cache: dict[str, bool] | None = None) -> bool:
    """Apply a mapped RuleDisabled value from a Rule and all disabled parents."""
    current = node
    chain = []
    while current is not None:
        if current.tag == 'Rule':
            if cache is not None and current.key in cache:
                result = cache[current.key]
                for item in chain:
                    cache[item.key] = result
                return result
            raw = read_attributes(current)
            rule_class = raw.get('RuleClassName', raw.get('ClassType', ''))
            spec = resolve(rule_class)
            attrs = normalize(raw, spec)
            chain.append(current)
            if parse_bool(attrs.get('RuleDisabled')):
                for item in chain:
                    if cache is not None:
                        cache[item.key] = True
                return True
        else:
            attrs = current.meta
            if parse_bool(attrs.get('RuleDisabled')):
                for item in chain:
                    if cache is not None:
                        cache[item.key] = True
                return True
        current = current.parent
    for item in chain:
        if cache is not None:
            cache[item.key] = False
    return False
