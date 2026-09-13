"""Portable trace bundles for disconnected analysis and repeatable collection."""
from __future__ import annotations

import json
from pathlib import Path

from .trace import Trace
from .templates import template_markdown


def logic_summary(data: dict) -> str:
    """A bounded reading guide; every claim references collected events."""
    events = {e['id']: e for e in data['events']}
    target = data.get('target', {})
    lines = ['# 逻辑摘要', '',
             f"分析起点：`{target.get('field') or target.get('rule_eid')}`；入口：`{target.get('entry') or '各入口分别分析'}`。", '',
             '以下是配置证据及场景推导。候选路径仍有执行前提，不表示实际执行或最终运行值。', '']
    scenario = data.get('scenario')
    if scenario:
        lines += ['## 场景假设', '', f"触发控件 eid：`{scenario['specification'].get('trigger_eid', '阶段入口')}`。", '']
        for assumption in scenario['specification'].get('assumptions', []):
            lines.append(f"- `{assumption['field']}` 初始为 `{assumption['value']}`；后续写入可能使该假设失效。")
    lines += ['', '## 目标的写入路径或规则', '']
    labels = {'excluded': '此场景下排除', 'candidate': '有前提的候选路径', 'other_event_context': '其他事件的证据，先后未确定'}
    conditions = {c['event']: c for c in scenario['conditions']} if scenario else {}
    for eid in data['seeds'][:20]:
        e = events[eid]
        node = data['nodes'][e['node']]
        state = labels.get(e.get('scenario', {}).get('status'), '候选路径')
        lines.append(f"- **{e['name']}（{state}）**：`{node['file']}:{node['line']}`，事件 `{eid}`。")
        if e['writes']:
            lines.append('  - 写入：' + '、'.join(f"`{r['path']}`" for r in e['writes']) + '。')
        if e['reads']:
            lines.append('  - 读取：' + '、'.join(f"`{r['path']}`" for r in e['reads']) + '。')
        a = e.get('semantics', {}).get('attributes', {})
        if e['kind'] == 'set' and e.get('assignment_modes', {}).get('source') == 'Value':
            lines.append(f"  - 配置值：`{a.get('FromValue', '未提供')}`。")
        if e['kind'] == 'expression':
            lines.append(f"  - 转换表达式：`{a.get('Expression', '')}`。")
        if e['kind'] == 'output_mapping':
            lines.append('  - 这是组件调用后的输出映射，可能覆盖调用前传入的值。')
        for g in e['guards']:
            condition = conditions.get(g['event'])
            result = f"；场景比较结果 `{condition['result']}`" if condition else ''
            lines.append(f"  - 执行前提：`{g['event']}` 的 `{g['branch']}` 分支，`{g.get('expression') or '父规则返回结果'}`{result}。")
    if len(data['seeds']) > 20:
        lines.append(f"\n共 {len(data['seeds'])} 个锚点事件，此处展示前 20 个，其余见 report.md。")
    if not data['seeds']:
        lines.append('未到达锚点；需要先检查采集范围和诊断。')
    lines += ['', '## 接口候选', '']
    for e in data['events']:
        if e.get('api'):
            state = labels.get(e.get('scenario', {}).get('status'), '候选路径')
            path_label = '动态路径模板' if '$%' in (e['api']['path'] or '') else e['api']['path']
            lines.append(f"- `{e['api']['method']} {path_label}`：{state}（`{e['id']}`）。")
            if '$%' in (e['api']['path'] or ''):
                lines.append(f"  - 服务根地址：`{e['api'].get('base_url') or '未确定'}`。")
            if e['api'].get('manual_payload'):
                lines.append('  - 使用手工请求体；字段和分支证据见 report.md。')
            if e['api'].get('context'):
                lines.append(f"  - 上下文数据组：`{e['api']['context']}`；其读写方向未由配置声明。")
            for concept, template in e['api'].get('templates', {}).items():
                if '$%' in template['raw']:
                    if template['status'] != 'parsed':
                        lines.append(f"  - `{concept}` 模板边界：`{template.get('reason')}`。")
                    for variant in template['variants']:
                        path = variant['template'].replace('\n', r'\n').replace('\r', r'\r')
                        guards = '；'.join(f"({g['condition']}) 为 {str(g['branch']).lower()}" for g in variant['guards'])
                        lines.append(f"  - 路径候选：`{path}`；前提：{guards}。")
                    if concept == 'query':
                        fields = list(dict.fromkeys(d['field'] for d in template['dependencies']))
                        lines.append('  - 查询模板依赖：' + '、'.join(f'`{p}`' for p in fields) + '；嵌套条件与拼接片段见 report.md。')
    lines += ['', '## 尚未确定', '',
              '源值的生成条件与目标赋值的执行条件需要分别查看；未命中本次赋值时，仍可能读取已有值。', '']
    unknown = [c for c in conditions.values() if c['result'] == 'unknown']
    unknown.sort(key=lambda c: events[c['event']].get('scenario', {}).get('status') == 'other_event_context')
    for c in unknown[:8]:
        context = labels.get(events[c['event']].get('scenario', {}).get('status'), '候选路径')
        lines.append(f"- `{c['event']}`（{context}）条件尚不能确定：`{c['expression']}`。原因：`{c['reason']}`；具体字段和推导依据见 report.md。")
    if len(unknown) > 8:
        lines.append(f"- 另有 {len(unknown) - 8} 个未知条件，见 report.md。")
    for issue in data['issues']:
        lines.append('- ' + json.dumps(issue, ensure_ascii=False))
    lines += ['', f"保留 {len(data['inputs'])} 条外部输入或条件旧值边界。场景标签不删除原始证据，也不消除其他事件的先后不确定性。", '',
              '完整条件、读写依赖和源文件位置见 report.md；机器可读证据见 evidence.json。', '']
    return '\n'.join(lines)


def markdown(data: dict) -> str:
    nodes = data['nodes']
    events = {e['id']: e for e in data['events']}
    lines = ['# IFP trace evidence', '',
             'Static configuration evidence; branches and iterations are symbolic. '
             'This report does not prove which path ran or the final runtime value.', '',
             f"Files read: {data['coverage']['files_read']}; contexts exported: {len(events)}; "
             f"limited: {data['coverage']['limited']}.", '', '## Anchor evidence', '']
    for eid in data['seeds']:
        ev = events[eid]
        node = nodes[ev['node']]
        lines.append(f"- **{eid}: {ev['name']}** ({ev['kind']}, {node['file']}:{node['line']})")
        for read in ev['reads']:
            lines.append(f"  - Reads `{read['path']}`")
        for written in ev['writes']:
            lines.append(f"  - Writes `{written['path']}`")
        for guard in ev['guards']:
            if guard['kind'] == 'condition':
                lines.append(f"  - {guard['branch']} branch: `{guard['expression']}`")
    if not data['seeds']:
        lines.append('Anchor not reached. Consult coverage and diagnostics; partial evidence is not a complete trace.')
    scenario = data.get('scenario')
    if scenario:
        lines += ['', '## Scenario reasoning', '',
                  'Assumptions describe initial values at the selected activation, not observed runtime facts. '
                  'Excluded paths remain in the evidence for review. Candidate does not mean guaranteed execution.', '',
                  '```json', json.dumps(scenario['specification'], ensure_ascii=False, indent=2), '```', '']
        for condition in scenario['conditions']:
            lines.append(f"- {condition['event']}: `{condition['expression']}` → **{condition['result']}** ({condition['reason']})")
            for fact in condition['facts']:
                lines.append(f"  - `{fact['field']}`: {json.dumps(fact['value'], ensure_ascii=False)} ({fact['status']}); basis: {json.dumps(fact['basis'], ensure_ascii=False)}")
        lines += ['', 'Anchor paths:', '']
        for eid in data['seeds']:
            ev = events[eid]
            lines.append(f"- {eid} {ev['name']}: **{ev['scenario']['status']}**; excluded by {ev['scenario']['excluded_by']}")
    lines += ['', '## API evidence', '']
    for ev in data['events']:
        if ev.get('api'):
            lines.append(f"- {ev['id']}: `{ev['api']['method']} {ev['api']['path']}` (conditional candidate, not a runtime call log)")
    lines += ['', '## Trace', '']
    for ev in data['events']:
        node = nodes[ev['node']]
        label = node['attributes'].get('QuestionText') or node['attributes'].get('ActionCommand') or ev['name']
        lines += [f"### {ev['id']} · {ev['kind']} · {label}", '',
                  f"Source: `{node['file']}:{node['line']}` · byte `{node['offset']}` · eid `{node['eid']}`",
                  f"Entry: `{ev['entry']}` · scope: `{ev['scope']}`", '']
        if ev.get('api'):
            lines += [f"API: `{ev['api']['method']} {ev['api']['path']}`", '']
            lines += [f"Rule scheduling configuration: `{node['attributes'].get('RuleType', '')}`", '',
                      f"Service root: `{ev['api'].get('base_url') or ''}`", '']
            if ev['api'].get('manual_payload'):
                lines += [f"Manual payload: `{ev['api']['manual_payload']}`", '']
            if ev['api'].get('context'):
                lines += [f"Context group (direction not declared): `{ev['api']['context']}`", '']
            for concept, template in ev['api'].get('templates', {}).items():
                if ('$%' in template['raw'] or concept in {'query', 'payload', 'header_name', 'header_value', 'language'}):
                    lines += template_markdown(concept, template)
        if ev.get('scenario'):
            lines += [f"Scenario: **{ev['scenario']['status']}**; excluded by {ev['scenario']['excluded_by']}", '']
        if ev.get('triggers'):
            lines += ['UI event context: ' + ', '.join(ev['triggers']) + '. Order between separate UI events is unknown.', '']
        if ev.get('ui_condition'):
            lines += ['UI condition configuration (activation semantics not inferred):', '',
                      '```text', ev['ui_condition']['expression'], '```', '']
        for key, label in [('reads', 'Reads'), ('writes', 'Writes')]:
            if ev[key]:
                lines.append(label + ': ' + ', '.join(f"`{r['path']}` ({r['scope']})" for r in ev[key]))
        if ev['reads'] or ev['writes']:
            lines.append('')
        expr = ev.get('semantics', {}).get('attributes', node['attributes']).get('Expression')
        if expr:
            lines += ['Expression:', '', '```text', expr, '```', '']
        if ev['guards']:
            lines += ['Execution prerequisites (retained separately from source-value prerequisites):', '']
            for g in ev['guards']:
                parent = events.get(g['event'], {})
                label = g.get('expression') or parent.get('name', g['event'])
                lines.append(f"- {g['event']}: `{g['branch']}` branch of {g['kind']} `{label}`")
            lines.append('')
        if ev['loops']:
            lines += ['Symbolic loops: ' + ', '.join(ev['loops']), '']
        incoming = [edge for edge in data['edges'] if edge['to'] == ev['id']]
        if incoming:
            lines += ['Source evidence:', '']
            for edge in incoming:
                suffix = '; conditional write — prior value may remain' if edge['conditional'] else ''
                if edge.get('event_order') == 'unknown_between_ui_events':
                    suffix += '; order between UI events unknown'
                lines.append(f"- {edge['from']} → {edge['to']}: `{edge['field']['path']}` ({edge['relation']}{suffix})")
            lines.append('')
    lines += ['## Boundaries and diagnostics', '',
              'External inputs and conditional writes below are not proof of a missing value or a defect.', '']
    for item in data['inputs']:
        lines.append(f"- {item['event']}: `{item['field']['path']}` — {item['status']}")
    for issue in data['issues']:
        lines.append('- ' + json.dumps(issue, ensure_ascii=False))
    if not data['inputs'] and not data['issues']:
        lines.append('No unresolved boundary found within the selected scope.')
    lines += ['', '## Continue', '',
              'Use `next.json` with `trace ROOT --request next.json --output NEW_DIR`. '
              'The request repeats the original anchors and includes pending evidence; '
              'increase limits for bounded frontiers or add explicit targets/path variables. '
              'Collection re-reads sources and reports changed fingerprints.', '']
    return '\n'.join(lines)


def run_trace(args) -> int:
    request = {}
    if args.request:
        request = json.loads(args.request.read_text(encoding='utf-8'))
        if not isinstance(request, dict) or request.get('schema_version') != 1:
            raise ValueError('Unsupported trace request format')
    config_path = args.rules_config
    if not config_path and request.get('rules_config'):
        config_path = Path(request['rules_config'])
        if not config_path.is_absolute():
            config_path = args.request.parent / config_path
    variables = dict(request.get('path_variables', {}))
    for item in args.path_var or []:
        name, separator, value = item.partition('=')
        if not separator or not name or not value:
            raise ValueError('--path-var must be NAME=PATH')
        variables[name] = value
    targets = request.get('targets', [])
    scenario = json.loads(args.scenario.read_text(encoding='utf-8')) if args.scenario else None
    if args.file:
        targets = [{'file': str(args.file), 'field': args.field, 'rule_eid': args.rule_eid, 'entry': args.entry}]
    if (not isinstance(targets, list) or not targets or
        any(not isinstance(t, dict) or not isinstance(t.get('file'), str) or not t['file'] or
            bool(t.get('field')) == bool(t.get('rule_eid')) or
            any(t.get(k) is not None and not isinstance(t[k], str) for k in ('field', 'rule_eid', 'entry')) for t in targets)):
        raise ValueError('Supply --file and exactly one of --field/--rule-eid, or a request with valid targets')
    max_files = args.max_files if args.max_files is not None else request.get('max_files', 50)
    max_contexts = args.max_contexts if args.max_contexts is not None else request.get('max_contexts', 5000)
    bundles = []
    failed = False
    # Each target gets independent contexts; never merge branches from separate anchors.
    for target in targets:
        trace = Trace(args.root, rules_config=config_path, path_variables=variables,
                      max_files=max_files, max_contexts=max_contexts)
        target = dict(target)
        if scenario is not None:
            target['scenario'] = scenario
        data = trace.collect(target['file'], target.get('field'), target.get('rule_eid'), target.get('entry'),
                             scenario=target.get('scenario'))
        data['target'] = target
        previous = request.get('file_fingerprints', {})
        for name, fingerprint in data['files'].items():
            if name in previous and previous[name] != fingerprint:
                data['issues'].append({'code': 'SOURCE_CHANGED_SINCE_REQUEST', 'file': name})
        bundles.append(data)
        failed |= trace.failed
    if args.root.resolve() == args.output.resolve():
        raise ValueError('Trace output must differ from the IFP root')
    args.output.mkdir(parents=True, exist_ok=True)
    evidence = bundles[0] if len(bundles) == 1 else {'schema_version': 1, 'traces': bundles}
    pending = [{'target': b['target'], 'issues': b['issues'], 'inputs': b['inputs']} for b in bundles]
    followup = {'schema_version': 1, 'targets': [b['target'] for b in bundles], 'path_variables': variables,
                'rules_config': 'rules-config.json' if config_path else None,
                'max_files': max_files, 'max_contexts': max_contexts,
                'file_fingerprints': {k: v for b in bundles for k, v in b['files'].items()},
                'pending': pending}
    if config_path:
        config_content = config_path.read_text(encoding='utf-8')
        (args.output / 'rules-config.json').write_text(config_content, encoding='utf-8')
    for name, payload in [('evidence.json', evidence), ('next.json', followup)]:
        (args.output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.output / 'report.md').write_text('\n\n---\n\n'.join(markdown(b) for b in bundles), encoding='utf-8')
    (args.output / 'summary.md').write_text('\n\n---\n\n'.join(logic_summary(b) for b in bundles), encoding='utf-8')
    print(f"Trace bundle: {args.output}; files={sum(b['coverage']['files_read'] for b in bundles)}; "
          f"contexts={sum(len(b['events']) for b in bundles)}")
    if failed:
        return 1
    return 2 if args.strict and any(b['issues'] or b['inputs'] for b in bundles) else 0
