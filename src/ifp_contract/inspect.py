"""Read-only compatibility census for adapting trace semantics to an IFP corpus."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import os
from pathlib import Path

from .config import DEFAULT_ALIASES, load_rule_config
from .behavior import effective_disabled, selector_value
from .extractor import _value, _value_ending_with
from .trace_config import TraceConfig
from .trace_source import TraceNode, TraceSource


ISSUE_HELP = {
    'UNKNOWN_RULE': ('warning', 'Rule behavior is opaque to trace.',
                     'Classify the Rule only after confirming its reads, writes and control behavior.'),
    'UNKNOWN_BRANCH': ('warning', 'The child keeps an unknown execution prerequisite.',
                       'Declare the parent branch attribute and exact label mapping.'),
    'MISSING_SET_TARGET': ('warning', 'The configured assignment has no recognized write target.',
                           'Check Type and target attribute aliases for this Rule.'),
    'MISSING_ASSIGNMENT_SOURCE': ('warning', 'The configured assignment source attribute is absent.',
                                  'Check FromType, defaults and source attribute aliases.'),
    'UNSUPPORTED_ASSIGNMENT_TARGET_TYPE': ('warning', 'The assignment target type has no built-in trace semantics.',
                                           'Add a project Rule adaptation after confirming its target scope.'),
    'UNSUPPORTED_ASSIGNMENT_SOURCE_TYPE': ('warning', 'The assignment source type has no built-in trace semantics.',
                                           'Add a project Rule adaptation after confirming how its value is read.'),
    'MISSING_EXPRESSION': ('warning', 'A condition/expression Rule has no recognized expression.',
                           'Configure the expression attribute or confirm that an empty value is valid.'),
    'MISSING_COMPONENT_SELECTOR': ('warning', 'A component call has no recognized file selector.',
                                   'Configure SelectComponent or the project selector attribute.'),
    'DYNAMIC_COMPONENT': ('warning', 'The component selector still contains runtime substitutions.',
                          'Supply deterministic --path-var values or collect the target separately.'),
    'COMPONENT_NOT_FOUND': ('warning', 'The component selector did not resolve under the inspected root.',
                            'Check the component root and path-variable mapping.'),
    'COMPONENT_NOT_UNIQUE': ('warning', 'The component selector resolves to more than one file.',
                             'Use an unambiguous component path.'),
    'OUTSIDE_ROOT': ('warning', 'A component selector resolves outside the declared root.',
                     'Choose a common root that contains every followed component.'),
    'API_ATTRIBUTE_CONFLICT': ('warning', 'Several aliases provide different values for one API concept.',
                               'Add an explicit attribute override for the intended precedence.'),
    'MISSING_API_METHOD': ('warning', 'The API Rule has no explicit or configured HTTP method.',
                           'Add a Rule attribute or class method mapping.'),
    'FILE_ERROR': ('error', 'The file could not be structurally inspected.',
                   'Fix the reported encoding/XML problem or collect the file separately.'),
    'FILE_DISCOVERY_ERROR': ('error', 'Part of the requested directory could not be enumerated.',
                             'Check access to the reported directory.'),
    'SCAN_FILE_LIMIT': ('warning', 'Only the configured prefix of the file list was inspected.',
                        'Increase --max-files to inspect the remaining files.'),
    'API_TRACE_SEMANTIC_CONFLICT': ('error', 'The same class is configured as API and behavioral Rule.',
                                    'Remove one classification so build and trace share one meaning.'),
    'TRUNCATED_ATTRIBUTE': ('warning', 'A large source attribute was truncated during inspection.',
                            'Inspect the original Rule before declaring complete semantics.'),
    'DUPLICATE_ATTRIBUTE': ('warning', 'A source tag contains duplicate XML attributes.',
                            'Correct the source XML; attribute precedence is not reliable.'),
    'LINK_NOT_FOUND': ('error', 'A LinkReference has no matching eid in the inspected file.',
                       'Declare the target Rule with a unique eid or correct the reference.'),
    'LINK_NOT_UNIQUE': ('error', 'A LinkReference resolves to multiple eids in the inspected file.',
                        'Make the target eid unique before relying on the shared Rule.'),
    'LINK_RECURSIVE': ('error', 'A LinkReference points back to itself.',
                       'Break the shared Rule cycle before tracing its behavior.'),
}


class Findings:
    def __init__(self, max_samples: int):
        self.max_samples = max_samples
        self.counts: Counter[str] = Counter()
        self.samples: dict[str, list[dict]] = defaultdict(list)

    def add(self, code: str, **sample) -> None:
        self.counts[code] += 1
        if len(self.samples[code]) < self.max_samples:
            self.samples[code].append(sample)

    def export(self) -> list[dict]:
        result = []
        for code in sorted(self.counts):
            severity, impact, action = ISSUE_HELP[code]
            result.append({'code': code, 'severity': severity, 'count': self.counts[code],
                           'impact': impact, 'action': action, 'samples': self.samples[code]})
        return result


def _files(root: Path, findings: Findings) -> tuple[Path, list[Path]]:
    root = root.resolve()
    if root.is_file():
        if root.suffix.casefold() != '.ifp':
            raise ValueError('inspect root file must use the .ifp extension')
        return root.parent, [root]
    if not root.is_dir():
        raise ValueError(f'IFP root does not exist or is not a directory: {root}')
    found = []

    def failed(error: OSError) -> None:
        findings.add('FILE_DISCOVERY_ERROR', path=error.filename, message=str(error))

    for directory, names, files in os.walk(root, onerror=failed):
        names.sort(key=str.casefold)
        for name in sorted(files, key=str.casefold):
            if name.casefold().endswith('.ifp'):
                found.append(Path(directory) / name)
    return root, found


def _location(source: TraceSource, node: TraceNode, **details) -> dict:
    return {'file': source.relative, 'line': node.line, 'offset': node.offset,
            'eid': node.meta.get('eid'), 'name': node.meta.get('Name'), **details}


def _component_check(root: Path, source: TraceSource, node: TraceNode, selector: str,
                     variables: dict[str, str], findings: Findings) -> None:
    value = selector.replace('\\', '/')
    for name, replacement in variables.items():
        value = value.replace('$$' + name + '$', replacement.replace('\\', '/'))
    if '$' in value:
        findings.add('DYNAMIC_COMPONENT', **_location(source, node, selector=selector,
                                                      unresolved=value))
        return
    raw_candidates = (root / value, source.path.parent / value)
    outside = [path.resolve() for path in raw_candidates
               if path.is_file() and not path.resolve().is_relative_to(root)]
    if outside:
        findings.add('OUTSIDE_ROOT', **_location(source, node, selector=selector,
                                                 candidates=[str(path) for path in outside]))
        return
    candidates = sorted({path.resolve() for path in raw_candidates
                         if path.is_file() and path.resolve().is_relative_to(root)})
    if not candidates:
        findings.add('COMPONENT_NOT_FOUND', **_location(source, node, selector=selector,
                                                        attempted=[str(path) for path in raw_candidates]))
    elif len(candidates) > 1:
        findings.add('COMPONENT_NOT_UNIQUE', **_location(source, node, selector=selector,
                                                         candidates=[str(path) for path in candidates]))


def _inspect_rule(root: Path, source: TraceSource, node: TraceNode, rule_config,
                  semantics: TraceConfig, variables: dict[str, str], findings: Findings,
                  class_counts: dict[str, Counter], stats: Counter,
                  disabled_cache: dict[str, bool]) -> None:
    raw = source.attributes(node)
    full_class = raw.get('RuleClassName', raw.get('ClassType', ''))
    class_name = full_class or '<missing>'
    spec = semantics.resolve(full_class)
    normalized = semantics.normalize(raw, spec)
    api_resolution = rule_config.api_rule_resolution(full_class)
    is_component = rule_config.is_component_rule(full_class)
    is_link = bool(raw.get('LinkReference'))
    understood = spec['kind'] != 'unknown' or api_resolution is not None or is_link
    disabled = effective_disabled(node, source.attributes, semantics.resolve,
                                  semantics.normalize, disabled_cache)
    stats['rules'] += 1
    stats['disabled_rules'] += int(disabled)
    stats['semantically_understood_rules'] += int(understood)

    if api_resolution:
        category = 'api'
        stats['api_rules'] += 1
    elif spec['kind'] != 'unknown' or is_link:
        category = 'project' if spec['origin'] == 'project_extension' else 'standard'
        stats[category + '_rules'] += 1
    elif is_component:
        category = 'component_class_only'
        stats['component_class_only_rules'] += 1
    else:
        category = 'unknown'
        stats['unknown_rules'] += 1
        findings.add('UNKNOWN_RULE', **_location(source, node, rule_class=class_name))
    class_counts[category][class_name] += 1

    if api_resolution and spec['kind'] != 'unknown' and not is_link:
        findings.add('API_TRACE_SEMANTIC_CONFLICT',
                     **_location(source, node, rule_class=class_name,
                                 trace_kind=spec['kind'], api_match=api_resolution))

    kind = spec['kind']
    if not is_link and kind in {'evaluate', 'expression'} and not normalized.get('Expression'):
        findings.add('MISSING_EXPRESSION', **_location(source, node, rule_class=class_name,
                                                       trace_kind=kind))
    if not is_link and kind == 'set':
        target_type = normalized.get('Type', 'Data Item')
        target_keys = {'Data Item': 'PropertyName', 'Variable': 'VariableName',
                       'Data Group': 'PropertyGroupName',
                       'Data Group Instance': 'PropertyGroupInstanceName'}
        target_key = target_keys.get(target_type)
        if target_key is None:
            findings.add('UNSUPPORTED_ASSIGNMENT_TARGET_TYPE',
                         **_location(source, node, rule_class=class_name, target_type=target_type))
        elif not normalized.get(target_key):
            findings.add('MISSING_SET_TARGET', **_location(
                source, node, rule_class=class_name, attribute=target_key))
        source_type = normalized.get('FromType', 'Value')
        source_keys = {'Value': None, 'Data Item': 'FromPropertyName',
                       'Variable': 'FromVariableName', 'Data Group': 'FromPropertyGroupName',
                       'Data Group Instance': 'FromPropertyGroupInstanceName'}
        source_key = source_keys.get(source_type)
        if source_type not in source_keys:
            findings.add('UNSUPPORTED_ASSIGNMENT_SOURCE_TYPE',
                         **_location(source, node, rule_class=class_name, source_type=source_type))
        if source_key and not normalized.get(source_key):
            findings.add('MISSING_ASSIGNMENT_SOURCE',
                         **_location(source, node, rule_class=class_name, attribute=source_key))

    if not is_link and kind == 'evaluate':
        branch_attribute = spec.get('branch_attribute', 'RuleType')
        branches = spec.get('branches', {'True': True, 'False': False})
        for child in (item for item in node.children if item.tag == 'Rule'):
            label = child.meta.get(branch_attribute, '')
            if label not in branches:
                findings.add('UNKNOWN_BRANCH', **_location(
                    source, child, parent_rule_class=class_name, parent_line=node.line,
                    branch_attribute=branch_attribute, label=label))

    if kind == 'call' and not is_link:
        selector = selector_value(raw, spec,
                                  rule_config.attribute_names_for_rule(full_class, 'selector'),
                                  semantics.normalize)
        if not selector:
            findings.add('MISSING_COMPONENT_SELECTOR', **_location(source, node,
                                                                   rule_class=class_name))
        else:
            _component_check(root, source, node, selector, variables, findings)

    if api_resolution and not is_link:
        method = _value(raw, *rule_config.attribute_names_for_rule(full_class, 'method'))
        method = (method or _value_ending_with(raw, 'method') or
                  rule_config.method_for_rule_class(full_class))
        if not method:
            findings.add('MISSING_API_METHOD', **_location(source, node, rule_class=class_name,
                                                           api_match=api_resolution))
        scalar_concepts = {'source', 'method', 'path', 'base_url', 'filter', 'payload',
                           'header_name', 'header_value', 'language'}
        for concept in scalar_concepts:
            if rule_config.has_attribute_override(full_class, concept):
                continue
            names = rule_config.attribute_names_for_rule(full_class, concept)
            by_lower = {key.casefold(): (key, value) for key, value in raw.items()}
            present = [by_lower[name.casefold()] for name in names
                       if name.casefold() in by_lower and by_lower[name.casefold()][1] != '']
            values = {value for _, value in present}
            if len(values) > 1:
                findings.add('API_ATTRIBUTE_CONFLICT', **_location(
                    source, node, rule_class=class_name, concept=concept,
                    attributes=[{'name': name, 'value': value} for name, value in present]))

    if is_link:
        target = raw.get('LinkReference', '').strip()
        targets = source.eids.get(target, [])
        if not targets:
            findings.add('LINK_NOT_FOUND', **_location(source, node, target=target))
        elif len(targets) > 1:
            findings.add('LINK_NOT_UNIQUE', **_location(
                source, node, target=target,
                candidates=[item.key for item in targets]))
        elif targets[0] is node:
            findings.add('LINK_RECURSIVE', **_location(source, node, target=target))


def inspect_project(root: Path, *, rules_config: Path | None = None,
                    path_variables: dict[str, str] | None = None,
                    max_files: int = 10_000, max_samples: int = 5,
                    progress=None) -> tuple[dict, dict]:
    if max_files <= 0 or max_samples <= 0:
        raise ValueError('inspect limits must be positive')
    findings = Findings(max_samples)
    corpus_root, files = _files(root, findings)
    discovered = len(files)
    if discovered > max_files:
        findings.add('SCAN_FILE_LIMIT', discovered=discovered, max_files=max_files,
                     omitted=discovered - max_files)
        files = files[:max_files]
    rule_config = load_rule_config(rules_config)
    semantics = TraceConfig(rules_config)
    class_counts: dict[str, Counter] = defaultdict(Counter)
    stats: Counter = Counter(files_discovered=discovered, files_selected=len(files))
    scanned_files = []
    variables = path_variables or {}
    for index, path in enumerate(files, 1):
        if progress:
            progress(index, len(files), path)
        try:
            source = TraceSource(corpus_root, path, extra_metadata=semantics.extra_metadata)
            disabled_cache: dict[str, bool] = {}
            for node in source.nodes:
                if node.tag == 'Rule':
                    _inspect_rule(corpus_root, source, node, rule_config, semantics, variables,
                                  findings, class_counts, stats, disabled_cache)
            for issue in source.issues:
                if issue['code'] == 'TRUNCATED_ATTRIBUTE':
                    node = next((item for item in source.nodes if item.key == issue['node']), None)
                    findings.add('TRUNCATED_ATTRIBUTE',
                                 **(_location(source, node, attribute=issue['attribute'])
                                    if node else {'file': source.relative, 'attribute': issue['attribute']}))
                elif issue['code'] == 'DUPLICATE_ATTRIBUTE':
                    node = next((item for item in source.nodes if item.key == issue['node']), None)
                    findings.add('DUPLICATE_ATTRIBUTE',
                                 **(_location(source, node, attributes=issue['attributes'])
                                    if node else {'file': source.relative, 'attributes': issue['attributes']}))
            stats['files_scanned'] += 1
            scanned_files.append({'file': source.relative, 'sha256': source.sha256,
                                  'size': source.before.st_size, 'rules': sum(
                                      1 for node in source.nodes if node.tag == 'Rule')})
        except (OSError, ValueError) as error:
            stats['files_failed'] += 1
            try:
                relative = path.relative_to(corpus_root).as_posix()
            except ValueError:
                relative = str(path)
            findings.add('FILE_ERROR', file=relative, message=str(error))
    classes = {category: [{'class': name, 'count': count}
                          for name, count in sorted(counts.items(), key=lambda item: item[0].casefold())]
               for category, counts in sorted(class_counts.items())}
    result = {
        'schema_version': 1,
        'analysis': 'static_compatibility_census',
        'root': str(corpus_root),
        'coverage': dict(sorted(stats.items())),
        'classes': classes,
        'findings': findings.export(),
        'path_variables': dict(sorted(variables.items())),
        'configuration': {'trace_semantics_sha256': semantics.signature,
                          'api_rules': rule_config.signature_payload()},
        'files': scanned_files,
        'runtime_verified': False,
    }
    draft = json.loads(rules_config.read_text(encoding='utf-8')) if rules_config else {}
    return result, draft


def _inline(value) -> str:
    return str(value).replace('`', '\\`').replace('\n', r'\n')


def compatibility_markdown(data: dict) -> str:
    coverage = data['coverage']
    lines = ['# IFP trace compatibility report', '',
             'Static, read-only census. Class recognition does not by itself prove complete data-flow semantics.', '',
             '## Coverage', '',
             f"- Files discovered: {coverage.get('files_discovered', 0)}",
             f"- Files scanned: {coverage.get('files_scanned', 0)}",
             f"- Files failed: {coverage.get('files_failed', 0)}",
             f"- Rules: {coverage.get('rules', 0)}",
             f"- Semantically understood Rules: {coverage.get('semantically_understood_rules', 0)}",
             f"- Disabled Rules: {coverage.get('disabled_rules', 0)}", '',
             '## Rule classes', '']
    for category, entries in data['classes'].items():
        lines.append(f"### {category}")
        lines.append('')
        for item in entries:
            lines.append(f"- `{_inline(item['class'])}`: {item['count']}")
        if not entries:
            lines.append('- None')
        lines.append('')
    lines += ['## Findings', '']
    if not data['findings']:
        lines += ['No compatibility findings in the inspected scope.', '']
    for finding in data['findings']:
        lines += [f"### {finding['code']} ({finding['count']}, {finding['severity']})", '',
                  finding['impact'], '', f"Action: {finding['action']}", '']
        for sample in finding['samples']:
            location = f"{sample.get('file', '?')}:{sample.get('line', '?')}"
            details = {key: value for key, value in sample.items()
                       if key not in {'file', 'line', 'offset'}}
            encoded = json.dumps(details, ensure_ascii=False, sort_keys=True)
            lines.append(f"- `{_inline(location)}`: `{_inline(encoded)}`")
        lines.append('')
    lines += ['## Limits', '',
              '- Runtime execution was not observed.',
              '- Windows path syntax is normalized during selector checks, but this report was not '
              'a native Windows run.',
              '- Unknown classes remain unclassified in the draft configuration.', '']
    return '\n'.join(lines)


def todo_markdown(data: dict) -> str:
    lines = ['# IFP trace adaptation TODO', '',
             'Resolve these items from source evidence before adding project semantics.', '']
    actionable = [finding for finding in data['findings']
                  if finding['code'] not in {'SCAN_FILE_LIMIT'}]
    if not actionable:
        lines.append('- [x] No adaptation items found in the inspected scope.')
    for finding in actionable:
        lines += [f"- [ ] **{finding['code']}** ({finding['count']}): {finding['action']}"]
        for sample in finding['samples']:
            lines.append(f"  - `{_inline(sample.get('file', '?'))}:{sample.get('line', '?')}`")
    return '\n'.join(lines) + '\n'


def run_inspect(args) -> int:
    variables = {}
    for item in args.path_var or []:
        name, separator, value = item.partition('=')
        if not separator or not name or not value:
            raise ValueError('--path-var must be NAME=PATH')
        variables[name] = value
    output = args.output.resolve()
    root = args.root.resolve()
    corpus_root = root.parent if root.is_file() else root
    if output == corpus_root:
        raise ValueError('inspect output must differ from the IFP root')

    def progress(index, total, path):
        if not args.quiet and (index == 1 or index == total or index % 100 == 0):
            print(f"Inspecting {index}/{total}: {path}")

    data, draft = inspect_project(
        root, rules_config=args.rules_config, path_variables=variables,
        max_files=args.max_files, max_samples=args.max_samples, progress=progress)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'compatibility.json').write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / 'compatibility.md').write_text(compatibility_markdown(data), encoding='utf-8')
    (output / 'rules-config.draft.json').write_text(
        json.dumps(draft, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / 'adaptation-todo.md').write_text(todo_markdown(data), encoding='utf-8')
    print(f"Compatibility bundle: {output}; files={data['coverage'].get('files_scanned', 0)}; "
          f"rules={data['coverage'].get('rules', 0)}; findings={sum(item['count'] for item in data['findings'])}")
    failed_discovery = any(item['code'] == 'FILE_DISCOVERY_ERROR' for item in data['findings'])
    return 1 if data['coverage'].get('files_failed', 0) or failed_discovery else 0
