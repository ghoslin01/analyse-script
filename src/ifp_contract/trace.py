"""Context-sensitive evidence collection, with conservative backward data slicing.

This is not a UXP interpreter: branches and loop iterations remain symbolic.
"""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import re

from .config import load_rule_config
from .extractor import mappings_from_attributes, _value, _value_ending_with, rule_base_url
from .trace_source import TraceNode, TraceSource
from .trace_config import TraceConfig, STANDARD_KINDS, ATTRIBUTES
from .templates import field_references, template_evidence


KINDS = STANDARD_KINDS
KINDS = {k.casefold(): v for k, v in KINDS.items()}
INDEX = re.compile(r'\[[^\]]*\]')


def refs(expression: str) -> list[str]:
    """Extract references without evaluating expressions or losing the original text."""
    return field_references(expression)


def shape(path: str) -> str:
    return INDEX.sub('', path).removeprefix('Data Store Root.').rstrip('.')


def overlaps(left: dict, right: dict) -> bool:
    if left['scope'] != right['scope']:
        return False
    a, b = shape(left['path']), shape(right['path'])
    same = a == b
    group = ((left.get('group') and b.startswith(a + '.')) or
             (right.get('group') and a.startswith(b + '.')))
    if not (same or group):
        return False
    for x, y in zip(left['path'].split('.'), right['path'].split('.')):
        xi, yi = INDEX.findall(x), INDEX.findall(y)
        if xi and yi and xi[0][1:-1].isdigit() and yi[0][1:-1].isdigit() and xi != yi:
            return False
    return True


def compatible(writer: dict, reader: dict) -> bool:
    constraints = {g['event']: g['branch'] for g in reader['guards']}
    return not any(g['event'] in constraints and constraints[g['event']] != g['branch']
                   for g in writer['guards'])


def project_read(read: dict, written: dict, demand: dict) -> dict:
    """Carry a concrete instance through a mapping rather than merging every [A].

    Remaining symbolic indices retain their original text; this is not an attempt
    to evaluate [C] or dynamically computed indices.
    """
    path = read['path']
    wi, di = INDEX.findall(written['path']), INDEX.findall(demand['path'])
    ri = list(INDEX.finditer(path))
    replacements = []
    for index, (w, d) in enumerate(zip(reversed(wi), reversed(di))):
        if index < len(ri) and w in {'[A]', '[C]'} and d[1:-1].isdigit():
            match = ri[-index - 1]
            if match.group() in {'[A]', '[C]'}:
                replacements.append((match.start(), match.end(), d))
    for start, end, value in sorted(replacements, reverse=True):
        path = path[:start] + value + path[end:]
    if written.get('group') and shape(demand['path']).startswith(shape(written['path']) + '.'):
        suffix = demand['path'].split('.')[len(written['path'].split('.')):]
        path += '.' + '.'.join(suffix)
    return {**read, 'path': path, 'group': demand.get('group', False) if written.get('group') else read.get('group', False)}


class Trace:
    def __init__(self, root: Path, *, rules_config: Path | None = None,
                 path_variables: dict[str, str] | None = None,
                 max_files: int = 50, max_contexts: int = 5000):
        self.root = root.resolve()
        self.config = load_rule_config(rules_config)
        self.semantics = TraceConfig(rules_config)
        for name in self.semantics.rules:
            if self.config.is_api_rule(name):
                raise ValueError(f'Conflicting API and trace semantics for {name}')
        self.scenario = None
        self.path_variables = path_variables or {}
        self.max_files, self.max_contexts = max_files, max_contexts
        if not all(isinstance(x, int) and x > 0 for x in (max_files, max_contexts)):
            raise ValueError('Trace limits must be positive')
        self.sources: dict[str, TraceSource] = {}
        self.events: list[dict] = []
        self.nodes: dict[str, dict] = {}
        self.issues: list[dict] = []
        self.failed = False
        self.limited = False
        self.current_entry = ''

    def issue(self, code: str, **details):
        item = {'code': code, **details}
        if item not in self.issues:
            self.issues.append(item)

    def source(self, path: Path) -> TraceSource | None:
        path = path.resolve()
        if not path.is_relative_to(self.root):
            self.issue('OUTSIDE_ROOT', file=str(path))
            return None
        key = path.relative_to(self.root).as_posix()
        if key in self.sources:
            return self.sources[key]
        if len(self.sources) >= self.max_files:
            self.limited = True
            self.issue('FILE_LIMIT', file=key)
            return None
        try:
            result = TraceSource(self.root, path, extra_metadata=self.semantics.extra_metadata)
            self.sources[key] = result
            return result
        except (OSError, ValueError) as error:
            self.failed = True
            self.issue('FILE_ERROR', file=key, message=str(error))
            return None

    def resolve(self, selector: str, source: TraceSource) -> TraceSource | None:
        value = selector.replace('\\', '/')
        for name, path in self.path_variables.items():
            value = value.replace('$$' + name + '$', path.replace('\\', '/'))
        if '$' in value:
            self.issue('DYNAMIC_COMPONENT', file=source.relative, selector=selector)
            return None
        candidates = {p.resolve() for p in (self.root / value, source.path.parent / value) if p.is_file()}
        if len(candidates) != 1:
            self.issue('COMPONENT_NOT_UNIQUE', file=source.relative, selector=selector,
                       candidates=[str(p) for p in sorted(candidates)])
            return None
        return self.source(candidates.pop())

    def field(self, scope: str, path: str, group: bool = False) -> dict:
        if path.startswith('!'):
            return {'scope': self.current_entry + ':session', 'path': path[1:], 'group': group}
        return {'scope': scope, 'path': path, 'group': group}

    def event(self, source: TraceSource, node: TraceNode, scope: str, kind: str,
              guards: list, loops: list, parents: list[str], attrs: dict | None = None,
              reads: list | None = None, writes: list | None = None, **details) -> dict | None:
        if len(self.events) >= self.max_contexts:
            self.limited = True
            self.issue('CONTEXT_LIMIT', file=source.relative, node=node.key)
            return None
        if node.key not in self.nodes:
            # Full generated Component metadata is huge. Retain behavioral attributes;
            # the exact mapping used by a data edge is attached to its mapping event.
            raw = attrs or node.meta
            evidence = {k: v for k, v in raw.items() if '_' not in k and
                        not k.startswith(('Display', 'styleSheet')) and
                        not k.endswith(('LinkedEntity', 'ProjectLocation', 'ComponentComment'))}
            packed = json.dumps(evidence, ensure_ascii=False)
            if len(packed.encode('utf-8')) > 128 * 1024:
                evidence = dict(node.meta)
                self.issue('RAW_EVIDENCE_LIMIT', node=node.key)
            self.nodes[node.key] = {'file': source.relative, 'offset': node.offset, 'line': node.line,
                                    'eid': node.meta.get('eid'), 'tag': node.tag, 'attributes': evidence}
        item = {'id': f'e{len(self.events)}', 'sequence': len(self.events), 'node': node.key,
                'entry': self.current_entry, 'scope': scope, 'kind': kind,
                'name': node.meta.get('Name', node.tag), 'guards': list(guards), 'loops': list(loops),
                'parents': list(parents), 'reads': reads or [], 'writes': writes or [], **details}
        item['triggers'] = [p for p in parents if self.events[int(p[1:])]['kind'] in {'ui_event', 'ui_input'}]
        if kind in {'ui_event', 'ui_input'}:
            item['triggers'].append(item['id'])
        self.events.append(item)
        return item

    def walk(self, source: TraceSource, node: TraceNode, scope: str, guards: list,
             loops: list, parents: list, active: tuple = ()):
        if len(self.events) >= self.max_contexts:
            self.limited = True
            self.issue('CONTEXT_LIMIT', file=source.relative, node=node.key)
            return
        if node.key in active:
            self.issue('RECURSIVE_REFERENCE', node=node.key, context=parents)
            return
        if len(active) >= 100:
            self.issue('DEPTH_LIMIT', node=node.key)
            return
        active = (*active, node.key)
        raw = source.attributes(node) if node.tag == 'Rule' else node.meta
        full_class = raw.get('RuleClassName', raw.get('ClassType', ''))
        cls = full_class.rsplit('.', 1)[-1]
        spec = self.semantics.resolve(full_class)
        a = self.semantics.normalize(raw, spec) if node.tag == 'Rule' else raw
        kind = spec['kind'] if node.tag == 'Rule' else 'structure'
        if node.tag in {'Question', 'Button'}:
            kind = 'ui_event'
            if (node.tag == 'Question' and a.get('PropertyKey') and
                not any(a.get(k, '').casefold() in {'y', 'true'} for k in ('ReadOnly', 'DisableInput'))):
                kind = 'ui_input'
        if a.get('RuleDisabled', '').casefold() in {'y', 'true'}:
            self.event(source, node, scope, 'disabled', guards, loops, parents, a)
            return
        ev = self.event(source, node, scope, kind, guards, loops, parents, raw)
        if ev is None:
            return
        if node.tag == 'Rule':
            behavior = {k: v for k, v in a.items() if k in ATTRIBUTES}
            incomplete = any(i.get('node') == node.key for i in source.issues)
            if len(json.dumps(behavior).encode()) > 128 * 1024:
                behavior, incomplete = {}, True
                self.issue('SEMANTIC_EVIDENCE_LIMIT', event=ev['id'])
            ev['semantics'] = {'rule_class': full_class, 'kind': kind, 'origin': spec['origin'],
                               'attributes': behavior, 'incomplete': incomplete,
                               'attribute_mapping': spec['attributes']}
        descendants = [*parents, ev['id']]
        if a.get('LinkReference'):
            ev['kind'] = 'link'
            targets = source.eids.get(a['LinkReference'], [])
            if len(targets) != 1:
                self.issue('LINK_NOT_UNIQUE', event=ev['id'], target=a['LinkReference'])
            else:
                self.walk(source, targets[0], scope, guards, loops, descendants, active)
            return

        def add_reads(expression):
            ev['reads'].extend(self.field(scope, p, p.endswith(']')) for p in refs(expression))

        if a.get('ConditionExpression'):
            ev['ui_condition'] = {'expression': a['ConditionExpression'],
                                  'not_applicable': a.get('NotApplicable'),
                                  'status': 'configuration_only_not_a_proven_execution_gate'}
            add_reads(a['ConditionExpression'])
        if kind == 'ui_input':
            ev['writes'].append(self.field(scope, a['PropertyKey']))
            ev['value_origin'] = 'user_or_existing_ui_value'
        if kind in {'evaluate', 'expression'}:
            add_reads(a.get('Expression', ''))
            if kind == 'expression' and a.get('OutputProperty'):
                ev['writes'].append(self.field(scope, a['OutputProperty']))
        elif kind == 'set':
            typ, from_type = a.get('Type', 'Data Item'), a.get('FromType', 'Value')
            ev['assignment_modes'] = {'target': typ, 'source': from_type,
                                      'source_defaulted': 'FromType' not in a,
                                      'target_defaulted': 'Type' not in a}
            target = a.get({'Variable': 'VariableName', 'Data Group': 'PropertyGroupName',
                            'Data Group Instance': 'PropertyGroupInstanceName'}.get(typ, 'PropertyName'), '')
            if target:
                prefix = '!' if typ == 'Variable' else '@instance:' if typ == 'Data Group Instance' else ''
                ev['writes'].append(self.field(scope, prefix + target,
                                               typ == 'Data Group'))
            source_key = {'Data Item': 'FromPropertyName', 'Variable': 'FromVariableName',
                          'Data Group': 'FromPropertyGroupName',
                          'Data Group Instance': 'FromPropertyGroupInstanceName'}.get(from_type)
            if source_key and a.get(source_key):
                prefix = '!' if from_type == 'Variable' else '@instance:' if from_type == 'Data Group Instance' else ''
                ev['reads'].append(self.field(scope, prefix + a[source_key],
                                              from_type == 'Data Group'))
            elif source_key:
                self.issue('MISSING_ASSIGNMENT_SOURCE', event=ev['id'], attribute=source_key)
            else:
                add_reads(a.get('FromValue', ''))
            if typ == 'Data Group Instance':
                ev['kind'] = 'instance'
        elif kind == 'reset':
            for key, group in [('ResetProperty', False), ('ResetPropertyGroup', True), ('ResetVariable', False)]:
                for target in a.get(key, '').split(','):
                    if target.strip():
                        ev['writes'].append(self.field(scope, ('!' if key == 'ResetVariable' else '') + target.strip(), group))
        elif kind == 'increment':
            if a.get('Type') == 'Data Group Instance':
                ev['kind'] = 'instance'
                if a.get('PropertyGroupName'):
                    cursor = self.field(scope, '@instance:' + a['PropertyGroupName'])
                    ev['reads'].append(cursor)
                    ev['writes'].append(cursor)
            elif a.get('PropertyName'):
                ev['reads'].append(self.field(scope, a['PropertyName']))
                ev['writes'].append(self.field(scope, a['PropertyName']))
            add_reads(a.get('IncrementBy', ''))
        elif kind == 'repeat':
            add_reads(a.get('EndInstance', ''))
            if a.get('DataGroupName'):
                ev['reads'].append(self.field(scope, a['DataGroupName'], True))
                ev['writes'].append(self.field(scope, '@instance:' + a['DataGroupName']))
        elif kind == 'goto':
            self.issue('PHASE_TRANSITION_BOUNDARY', event=ev['id'], phase=a.get('Phase'),
                       operation=a.get('OperationType'))
        elif self.config.is_api_rule(cls):
            ev['kind'] = 'api'
            names = lambda concept: self.config.attribute_names_for_rule(full_class, concept)
            method = (_value(a, *names('method')) or _value_ending_with(a, 'method')
                      or self.config.method_for_rule_class(cls))
            ev['api'] = {'method': method, 'path': _value(a, *names('path')),
                         'source': _value(a, *names('source')),
                         'base_url': rule_base_url(a, self.config),
                         'query': _value(a, *names('filter')),
                         'payload': _value(a, *names('payload')),
                         'header_name': _value(a, *names('header_name')),
                         'header_value': _value(a, *names('header_value')),
                         'language': _value(a, *names('language')),
                         'context': _value(a, *names('context')),
                         'manual_payload': a.get('UseManualPayload')}
            if ev['api']['base_url']:
                ev['api']['base_url_evidence'] = {'origin': 'rule', 'file': source.relative,
                                                 'line': node.line, 'offset': node.offset}
            for data_source in source.nodes:
                if data_source.tag == 'DataSource' and data_source.meta.get('Name') == ev['api']['source']:
                    da = source.attributes(data_source)
                    endpoint = _value(da, *self.config.attribute_names('base_url'))
                    if not ev['api']['base_url']:
                        ev['api']['base_url'] = endpoint
                        if endpoint:
                            ev['api']['base_url_evidence'] = {'origin': 'data_source', 'file': source.relative,
                                                             'line': data_source.line, 'offset': data_source.offset}
                    ev['api']['source_evidence'] = {'file': source.relative, 'offset': data_source.offset,
                                                  'line': data_source.line, 'name': data_source.meta.get('Name')}
            ev['api']['templates'] = {}
            for concept in ('path', 'query', 'payload', 'base_url', 'header_name', 'header_value', 'language'):
                value = ev['api'].get(concept)
                if value is None:
                    continue
                template = template_evidence(value, variants=concept == 'path')
                ev['api']['templates'][concept] = template
                for dep in template['dependencies']:
                    read = self.field(scope, dep['field'], dep['field'].endswith(']'))
                    if read not in ev['reads']:
                        ev['reads'].append(read)
                if template['status'] != 'parsed':
                    self.issue('API_TEMPLATE_UNRESOLVED', event=ev['id'], attribute=concept,
                               reason=template.get('reason'))
            for concept in ('request', 'result', 'output', 'target'):
                # Query and payload are both inputs, not alternatives to one another.
                values = {_value(a, alias) for alias in names(concept)} - {None, ''}
                for value in sorted(values):
                    collection = ev['reads'] if concept == 'request' else ev['writes']
                    collection.append(self.field(scope, value, True))
            for concept in ('error_code', 'error_message'):
                for value in {_value(a, alias) for alias in names(concept)} - {None, ''}:
                    ev['writes'].append(self.field(scope, value))
            for group in a.get('AdditionalHTTPDataGroups', '').split(','):
                if group:
                    ev['writes'].append(self.field(scope, group, True))
        elif kind == 'unknown' and node.tag == 'Rule':
            self.issue('UNKNOWN_RULE', event=ev['id'], rule_class=cls)
            for value in a.values():
                add_reads(value)
            # Preserve all unknown-rule attributes, including unfamiliar underscored ones.
            if len(json.dumps(a).encode()) <= 128 * 1024:
                self.nodes[node.key]['attributes'] = a.copy()
            else:
                self.issue('RAW_EVIDENCE_LIMIT', event=ev['id'])

        if kind == 'call':
            self.expand_call(source, node, a, ev, guards, loops, descendants, active)
        child_loops = [*loops, ev['id']] if kind == 'repeat' else loops
        for child in node.children:
            child_guards = guards
            branch_label = child.meta.get(spec.get('branch_attribute', 'RuleType'), '')
            branch_value = spec.get('branches', {'True': True, 'False': False}).get(branch_label)
            branch = 'True' if branch_value else 'False'
            if kind == 'evaluate' and spec['origin'] == 'project_extension' and branch_value is None:
                self.issue('UNKNOWN_BRANCH', event=ev['id'], label=branch_label, child=child.key)
                child_guards = [*guards, {'event': ev['id'], 'branch': branch_label,
                                         'kind': 'rule_result', 'expression': None}]
            if branch_value is not None and node.tag in {'Rule', 'Question', 'Button'}:
                child_guards = [*guards, {'event': ev['id'], 'branch': branch,
                                'source_branch': branch_label,
                                'kind': 'condition' if kind == 'evaluate' else 'rule_result',
                                'expression': a.get('Expression') if kind == 'evaluate' else None}]
            self.walk(source, child, scope, child_guards, child_loops, descendants, active)
        # Attributes for reached rules can contain thousands of generated mapping entries.
        node.attrs = None

    def expand_call(self, source, node, a, ev, guards, loops, parents, active):
        selector = a.get('SelectComponent', '')
        target = self.resolve(selector, source) if selector else None
        if not selector:
            self.issue('MISSING_COMPONENT_SELECTOR', event=ev['id'])
        names = [p.strip() for p in re.split('[,;]', a.get('ComponentList', a.get('Source', ''))) if p.strip()]
        products = [n for n in target.nodes if n.tag == 'Product' and n.meta.get('Name') in names] if target else []
        if target and (len(products) != 1 or len(names) != 1):
            self.issue('COMPONENT_ENTRY_NOT_UNIQUE', event=ev['id'], names=names)
        callee_scope = ev['id'] + ':call'
        mappings = mappings_from_attributes(a)
        for incoming in (True, False):
            if not incoming and target and len(products) == 1 and len(names) == 1:
                product = products[0]
                initial = product.meta.get('InitialPhase')
                phases = target.entry_roots(initial) if initial else [n for n in product.children if n.tag == 'Phase']
                if len(phases) != 1:
                    self.issue('INITIAL_PHASE_NOT_UNIQUE', event=ev['id'], initial=initial)
                else:
                    self.walk(target, phases[0], callee_scope, guards, loops, parents, active)
            for mapping in mappings:
                raw = mapping['attributes']
                # PubIn/Out declares a capability. Only In/Out enables a call-site edge.
                if str(raw.get('In' if incoming else 'Out', '')).casefold() not in {'y', 'true', '1'}:
                    continue
                local, remote = mapping['solution_data_item'], mapping['property_key']
                if not local or not remote:
                    self.issue('INCOMPLETE_MAPPING', event=ev['id'], mapping=mapping['mapping_prefix'])
                    continue
                prefix = mapping['mapping_prefix'] + '_'
                group = a.get(prefix + 'ComponentClassName') == 'PropertyGroup'
                local_ref = self.field(ev['scope'], local, group)
                remote_ref = self.field(callee_scope, remote, group)
                self.event(source, node, ev['scope'] if not incoming else callee_scope,
                           'input_mapping' if incoming else 'output_mapping', guards, loops, parents,
                           reads=[local_ref if incoming else remote_ref],
                           writes=[remote_ref if incoming else local_ref], mapping=raw,
                           mapping_prefix=mapping['mapping_prefix'])

    def collect(self, file: str, field: str | None, rule_eid: str | None, entry: str | None,
                scenario: dict | None = None):
        source = self.source(self.root / file.replace('\\', '/'))
        if source is None:
            return self.slice([], field)
        roots = source.entry_roots(entry)
        if entry and len(roots) != 1:
            raise ValueError(f'Entry must resolve uniquely: {entry!r}')
        if not roots:
            raise ValueError('No trace entry found')
        seeds = []
        for root in roots:
            self.current_entry = source.phase_name(root) or '@' + root.meta.get('eid', str(root.offset))
            start = len(self.events)
            scope = self.current_entry + ':root'
            try:
                self.walk(source, root, scope, [], [], [])
            except (OSError, ValueError) as error:
                self.failed = True
                self.issue('COLLECTION_ERROR', entry=self.current_entry, message=str(error))
            for ev in self.events[start:]:
                if rule_eid and self.nodes[ev['node']]['file'] == source.relative and self.nodes[ev['node']]['eid'] == rule_eid:
                    seeds.append(ev['id'])
                elif field and any(overlaps(w, self.field(scope, field)) for w in ev['writes']):
                    seeds.append(ev['id'])
            if rule_eid:
                anchored = set(seeds)
                seeds.extend(e['id'] for e in self.events[start:] if e['id'] not in anchored and
                             any(p in anchored for p in e['parents']))
        for src in self.sources.values():
            self.issues.extend(src.issues)
            try:
                src.check_unchanged()
            except (ValueError, OSError) as error:
                self.failed = True
                self.issue('FILE_CHANGED', file=src.relative, message=str(error))
        if scenario is not None:
            from .trace_scenario import Scenario, validate_scenario
            validate_scenario(scenario)
            if self.limited or self.failed:
                self.issue('SCENARIO_NOT_EVALUATED', reason='incomplete_collection')
            else:
                self.scenario = Scenario(self, scenario, source.relative).apply()
        return self.slice(seeds, field)

    def slice(self, seeds, field):
        by_id = {e['id']: e for e in self.events}
        selected: set[str] = set()
        partial = not seeds and (self.limited or self.failed)
        queue = deque((eid, None) for eid in (by_id if partial else seeds))
        processed = set()
        edges, inputs = [], []
        while queue:
            eid, demand = queue.popleft()
            task = (eid, json.dumps(demand, sort_keys=True))
            if task in processed:
                continue
            processed.add(task)
            selected.add(eid)
            ev = by_id[eid]
            queue.extend((p, None) for p in ev['parents'])
            queue.extend((g['event'], None) for g in ev['guards'])
            queue.extend((p, None) for p in ev['loops'])
            # Cursor-setting operations are dependencies of later field accesses.
            for prior in self.events[:ev['sequence']]:
                if prior['scope'] == ev['scope'] and prior['kind'] == 'instance' and compatible(prior, ev):
                    queue.append((prior['id'], None))
            for peer in self.events:
                if peer['kind'] == 'instance' and peer['scope'] == ev['scope'] and set(peer['loops']) & set(ev['loops']) and compatible(peer, ev):
                    queue.append((peer['id'], None))
            reads = ev['reads']
            if demand and (ev['kind'] in {'input_mapping', 'output_mapping'} or
                           (ev['kind'] == 'set' and any(w.get('group') for w in ev['writes']))):
                reads = [project_read(r, w, demand) for r in reads for w in ev['writes'] if overlaps(w, demand)]
            for read in reads:
                writers = [p for p in self.events if p['id'] != eid and
                           (p['sequence'] < ev['sequence'] or p['triggers'] != ev['triggers']) and compatible(p, ev)
                           and any(overlaps(w, read) for w in p['writes'])]
                # A definite, non-loop assignment/reset hides older definitions in
                # this activation. Conditional and cross-event writes stay candidates.
                barriers = [p['sequence'] for p in writers if p['kind'] in {'set', 'reset', 'expression'}
                            and not p['loops'] and p['triggers'] == ev['triggers']
                            and all(g in ev['guards'] for g in p['guards'])
                            and any(w['path'] == read['path'] and not any(x in w['path'] for x in ('[C]', '[A]', '$$'))
                                    for w in p['writes'])]
                if barriers:
                    writers = [p for p in writers if p['triggers'] != ev['triggers'] or p['sequence'] >= max(barriers)]
                for writer in writers:
                    queue.append((writer['id'], read))
                    edge = {'from': writer['id'], 'to': eid, 'field': read,
                                  'relation': 'candidate_definition' if len(writers) > 1 or writer['triggers'] != ev['triggers'] else 'definition',
                                  'event_order': 'unknown_between_ui_events' if writer['triggers'] != ev['triggers'] else 'configuration_order',
                                  'conditional': writer['triggers'] != ev['triggers'] or any(g not in ev['guards'] for g in writer['guards'])}
                    if edge not in edges:
                        edges.append(edge)
                if not writers:
                    item = {'event': eid, 'field': read, 'status': 'external_or_unresolved'}
                    if item not in inputs:
                        inputs.append(item)
                elif all(p['triggers'] != ev['triggers'] or any(g not in ev['guards'] for g in p['guards']) for p in writers):
                    item = {'event': eid, 'field': read, 'status': 'conditional_write_or_prior_value'}
                    if item not in inputs:
                        inputs.append(item)
            # An opaque preceding rule may affect the value. Do not silently skip it.
            for prior in self.events[:ev['sequence']]:
                if prior['scope'] == ev['scope'] and prior['kind'] in {'unknown', 'goto'} and compatible(prior, ev):
                    queue.append((prior['id'], None))
        if not seeds:
            self.issue('ANCHOR_NOT_FOUND', field=field)
        events = [e for e in self.events if e['id'] in selected]
        node_keys = {e['node'] for e in events}
        issues = [i for i in self.issues if not i.get('event') or i['event'] in selected]
        scenario = None
        if self.scenario is not None:
            scenario = {**self.scenario,
                        'conditions': [c for c in self.scenario['conditions'] if c['event'] in selected],
                        'classifications': [c for c in self.scenario['classifications'] if c['event'] in selected]}
        return {'schema_version': 1, 'analysis': 'static_candidates', 'seeds': seeds,
                'configuration': {'trace_semantics_sha256': self.semantics.signature,
                                  'api_rules': self.config.signature_payload()},
                'scenario': scenario,
                'events': events, 'nodes': {k: v for k, v in self.nodes.items() if k in node_keys},
                'edges': edges, 'inputs': inputs, 'issues': issues,
                'coverage': {'files_read': len(self.sources), 'contexts_examined': len(self.events),
                             'contexts_exported': len(events), 'limited': self.limited,
                             'partial_scope_only': partial,
                             'runtime_verified': False},
                'files': {s.relative: {'sha256': s.sha256, 'size': s.before.st_size}
                          for s in self.sources.values()}}
