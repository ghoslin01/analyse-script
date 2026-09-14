"""Conservative scenario overlay. No eval, runtime calls, or XML-order UI assumptions."""
from __future__ import annotations

import re


UNKNOWN = object()
FIELD_PATH = re.compile(r'!?[\w-]+(?:\[(?:[0-9]+|A|C)\])?(?:\.[\w-]+(?:\[(?:[0-9]+|A|C)\])?)*')
TOKEN = re.compile(r'''\s*(?:(\$\$[^$\r\n]+\$)|('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")|(==|!=|\(|\))|\b(AND|OR|NOT)\b)''', re.I)


def parse_condition(expression: str):
    """Only equality, inequality, AND/OR/NOT, parentheses and string operands.

    Unsupported syntax invalidates the whole parse, never just a convenient prefix.
    Quoted whole-field substitutions are recognized; escapes are deliberately unknown.
    """
    if len(expression) > 16384:
        raise ValueError('Expression limit exceeded')
    tokens, pos = [], 0
    while expression[pos:].strip():
        m = TOKEN.match(expression, pos)
        if not m or len(tokens) >= 1024:
            raise ValueError('Unsupported expression syntax')
        ref, string, op, boolean = m.groups()
        if ref:
            token = ('field', ref[2:-1])
        elif string:
            value = string[1:-1]
            if '\\' in value:
                raise ValueError('String escape semantics unknown')
            if value.startswith('$$') and value.endswith('$') and value.count('$') == 3:
                token = ('field', value[2:-1])
            elif '$' in value:
                raise ValueError('Interpolation semantics unknown')
            else:
                token = ('literal', value)
        else:
            token = (op or boolean.upper(),)
        if token[0] == 'field' and not FIELD_PATH.fullmatch(token[1]):
            raise ValueError('Unsupported field or function syntax')
        tokens.append(token)
        pos = m.end()
    cursor = 0

    def take(kind):
        nonlocal cursor
        if cursor < len(tokens) and tokens[cursor][0] == kind:
            cursor += 1
            return True
        return False

    def operand():
        nonlocal cursor
        if cursor >= len(tokens) or tokens[cursor][0] not in {'field', 'literal'}:
            raise ValueError('Expected string or field')
        result = tokens[cursor]
        cursor += 1
        return result

    def atom(depth):
        if depth > 64:
            raise ValueError('Expression depth limit exceeded')
        if take('NOT'):
            return ('not', atom(depth + 1))
        if take('('):
            result = disjunction(depth + 1)
            if not take(')'):
                raise ValueError('Missing closing parenthesis')
            return result
        left = operand()
        op = 'eq' if take('==') else 'ne' if take('!=') else None
        if op is None:
            raise ValueError('Expected equality comparison')
        return (op, left, operand())

    def conjunction(depth):
        result = atom(depth)
        while take('AND'):
            result = ('and', result, atom(depth))
        return result

    def disjunction(depth):
        result = conjunction(depth)
        while take('OR'):
            result = ('or', result, conjunction(depth))
        return result

    result = disjunction(0)
    if cursor != len(tokens):
        raise ValueError('Trailing expression syntax')
    return result


def evaluate(tree, lookup):
    op = tree[0]
    if op == 'literal':
        return tree[1]
    if op == 'field':
        return lookup(tree[1])
    left = evaluate(tree[1], lookup)
    if op == 'not':
        return UNKNOWN if left is UNKNOWN else not left
    right = evaluate(tree[2], lookup)
    if op == 'and':
        return False if left is False or right is False else UNKNOWN if left is UNKNOWN or right is UNKNOWN else True
    if op == 'or':
        return True if left is True or right is True else UNKNOWN if left is UNKNOWN or right is UNKNOWN else False
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    return (left == right) if op == 'eq' else (left != right)


def validate_scenario(scenario):
    if not isinstance(scenario, dict) or set(scenario) - {'name', 'trigger_eid', 'assumptions'}:
        raise ValueError('Scenario supports name, trigger_eid and assumptions')
    for key in ('name', 'trigger_eid'):
        if key in scenario and (not isinstance(scenario[key], str) or not scenario[key]):
            raise ValueError(f'scenario.{key} must be a nonempty string')
    assumptions = scenario.get('assumptions', [])
    if not isinstance(assumptions, list):
        raise ValueError('scenario.assumptions must be an array')
    seen = set()
    for a in assumptions:
        if (not isinstance(a, dict) or set(a) - {'field', 'operator', 'value'} or
            not isinstance(a.get('field'), str) or not a['field'] or
            not isinstance(a.get('value'), str) or a.get('operator', 'eq') != 'eq' or
            not FIELD_PATH.fullmatch(a['field']) or
            any(x in a['field'] for x in ('[A]', '[C]', '$'))):
            raise ValueError('Assumptions require concrete field paths, operator eq, and string values')
        if a['field'] in seen:
            raise ValueError(f'Duplicate scenario assumption: {a["field"]}')
        seen.add(a['field'])
    return scenario


class Scenario:
    def __init__(self, trace, scenario, source_file):
        from .trace import overlaps, compatible, project_read
        self.overlaps, self.compatible, self.project_read = overlaps, compatible, project_read
        self.trace, self.spec = trace, validate_scenario(scenario)
        self.events = {e['id']: e for e in trace.events}
        self.memo, self.busy = {}, set()
        self.proofs = {}
        self.condition_results = {}
        self.trigger = None
        trigger_eid = scenario.get('trigger_eid')
        if trigger_eid:
            matches = [e for e in trace.events if e['kind'] in {'ui_input', 'ui_event'} and
                       trace.nodes[e['node']]['eid'] == trigger_eid and
                       trace.nodes[e['node']]['file'] == source_file]
            if len(matches) != 1:
                raise ValueError('scenario.trigger_eid must identify one reached Question or Button in the starting file')
            self.trigger = matches[0]['id']
        elif any(e['kind'] in {'ui_input', 'ui_event'} for e in trace.events):
            raise ValueError('A UI scenario requires an explicit Question/Button trigger_eid')
        entries = {e['entry'] for e in trace.events}
        if len(entries) != 1:
            raise ValueError('A scenario requires one explicit entry')
        self.entry = next(iter(entries))

    def in_scenario(self, event):
        return self.trigger in event['triggers'] if self.trigger else not event['triggers']

    def field(self, event, path):
        return {'path': path[1:] if path.startswith('!') else path,
                'scope': self.entry + ':session' if path.startswith('!') else event['scope'], 'group': False}

    def cached(self, key, action):
        if key in self.memo:
            return self.memo[key]
        if key in self.busy or len(self.busy) >= 80:
            return UNKNOWN
        self.busy.add(key)
        try:
            result = action()
            self.memo[key] = result
            return result
        finally:
            self.busy.remove(key)

    def condition(self, event):
        def compute():
            expression = event.get('semantics', {}).get('attributes', {}).get('Expression', '')
            facts = []
            try:
                if event.get('semantics', {}).get('incomplete'):
                    raise ValueError('Incomplete source attributes')
                tree = parse_condition(expression)
                def lookup(path):
                    field = self.field(event, path)
                    value = self.value(event, field)
                    facts.append({'field': path, 'value': None if value is UNKNOWN else value,
                                  'status': 'unknown' if value is UNKNOWN else 'derived_or_assumed',
                                  'basis': self.proofs.get(('value', event['id'], field['scope'], field['path']),
                                                           {'reason': 'analysis_recursion_limit'})})
                    return value
                value = evaluate(tree, lookup)
                reason = 'partial_information' if value is UNKNOWN else 'supported_expression'
            except (ValueError, RecursionError) as error:
                value, tree, reason = UNKNOWN, None, str(error)
            self.condition_results[event['id']] = {
                'event': event['id'], 'expression': expression, 'tree': tree,
                'result': 'unknown' if value is UNKNOWN else value, 'reason': reason, 'facts': facts}
            return value
        return self.cached(('condition', event['id']), compute)

    def guard_value(self, guard):
        if guard['kind'] != 'condition':
            return UNKNOWN
        result = self.condition(self.events[guard['event']])
        return UNKNOWN if result is UNKNOWN else result == (guard['branch'] == 'True')

    def excluded(self, event):
        return any(self.guard_value(g) is False for g in event['guards'])

    def value(self, reader, field):
        key = ('value', reader['id'], field['scope'], field['path'])
        def initial_assumption():
            if self.in_scenario(reader):
                for a in self.spec.get('assumptions', []):
                    expected = self.field({'scope': self.entry + ':root'}, a['field'])
                    if field['scope'] == expected['scope'] and field['path'] == expected['path']:
                        return a
            return None

        def compute():
            basis = {'reason': 'no_definition_in_this_activation'}
            self.proofs[key] = basis
            if any(x in field['path'] for x in ('[A]', '[C]', '$', '@instance:')) or reader['loops']:
                basis['reason'] = 'symbolic_instance_or_loop'
                return UNKNOWN
            for writer in reversed(self.trace.events[:reader['sequence']]):
                if writer['triggers'] != reader['triggers'] or not self.compatible(writer, reader):
                    continue
                if self.excluded(writer):
                    continue
                same_scope = writer['scope'] == field['scope'] or field['scope'].endswith(':session')
                if writer['kind'] in {'unknown', 'goto'} and same_scope:
                    basis.update(reason='unknown_effect_or_phase_boundary', source_event=writer['id'])
                    return UNKNOWN
                if (writer['kind'] == 'call' and field['scope'].endswith(':session') and
                    writer['id'] not in reader['parents']):
                    basis.update(reason='session_effect_of_component_unknown', source_event=writer['id'])
                    return UNKNOWN
                writes = [w for w in writer['writes'] if self.overlaps(w, field)]
                if not writes:
                    continue
                basis['source_event'] = writer['id']
                basis['source'] = {k: self.trace.nodes[writer['node']][k] for k in ('file', 'line', 'eid')}
                if (writer['loops'] or writer.get('semantics', {}).get('incomplete') or
                    any(any(x in w['path'] for x in ('[A]', '[C]', '$')) for w in writes) or
                    any(g not in reader['guards'] and self.guard_value(g) is not True for g in writer['guards'])):
                    basis['reason'] = 'conditional_or_symbolic_write'
                    return UNKNOWN
                attrs = writer.get('semantics', {}).get('attributes', {})
                if writer['kind'] == 'ui_input' and writer['id'] == self.trigger:
                    assumption = initial_assumption()
                    if assumption is not None:
                        basis.update(reason='initial_scenario_assumption', assumption=assumption)
                        return assumption['value']
                if writer['kind'] == 'set' and attrs.get('Trim', 'N').casefold() not in {'n', 'false'}:
                    basis['reason'] = 'assignment_transformation_unknown'
                    return UNKNOWN
                if writer['kind'] == 'set' and writer.get('assignment_modes', {}).get('source') == 'Value':
                    literal = attrs.get('FromValue')
                    if any(w.get('group') for w in writes):
                        basis['reason'] = 'group_literal_semantics_unknown'
                        return UNKNOWN
                    basis['reason'] = 'literal_assignment' if literal is not None and '$' not in literal else 'value_expression_unknown'
                    return literal if literal is not None and '$' not in literal else UNKNOWN
                if writer['kind'] in {'set', 'input_mapping', 'output_mapping'} and len(writer['reads']) == 1:
                    read = self.project_read(writer['reads'][0], writes[0], field)
                    value = self.value(writer, read)
                    basis.update(reason='copied_value', input=read,
                                 input_basis=self.proofs.get(('value', writer['id'], read['scope'], read['path']),
                                                             {'reason': 'analysis_recursion_limit'}))
                    return value
                basis['reason'] = 'write_value_unknown'
                return UNKNOWN
            assumption = initial_assumption()
            if assumption is not None:
                basis.update(reason='initial_scenario_assumption', assumption=assumption)
                return assumption['value']
            return UNKNOWN
        return self.cached(key, compute)

    def apply(self):
        classifications = []
        for e in self.trace.events:
            if e['kind'] == 'evaluate':
                self.condition(e)
            rejected = [g['event'] for g in e['guards'] if self.guard_value(g) is False]
            status = 'excluded' if rejected else 'candidate'
            if not self.in_scenario(e):
                status = 'other_event_context'
            e['scenario'] = {'status': status, 'excluded_by': rejected if status == 'excluded' else []}
            classifications.append({'event': e['id'], **e['scenario']})
        return {'specification': self.spec, 'assumption_lifetime': 'initial values of the selected activation; writes invalidate them',
                'runtime_verified': False, 'conditions': list(self.condition_results.values()),
                'classifications': classifications}
