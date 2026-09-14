"""Conservative, machine-readable limits for a traced business conclusion.

The trace is deliberately static.  This module therefore records why an
anchor is only a candidate instead of inventing a numeric confidence score.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque


_HARD_ISSUES = frozenset({
    'UNKNOWN_RULE', 'UNKNOWN_BRANCH', 'RAW_EVIDENCE_LIMIT',
    'SEMANTIC_EVIDENCE_LIMIT', 'CONTEXT_LIMIT', 'FILE_LIMIT',
    'FILE_ERROR', 'FILE_CHANGED', 'DEPTH_LIMIT',
})


def _sample(items: list[str], limit: int = 12) -> list[str]:
    """Keep evidence bounded while preserving collection order."""
    return list(dict.fromkeys(items))[:limit]


def _status(*, blockers: dict[str, list[str]], conditional: bool, seeds: bool) -> str:
    if not seeds:
        return 'not_assessable'
    if blockers:
        return 'blocked'
    return 'conditional' if conditional else 'static_candidate'


def _blocker(code: str, values: list[str]) -> dict:
    return {'code': code, 'count': len(dict.fromkeys(values)), 'events': _sample(values)}


def evidence_closures(events: list[dict], edges: list[dict], seeds: list[str]) -> dict[str, set[str]]:
    """Return backward closures including execution controls, stopping at APIs."""
    by_id = {event['id']: event for event in events}
    incoming: dict[str, list[dict]] = defaultdict(list)
    for edge in edges:
        if edge.get('from') in by_id and edge.get('to') in by_id:
            incoming[edge['to']].append(edge)
    result = {}
    for seed in seeds:
        if seed not in by_id:
            continue
        queue = deque([seed])
        closure: set[str] = set()
        while queue:
            current = queue.popleft()
            if current in closure:
                continue
            closure.add(current)
            event = by_id[current]
            if event.get('api'):
                continue
            queue.extend(edge['from'] for edge in incoming[current])
            queue.extend(event.get('parents', ()))
            queue.extend(guard.get('event') for guard in event.get('guards', ()))
            queue.extend(event.get('loops', ()))
        result[seed] = closure
    return result


def _value_closure(seed: str, by_id: dict[str, dict], incoming: dict[str, list[dict]]) -> set[str]:
    """Return data definitions only, excluding parents, guards and loops."""
    queue = deque([seed])
    closure: set[str] = set()
    while queue:
        current = queue.popleft()
        if current in closure:
            continue
        closure.add(current)
        if by_id[current].get('api'):
            continue
        queue.extend(edge['from'] for edge in incoming[current])
    return closure


def assess_conclusions(events: list[dict], edges: list[dict], inputs: list[dict],
                       issues: list[dict], seeds: list[str], coverage: dict,
                       opaque_dependencies: list[dict], scenario: dict | None = None) -> dict:
    """Assess every anchor from its actual backward evidence closure.

    API request construction is intentionally a boundary: fields used to make
    a request are not presented as direct value sources for its response.
    Opaque predecessors are separate from data edges because their unknown
    behavior may affect a value but does not prove a definition.
    """
    by_id = {event['id']: event for event in events}
    incoming: dict[str, list[dict]] = defaultdict(list)
    for edge in edges:
        if edge.get('from') in by_id and edge.get('to') in by_id:
            incoming[edge['to']].append(edge)
    opaque_to: dict[str, list[str]] = defaultdict(list)
    for dependency in opaque_dependencies:
        source, target = dependency.get('from'), dependency.get('to')
        if source in by_id and target in by_id:
            opaque_to[target].append(source)
    input_by_event: dict[str, list[dict]] = defaultdict(list)
    for item in inputs:
        if item.get('event') in by_id:
            input_by_event[item['event']].append(item)
    issue_by_event: dict[str, list[dict]] = defaultdict(list)
    for item in issues:
        if item.get('event') in by_id:
            issue_by_event[item['event']].append(item)
    conditions = {item.get('event'): item for item in (scenario or {}).get('conditions', [])}

    control_closures = evidence_closures(events, edges, seeds)
    anchors = []
    for seed, closure in control_closures.items():
        value_closure = _value_closure(seed, by_id, incoming)
        opaque: list[str] = []
        conditional = False
        ordered_closure = [event['id'] for event in events if event['id'] in closure]
        for current in ordered_closure:
            if not by_id[current].get('api'):
                for edge in incoming[current]:
                    if edge.get('conditional') or edge.get('event_order') == 'unknown_between_ui_events':
                        conditional = True
            for source in opaque_to[current]:
                if source not in opaque:
                    opaque.append(source)
        for current in (event['id'] for event in events if event['id'] in value_closure):
            if not by_id[current].get('api'):
                for edge in incoming[current]:
                    if edge.get('conditional') or edge.get('event_order') == 'unknown_between_ui_events':
                        conditional = True

        blockers: dict[str, list[str]] = defaultdict(list)
        if coverage.get('limited') or coverage.get('partial_scope_only'):
            blockers['COLLECTION_INCOMPLETE'].append(seed)
        for event_id in ordered_closure:
            event = by_id[event_id]
            if event.get('kind') == 'unknown':
                blockers['UNKNOWN_RULE_SEMANTICS'].append(event_id)
            if event.get('semantics', {}).get('incomplete'):
                blockers['INCOMPLETE_RULE_EVIDENCE'].append(event_id)
            for item in input_by_event[event_id]:
                blockers['UNRESOLVED_VALUE'].append(f"{event_id}:{item['field']['path']}")
            for item in issue_by_event[event_id]:
                if item.get('code') in _HARD_ISSUES and item['code'] != 'UNKNOWN_RULE':
                    blockers[item['code']].append(event_id)
            condition = conditions.get(event_id)
            if condition and condition.get('result') == 'unknown':
                conditional = True
        if opaque:
            blockers['OPAQUE_RULE_MAY_AFFECT'].extend(opaque)
        api_events = [event['id'] for event in events if event['id'] in value_closure and event.get('api')]
        anchors.append({
            'seed': seed,
            'status': _status(blockers=blockers, conditional=conditional, seeds=True),
            'runtime_verified': False,
            'direct_source_api_events': api_events,
            'dependency_event_count': len(closure),
            'conditional_path': conditional,
            'blockers': [_blocker(code, values) for code, values in sorted(blockers.items())],
        })

    status_counts = Counter(anchor['status'] for anchor in anchors)
    overall_status = ('not_assessable' if not anchors else
                      'blocked' if status_counts['blocked'] else
                      'conditional' if status_counts['conditional'] else
                      'static_candidate')
    return {
        'schema_version': 1,
        'runtime_verified': False,
        'meaning': ('blocked means a collected boundary can change the conclusion; '
                    'conditional means static evidence has execution prerequisites; '
                    'static_candidate is still not runtime verification.'),
        'overall_status': overall_status,
        'status_counts': dict(sorted(status_counts.items())),
        'anchors': anchors,
    }
