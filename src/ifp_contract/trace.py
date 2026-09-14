"""Context-sensitive evidence collection, with conservative backward data slicing.

This is not a UXP interpreter: branches and loop iterations remain symbolic.
"""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import re
from time import perf_counter

from .config import load_rule_config
from .extractor import mappings_from_attributes, _value, rule_base_url
from .trace_source import TraceNode, TraceSource
from .trace_config import TraceConfig, STANDARD_KINDS, ATTRIBUTES
from .trace_index import TraceIndex
from .behavior import effective_disabled, selector_value, parse_bool
from .trace_partial import partial_rule_evidence
from .trace_assessment import assess_conclusions, evidence_closures
from .templates import condition_references, field_references, template_evidence


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
                 max_files: int = 50, max_contexts: int = 5000,
                 cache_dir: Path | None = None):
        self.root = root.resolve()
        self.config = load_rule_config(rules_config)
        self.semantics = TraceConfig(rules_config)
        for name in self.semantics.rules:
            if self.config.is_api_rule(name):
                raise ValueError(f'Conflicting API and trace semantics for {name}')
        for name in self.config.exact_api_rule_classes:
            if self.semantics.resolve(name)['kind'] != 'unknown':
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
        self.disabled_cache: dict[str, bool] = {}
        self.cache_dir = cache_dir
        self.performance = {'source_seconds': 0.0}

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
            started = perf_counter()
            result = TraceSource(self.root, path, extra_metadata=self.semantics.extra_metadata,
                                 cache_dir=self.cache_dir)
            self.performance['source_seconds'] += perf_counter() - started
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
        item['triggers'] = [p for p in parents if self.events[int(p[1:])]['kind'] in {'ui_event', 'ui_input', 'product_rules'}]
        if kind in {'ui_event', 'ui_input', 'product_rules'}:
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
        if kind == 'call':
            a['SelectComponent'] = selector_value(
                raw, spec, self.config.attribute_names_for_rule(full_class, 'selector'),
                self.semantics.normalize) or ''
        if node.tag in {'Question', 'Button'}:
            kind = 'ui_event'
            if (node.tag == 'Question' and a.get('PropertyKey') and
                not any(parse_bool(a.get(k)) for k in ('ReadOnly', 'DisableInput'))):
                kind = 'ui_input'
        if effective_disabled(node, source.attributes, self.semantics.resolve,
                              self.semantics.normalize, self.disabled_cache):
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

        def add_condition_reads(expression):
            paths = condition_references(expression)
            ev['reads'].extend(self.field(scope, p, p.endswith(']')) for p in paths)
            ev['condition_dependencies'] = {
                'status': 'lexically_extracted', 'fields': paths,
                'completeness': 'best_effort_not_proven',
                'expression_evaluation': 'separate_and_may_be_unsupported',
            }

        if a.get('ConditionExpression'):
            ev['ui_condition'] = {'expression': a['ConditionExpression'],
                                  'not_applicable': a.get('NotApplicable'),
                                  'status': 'configuration_only_not_a_proven_execution_gate'}
            ev['ui_condition_fields'] = [self.field(scope, p, p.endswith(']'))
                                         for p in condition_references(a['ConditionExpression'])]
        if kind == 'ui_input':
            ev['writes'].append(self.field(scope, a['PropertyKey']))
            ev['value_origin'] = 'user_or_existing_ui_value'
        elif node.tag == 'Question' and a.get('PropertyKey'):
            ev['display_field'] = self.field(scope, a['PropertyKey'])
            ev['reads'].append(ev['display_field'])
            ev['value_origin'] = 'displayed_existing_value'
        if kind in {'evaluate', 'expression'}:
            if kind == 'evaluate':
                add_condition_reads(a.get('Expression', ''))
            else:
                add_reads(a.get('Expression', ''))
            if kind == 'expression' and a.get('OutputProperty'):
                ev['writes'].append(self.field(scope, a['OutputProperty']))
        elif kind == 'set':
            typ, from_type = a.get('Type', 'Data Item'), a.get('FromType', 'Value')
            ev['assignment_modes'] = {'target': typ, 'source': from_type,
                                      'source_defaulted': 'FromType' not in a,
                                      'target_defaulted': 'Type' not in a}
            target_keys = {'Data Item': 'PropertyName', 'Variable': 'VariableName',
                           'Data Group': 'PropertyGroupName',
                           'Data Group Instance': 'PropertyGroupInstanceName'}
            target_key = target_keys.get(typ)
            if target_key is None:
                self.issue('UNSUPPORTED_ASSIGNMENT_TARGET_TYPE', event=ev['id'], target_type=typ)
            target = a.get(target_key, '') if target_key else ''
            if target:
                prefix = '!' if typ == 'Variable' else '@instance:' if typ == 'Data Group Instance' else ''
                ev['writes'].append(self.field(scope, prefix + target,
                                               typ == 'Data Group'))
            source_keys = {'Value': None, 'Data Item': 'FromPropertyName',
                           'Variable': 'FromVariableName', 'Data Group': 'FromPropertyGroupName',
                           'Data Group Instance': 'FromPropertyGroupInstanceName'}
            source_key = source_keys.get(from_type)
            if from_type not in source_keys:
                self.issue('UNSUPPORTED_ASSIGNMENT_SOURCE_TYPE', event=ev['id'], source_type=from_type)
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
        elif self.config.is_api_rule(full_class):
            ev['kind'] = 'api'
            names = lambda concept: self.config.attribute_names_for_rule(full_class, concept)
            by_lower = {key.casefold(): (key, value) for key, value in a.items()}
            explicit_method_item = next(
                (by_lower[name.casefold()] for name in names('method')
                 if name.casefold() in by_lower and by_lower[name.casefold()][1] != ''), None)
            if explicit_method_item is None:
                explicit_method_item = next(
                    ((key, value) for key, value in a.items()
                     if value and key.casefold().endswith('method')), None)
            explicit_method = explicit_method_item[1] if explicit_method_item else None
            method_resolution = self.config.method_resolution(full_class)
            method = explicit_method or (method_resolution or {}).get('method')
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
            ev['api']['config_resolution'] = {
                'rule_class': self.config.api_rule_resolution(full_class),
                'method': ({'match': 'attribute', 'key': explicit_method_item[0],
                            'method': explicit_method} if explicit_method else method_resolution),
            }
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
                partial = partial_rule_evidence(full_class, a)
                if partial:
                    ev['partial_semantics'] = {'status': 'declared_attributes_only',
                                               'runtime_behavior_verified': False,
                                               **partial['evidence']}
                    ev['semantics']['incomplete'] = True
                    for key, paths in (('reads', partial['read_paths']),
                                       ('writes', partial['write_paths'])):
                        for path in paths:
                            value = self.field(scope, path, path.endswith(']'))
                            if value not in ev[key]:
                                ev[key].append(value)
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
            if kind == 'evaluate' and branch_value is None:
                self.issue('UNKNOWN_BRANCH', event=ev['id'], label=branch_label, child=child.key,
                           file=source.relative, line=child.line,
                           branch_attribute=spec.get('branch_attribute', 'RuleType'))
                child_guards = [*guards, {'event': ev['id'], 'branch': branch_label,
                                         'source_branch': branch_label,
                                         'kind': 'unknown_condition',
                                         'expression': a.get('Expression'),
                                         'status': 'unknown_branch_label'}]
            if branch_value is not None and node.tag in {'Rule', 'Question', 'Button'}:
                child_guards = [*guards, {'event': ev['id'], 'branch': branch,
                                'source_branch': branch_label,
                                'kind': 'condition' if kind == 'evaluate' else 'rule_result',
                                'expression': a.get('Expression') if kind == 'evaluate' else None}]
            self.walk(source, child, scope, child_guards, child_loops, descendants, active)
        source.release_attributes(node)

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
                    self.walk_entry(target, phases[0], callee_scope, guards, loops, parents, active)
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

    def walk_entry(self, source, root, scope, guards, loops, parents, active=()):
        # Product-level rules are candidates available to this phase, but their
        # scheduling relative to phase/UI rules is not established by nesting.
        # Give them a separate activation so they cannot become definite writes
        # merely because collection visits them first. Sibling phases stay out.
        product = root.parent if root.tag == 'Phase' else None
        if product is not None and product.tag == 'Product':
            rules = [node for node in product.children if node.tag == 'Rule']
            if rules:
                context = self.event(source, product, scope, 'product_rules', guards, loops, parents,
                                     scheduling='unknown_relative_to_phase')
                if context is not None:
                    for rule in rules:
                        self.walk(source, rule, scope, guards, loops, [*parents, context['id']], active)
        self.walk(source, root, scope, guards, loops, parents, active)

    def collect(self, file: str, field: str | None, rule_eid: str | None, entry: str | None,
                scenario: dict | None = None):
        started = perf_counter()
        source = self.source(self.root / file.replace('\\', '/'))
        if source is None:
            return self._finish_collect([], field, started)
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
                self.walk_entry(source, root, scope, [], [], [])
            except (OSError, ValueError) as error:
                self.failed = True
                self.issue('COLLECTION_ERROR', entry=self.current_entry, message=str(error))
            for ev in self.events[start:]:
                if rule_eid and self.nodes[ev['node']]['file'] == source.relative and self.nodes[ev['node']]['eid'] == rule_eid:
                    seeds.append(ev['id'])
                elif field and any(overlaps(w, self.field(scope, field)) for w in ev['writes']):
                    seeds.append(ev['id'])
                elif field and ev.get('display_field') and overlaps(ev['display_field'], self.field(scope, field)):
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
        return self._finish_collect(seeds, field, started)

    def _finish_collect(self, seeds, field, started):
        self.performance['collection_seconds'] = perf_counter() - started
        sliced = perf_counter()
        result = self.slice(seeds, field)
        self.performance['slice_seconds'] = perf_counter() - sliced
        cached = perf_counter()
        if not self.failed:
            for source in self.sources.values():
                source.save_cache()
        self.performance.update(
            cache_write_seconds=perf_counter() - cached,
            source_files=len(self.sources),
            source_bytes=sum(s.before.st_size for s in self.sources.values()),
            cache_hits=sum(s.cache_hit for s in self.sources.values()),
        )
        return result

    def slice(self, seeds, field):
        by_id = {e['id']: e for e in self.events}
        index = TraceIndex(self.events, shape)
        selected: set[str] = set()
        partial = not seeds and (self.limited or self.failed)
        queue = deque()
        scheduled = set()
        edges, inputs = [], []
        edge_keys, input_keys = set(), set()
        opaque_by_target: dict[str, set[str]] = {}
        writer_cache = {}
        context_only = set()
        expanded = set()

        def retain(eid):
            # Containment records location and activation, not a demand for
            # every value read by the enclosing rule. Keep references closed
            # without feeding these nodes back into the dependency queue.
            pending = [eid]
            while pending:
                current = pending.pop()
                if current in context_only:
                    continue
                context_only.add(current)
                selected.add(current)
                context = by_id[current]
                pending.extend(context['parents'])
                pending.extend(g['event'] for g in context['guards'])
                pending.extend(context['loops'])

        def read_key(read):
            return tuple(sorted(read.items()))

        def enqueue(eid, demand=None):
            task = (eid, read_key(demand) if demand is not None else None)
            if task not in scheduled:
                scheduled.add(task)
                queue.append((eid, demand))

        def add_input(eid, read, status):
            key = (eid, read_key(read), status)
            if key not in input_keys:
                input_keys.add(key)
                inputs.append({'event': eid, 'field': read, 'status': status})

        for eid in (by_id if partial else seeds):
            seed = by_id[eid]
            demand = None
            if field and not partial:
                path = field.removeprefix('!')
                demand = {
                    'scope': seed['entry'] + ':session' if field.startswith('!') else seed['scope'],
                    'path': path,
                    'group': any(w.get('group') and shape(w['path']) == shape(path)
                                 for w in seed['writes']),
                }
            enqueue(eid, demand)
        while queue:
            eid, demand = queue.popleft()
            selected.add(eid)
            expanded.add(eid)
            ev = by_id[eid]
            for parent in ev['parents']:
                retain(parent)
            for guard in ev['guards']:
                enqueue(guard['event'])
            for loop in ev['loops']:
                enqueue(loop)
            # Cursor-setting operations are dependencies of later field accesses.
            for prior in index.instances.get(ev['scope'], ()):
                if prior['sequence'] >= ev['sequence']:
                    break
                if index.compatible(prior, ev):
                    enqueue(prior['id'])
            for peer in index.instances.get(ev['scope'], ()):
                if set(peer['loops']) & set(ev['loops']) and index.compatible(peer, ev):
                    enqueue(peer['id'])
            reads = ev['reads']
            if demand and (ev['kind'] in {'input_mapping', 'output_mapping'} or
                           (ev['kind'] == 'set' and any(w.get('group') for w in ev['writes']))):
                reads = [project_read(r, w, demand) for r in reads for w in ev['writes'] if overlaps(w, demand)]
            demands = [(read, 'value') for read in reads]
            demands.extend((read, 'control') for read in ev.get('ui_condition_fields', ()))
            for read, dependency_role in demands:
                key = (eid, read_key(read))
                writers = writer_cache.get(key)
                if writers is None:
                    writers = [p for p in index.candidates(read) if p['id'] != eid and
                               (p['sequence'] < ev['sequence'] or p['triggers'] != ev['triggers'])
                               and index.compatible(p, ev)
                               and any(overlaps(w, read) for w in p['writes'])]
                    # A definite, non-loop assignment/reset hides older definitions
                    # in this activation. Conditional and cross-event writes stay candidates.
                    barriers = [p['sequence'] for p in writers if p['kind'] in {'set', 'reset', 'expression'}
                                and not p['loops'] and p['triggers'] == ev['triggers']
                                and all(g in ev['guards'] for g in p['guards'])
                                and any(w['path'] == read['path'] and not any(x in w['path'] for x in ('[C]', '[A]', '$$'))
                                        for w in p['writes'])]
                    if barriers:
                        barrier = max(barriers)
                        writers = [p for p in writers if p['triggers'] != ev['triggers'] or p['sequence'] >= barrier]
                    writer_cache[key] = writers
                for writer in writers:
                    enqueue(writer['id'], read)
                    edge = {'from': writer['id'], 'to': eid, 'field': read,
                                  'relation': 'candidate_definition' if len(writers) > 1 or writer['triggers'] != ev['triggers'] else 'definition',
                                  'event_order': 'unknown_between_ui_events' if writer['triggers'] != ev['triggers'] else 'configuration_order',
                                  'conditional': writer['triggers'] != ev['triggers'] or any(g not in ev['guards'] for g in writer['guards'])}
                    if dependency_role == 'control':
                        edge['dependency_role'] = 'control'
                    if (writer['triggers'] != ev['triggers'] and
                        any(by_id[t]['kind'] == 'product_rules'
                            for t in writer['triggers'] + ev['triggers'] if t in by_id)):
                        edge['event_order'] = 'unknown_product_scheduling'
                    edge_key = (writer['id'], eid, read_key(read), edge['relation'],
                                edge['event_order'], edge['conditional'], dependency_role)
                    if edge_key not in edge_keys:
                        edge_keys.add(edge_key)
                        edges.append(edge)
                if not writers:
                    add_input(eid, read, 'external_or_unresolved')
                elif all(p['triggers'] != ev['triggers'] or any(g not in ev['guards'] for g in p['guards']) for p in writers):
                    add_input(eid, read, 'conditional_write_or_prior_value')
            # An opaque preceding rule may affect the value. Do not silently skip it.
            for prior in index.opaque.get(ev['scope'], ()):
                if prior['sequence'] >= ev['sequence']:
                    break
                if index.compatible(prior, ev):
                    retain(prior['id'])
                    opaque_by_target.setdefault(eid, set()).add(prior['id'])
        if not seeds:
            self.issue('ANCHOR_NOT_FOUND', field=field)
        events = [{**e, 'slice_role': 'dependency' if e['id'] in expanded else 'context_or_boundary'}
                  for e in self.events if e['id'] in selected]
        node_keys = {e['node'] for e in events}
        issues = [i for i in self.issues if not i.get('event') or i['event'] in selected]
        scenario = None
        if self.scenario is not None:
            scenario = {**self.scenario,
                        'conditions': [c for c in self.scenario['conditions'] if c['event'] in selected],
                        'classifications': [c for c in self.scenario['classifications'] if c['event'] in selected]}
        coverage = {'files_read': len(self.sources), 'contexts_examined': len(self.events),
                    'contexts_exported': len(events), 'limited': self.limited,
                    'partial_scope_only': partial, 'runtime_verified': False,
                    'dependency_contexts': len(expanded),
                    'context_or_boundary_contexts': len(selected - expanded)}
        # The slicer considers opaque predecessors for every field demand.  Keep
        # only those that reach an anchor through its final evidence closure;
        # this avoids exporting a quadratic internal work list.
        closures = evidence_closures(events, edges, seeds)
        order = {event['id']: event['sequence'] for event in events}
        opaque_dependencies = []
        for seed, closure in closures.items():
            sources = set().union(*(opaque_by_target.get(event_id, set()) for event_id in closure))
            for source in sorted(sources, key=order.get):
                opaque_dependencies.append({'from': source, 'to': seed,
                                            'relation': 'opaque_rule_may_affect_anchor'})
        conclusions = assess_conclusions(events, edges, inputs, issues, seeds, coverage,
                                         opaque_dependencies, scenario)
        return {'schema_version': 1, 'analysis': 'static_candidates', 'seeds': seeds,
                'slice_policy': {'containment': 'context_only',
                                 'possible_opaque_effect': 'boundary_only',
                                 'explicit_dependencies': 'follow_reads_guards_loops',
                                 'anchor_field': field},
                'configuration': {'trace_semantics_sha256': self.semantics.signature,
                                  'api_rules': self.config.signature_payload()},
                'scenario': scenario,
                'events': events, 'nodes': {k: v for k, v in self.nodes.items() if k in node_keys},
                'edges': edges, 'opaque_dependencies': opaque_dependencies, 'inputs': inputs,
                'issues': issues, 'conclusions': conclusions, 'coverage': coverage,
                'files': {s.relative: {'sha256': s.sha256, 'size': s.before.st_size}
                          for s in self.sources.values()}}
