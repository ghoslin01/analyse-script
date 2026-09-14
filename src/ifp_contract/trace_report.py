"""Portable trace bundles for disconnected analysis and repeatable collection."""
from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from pathlib import Path
from time import perf_counter

from .trace import Trace
from .trace_source import TraceSource
from .templates import template_markdown


def _event_location(event: dict, nodes: dict) -> str:
    node = nodes.get(event.get('node'), {})
    return f"{node.get('file', '?')}:{node.get('line', '?')}"


def _source_chains(data: dict, limit: int = 12):
    """Return bounded, edge-backed paths from anchors to source API events.

    The edge index is built once.  Event ids are context-specific, so paths that
    reach the same API through different contexts remain separate.
    """
    events = {e['id']: e for e in data.get('events', [])}
    incoming = defaultdict(list)
    for edge in data.get('edges', []):
        if edge.get('dependency_role') != 'control':
            incoming[edge['to']].append(edge)
    roots = [seed for seed in data.get('seeds', []) if seed in events]
    displays = [seed for seed in roots if events[seed].get('display_field')]
    if displays:
        # A writer may itself be an anchor. Do not let that shorter starting
        # point hide the display -> writer -> API path. Keep other anchors
        # when they are not reachable from any displayed value.
        covered = set(displays)
        pending = deque(displays)
        while pending:
            current = pending.popleft()
            if events[current].get('api'):
                continue
            for edge in incoming.get(current, ()):
                source = edge['from']
                if source in events and source not in covered:
                    covered.add(source)
                    pending.append(source)
        roots = displays + [seed for seed in roots if seed not in covered]
    queue = deque(roots)
    visited = set(queue)
    predecessor = {}
    while queue:
        current = queue.popleft()
        # An API is the boundary of a source chain.  Its request construction
        # dependencies describe the call, not additional direct value sources.
        if events[current].get('api'):
            continue
        for edge in incoming.get(current, ()):
            source = edge['from']
            if source not in events or source in visited:
                continue
            visited.add(source)
            # The edge points from source to the already visited event.  A
            # single predecessor is enough to reconstruct one representative
            # path; reachability itself remains event based and linear.
            predecessor[source] = (current, edge)
            queue.append(source)

    # Preserve evidence/event order, including fixtures without ``sequence``.
    reachable_apis = [e['id'] for e in data.get('events', [])
                       if e['id'] in visited and e.get('api')]
    results = []
    for api_id in reachable_apis[:limit]:
        path = [api_id]
        edges = []
        while path[-1] in predecessor:
            target, edge = predecessor[path[-1]]
            edges.append(edge)
            path.append(target)
        path.reverse()
        edges.reverse()
        results.append((path, edges))
    return results, reachable_apis, sum(1 for e in events.values() if e.get('api'))


def _format_chain(path, edges, events, nodes, conditions=None, depth_limit: int = 32):
    conditions = conditions or {}
    positions = list(range(len(path)))
    omitted = 0
    if len(path) > depth_limit:
        left = depth_limit // 2
        right = depth_limit - left
        positions = positions[:left] + positions[-right:]
        omitted = len(path) - len(positions)
    parts = []
    previous = None
    for index in positions:
        if previous is not None and index != previous + 1:
            parts.append(f"…省略中间 {omitted} 个依赖事件…")
        elif previous is not None:
            parts.append(f"— `{edges[previous]['field']['path']}` →")
        eid = path[index]
        event = events[eid]
        label = event.get('name') or eid
        if event.get('display_field'):
            label = nodes.get(event.get('node'), {}).get('attributes', {}).get('QuestionText') or label
        if event.get('api'):
            api = event['api']
            label = f"{api.get('method', '?')} {api.get('path', '?')}"
        guards = []
        guards_all = event.get('guards', ())
        for guard in guards_all[:6]:
            condition = conditions.get(guard['event'])
            result = f"={condition['result']}" if condition else ''
            guards.append(f"{guard['event']}:{guard['branch']}{result}")
        if guards:
            label += f" [前提 {'、'.join(guards)}]"
            if len(guards_all) > 6:
                label += f"（另有 {len(guards_all) - 6} 个前提见 report.md）"
        parts.append(f"{label}（{_event_location(event, nodes)}，`{eid}`）")
        previous = index
    if omitted:
        parts.append(f'（代表路径超过 {depth_limit} 层；完整链路见 report.md）')
    return ' '.join(parts)


def logic_summary(data: dict) -> str:
    """A bounded reading guide; every claim references collected events."""
    events = {e['id']: e for e in data['events']}
    nodes = data.get('nodes', {})
    target = data.get('target', {})
    lines = ['# 逻辑摘要', '',
             f"分析起点：`{target.get('field') or target.get('rule_eid')}`；入口：`{target.get('entry') or '各入口分别分析'}`。", '',
             '以下是配置证据及场景推导。候选路径仍有执行前提，不表示实际执行或最终运行值。', '']
    scenario = data.get('scenario')
    conclusions = data.get('conclusions', {})
    assessments = {item['seed']: item for item in conclusions.get('anchors', [])}
    if assessments:
        lines += ['## 结论可靠性', '',
                  '此项只评估静态证据是否足以支撑锚点结论；所有状态均不等同于运行验证。', '']
        labels = {'blocked': '受阻：存在可改变结论的未知边界',
                  'conditional': '有条件：证据存在执行前提',
                  'static_candidate': '静态候选：未发现本次收集范围内的阻断项',
                  'not_assessable': '不可评估：未形成锚点'}
        for item in conclusions['anchors'][:20]:
            text = labels.get(item['status'], item['status'])
            lines.append(f"- `{item['seed']}`：{text}；直接 API 来源 {len(item['direct_source_api_events'])} 个，"
                         f"依赖事件 {item['dependency_event_count']} 个。")
            for blocker in item['blockers']:
                suffix = '、'.join(f"`{event}`" for event in blocker['events'])
                lines.append(f"  - `{blocker['code']}`：{blocker['count']} 项；代表：{suffix}。")
        if len(conclusions['anchors']) > 20:
            lines.append(f"- 另有 {len(conclusions['anchors']) - 20} 个锚点，见 evidence.json。")
        lines.append('')
    if scenario:
        lines += ['## 场景假设', '', f"触发控件 eid：`{scenario['specification'].get('trigger_eid', '阶段入口')}`。", '']
        for assumption in scenario['specification'].get('assumptions', []):
            lines.append(f"- `{assumption['field']}` 初始为 `{assumption['value']}`；后续写入可能使该假设失效。")
    lines += ['', '## 目标的直接来源链', '',
              '以下链路由 evidence.json 的数据依赖边连接；不同调用上下文分别保留。', '']
    chains, reachable_api_ids, total_api_count = _source_chains(data)
    scenario_data = data.get('scenario') or {}
    chain_conditions = {c['event']: c for c in scenario_data.get('conditions', [])}
    for path, path_edges in chains:
        lines.append('- ' + _format_chain(path, path_edges, events, nodes, chain_conditions))
    if len(reachable_api_ids) > 12:
        lines.append(f'- 已展示 {len(chains)} 条来源链，其余见 report.md。')
    if not chains:
        lines.append('未从锚点沿数据依赖边找到 API 来源；请结合下方赋值证据和 report.md 检查。'
                     '这不排除尚未解析的上游值间接来自 API。')
    lines += ['', '## 目标的显示节点、写入路径或规则', '']
    labels = {'excluded': '此场景下排除', 'candidate': '有前提的候选路径', 'other_event_context': '其他事件的证据，先后未确定'}
    conditions = {c['event']: c for c in scenario['conditions']} if scenario else {}
    for eid in data['seeds'][:20]:
        e = events[eid]
        node = data['nodes'][e['node']]
        state = labels.get(e.get('scenario', {}).get('status'), '候选路径')
        lines.append(f"- **{e['name']}（{state}）**：`{node['file']}:{node['line']}`，事件 `{eid}`。")
        if e.get('display_field'):
            label = node['attributes'].get('QuestionText') or e['name']
            lines.append(f"  - 只读显示：{label}，字段 `{e['display_field']['path']}`；源 eid `{node.get('eid')}`。")
        if e.get('ui_condition'):
            condition = e['ui_condition']
            lines.append(f"  - 显示条件配置：`{condition['expression']}`；NotApplicable=`{condition.get('not_applicable')}`。"
                         '保留原始配置，不推断显示/隐藏结果。')
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
    lines += ['', '## 接口候选概览', '',
              f'共 {total_api_count} 个 API 候选；其中 {len(set(reachable_api_ids))} 个沿目标数据依赖边可达，'
              '可达只表示配置证据关联，不代表实际调用。', '']
    reachable = set(reachable_api_ids[:12])
    for e in data['events']:
        if e.get('api') and e['id'] in reachable:
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
    omitted_apis = total_api_count - len(reachable)
    if omitted_apis:
        lines.append(f'- 另有 {omitted_apis} 个 API 候选未列出，见 report.md。')
    lines += ['', '## 尚未确定', '',
              '源值的生成条件与目标赋值的执行条件需要分别查看；未命中本次赋值时，仍可能读取已有值。', '']
    unknown = [c for c in conditions.values() if c['result'] == 'unknown']
    unknown.sort(key=lambda c: events[c['event']].get('scenario', {}).get('status') == 'other_event_context')
    for c in unknown[:8]:
        context = labels.get(events[c['event']].get('scenario', {}).get('status'), '候选路径')
        lines.append(f"- `{c['event']}`（{context}）条件尚不能确定：`{c['expression']}`。原因：`{c['reason']}`；具体字段和推导依据见 report.md。")
    if len(unknown) > 8:
        lines.append(f"- 另有 {len(unknown) - 8} 个未知条件，见 report.md。")
    issue_groups = defaultdict(list)
    for issue in data['issues']:
        issue_groups[issue.get('code', 'UNKNOWN')].append(issue)
    if issue_groups:
        lines.append(f'诊断共 {len(data["issues"])} 条，按 code 汇总（代表事件最多展示 3 个）：')
        codes = sorted(issue_groups)
        for code in codes[:12]:
            group = issue_groups[code]
            representatives = []
            for issue in group[:3]:
                event_id = issue.get('event')
                representatives.append(event_id or json.dumps(issue, ensure_ascii=False))
            suffix = '；' + '、'.join(f'`{item}`' for item in representatives)
            lines.append(f'- `{code}`：{len(group)} 条，代表：{suffix.lstrip("；")}。')
        if len(codes) > 12:
            lines.append(f'- 另有 {len(codes) - 12} 个诊断 code 未展开，见 report.md。')
    lines += ['', f"保留 {len(data['inputs'])} 条外部输入或条件旧值边界。场景标签不删除原始证据，也不消除其他事件的先后不确定性。", '',
              '完整条件、读写依赖和源文件位置见 report.md；机器可读证据见 evidence.json。', '']
    return '\n'.join(lines)


def markdown(data: dict) -> str:
    nodes = data['nodes']
    all_events = data['events']
    events = {e['id']: e for e in all_events}
    has_slice_roles = any('slice_role' in event for event in all_events)
    api_events = [event for event in all_events if event.get('api')]
    incoming_edges = defaultdict(list)
    for edge in data['edges']:
        incoming_edges[edge['to']].append(edge)
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
        if ev.get('display_field'):
            label = node['attributes'].get('QuestionText') or ev['name']
            lines.append(f"  - Read-only display: {label}; source eid `{node.get('eid')}`; no field write.")
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
    for ev in api_events:
        lines.append(f"- {ev['id']}: `{ev['api']['method']} {ev['api']['path']}` (conditional candidate, not a runtime call log)")
    lines += ['', '## Trace', '']
    context_events = []
    guard_catalog = set()
    for ev in all_events:
        node = nodes[ev['node']]
        if has_slice_roles:
            for guard in ev.get('guards', ()):
                parent = events.get(guard.get('event'), {})
                parent_node = nodes.get(parent.get('node'), {})
                attributes = parent.get('semantics', {}).get('attributes', parent_node.get('attributes', {}))
                expression = guard.get('expression') or attributes.get('Expression') or parent.get('name', guard.get('event', ''))
                kind = guard.get('kind') or parent.get('kind', 'unknown')
                guard_catalog.add((guard.get('event', ''), str(guard.get('branch', '')), expression, kind))
        if has_slice_roles and ev.get('slice_role') == 'context_or_boundary':
            context_events.append(ev)
            continue
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
            lines += ['Activation context: ' + ', '.join(ev['triggers']) + '. Order between separate activations is unknown.', '']
        if ev.get('ui_condition'):
            lines += ['UI condition configuration (activation semantics not inferred):', '',
                      '```text', ev['ui_condition']['expression'], '```',
                      f"NotApplicable: `{ev['ui_condition'].get('not_applicable')}`; visibility is not evaluated.", '']
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
            if has_slice_roles:
                refs = ', '.join(f"{g['event']}:{g['branch']}" for g in ev['guards'])
                lines.append(f'- Guard references: `{refs}`')
            else:
                for g in ev['guards']:
                    parent = events.get(g['event'], {})
                    label = g.get('expression') or parent.get('name', g['event'])
                    lines.append(f"- {g['event']}: `{g['branch']}` branch of {g['kind']} `{label}`")
            lines.append('')
        if ev['loops']:
            lines += ['Symbolic loops: ' + ', '.join(ev['loops']), '']
        incoming = incoming_edges.get(ev['id'], ())
        if incoming:
            lines += ['Source evidence:', '']
            for edge in incoming:
                suffix = '; conditional write — prior value may remain' if edge['conditional'] else ''
                if edge.get('event_order') == 'unknown_between_ui_events':
                    suffix += '; order between UI events unknown'
                elif edge.get('event_order') == 'unknown_product_scheduling':
                    suffix += '; product rule scheduling relative to phase is unknown'
                if edge.get('dependency_role') == 'control':
                    suffix += '; UI condition dependency, not display value source'
                lines.append(f"- {edge['from']} → {edge['to']}: `{edge['field']['path']}` ({edge['relation']}{suffix})")
            lines.append('')
    if context_events:
        lines += ['## Context and boundary index', '',
                  'These events are retained for location or possible unknown influence. '
                  'Their inputs were not recursively traced; full attributes and guards remain in evidence.json.', '']
        for ev in context_events:
            node = nodes[ev['node']]
            name = f" · {ev['name']}" if ev.get('name') else ''
            lines.append(f"- `{ev['id']}` · `{ev.get('kind', 'unknown')}` · `{node.get('file', '?')}:{node.get('line', '?')}`{name}")
    if guard_catalog:
        lines += ['', '## Guard catalog', '',
                  'Each `(event, branch, expression, kind)` tuple is listed once. '
                  'Guards of context-only events are retained configuration, not proven execution.', '']
        for event_id, branch, expression, kind in sorted(guard_catalog):
            lines.append(f"- `{event_id}:{branch}` · `{kind}` · `{expression}`")
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


def _eid_target(root: Path, eid: str, file: Path | None, entry: str | None,
                cache_dir: Path | None) -> dict:
    from .trace_locator import candidate_files

    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f'Trace root is not a directory: {root}')
    if not eid.strip():
        raise ValueError('--eid must not be empty')
    candidates = [root / str(file).replace('\\', '/')] if file else candidate_files(root, eid, cache_dir)
    matches = []
    for path in candidates:
        path = path.resolve()
        if not path.is_relative_to(root):
            raise ValueError(f'Starting IFP is outside ROOT: {path}')
        source = TraceSource(root, path, cache_dir=cache_dir)
        allowed = source.entry_roots(entry) if entry else None
        for node in source.eids.get(eid, []):
            ancestors = []
            current = node
            while current is not None:
                ancestors.append(current)
                current = current.parent
            if allowed is not None and not any(a is r for a in ancestors for r in allowed):
                continue
            if node.tag not in {'Question', 'Button', 'Rule'}:
                raise ValueError(f'--eid selects a {node.tag}; expected a Question, Button or Rule')
            phase = next((a for a in ancestors if a.tag == 'Phase'), None)
            resolved_entry = entry
            if resolved_entry is None:
                if phase is not None:
                    resolved_entry = ('@' + phase.meta['eid'] if phase.meta.get('eid')
                                      else source.phase_name(phase))
                else:
                    # Keep enclosing branch rules when a shared rule is selected.
                    outer_rule = next((a for a in reversed(ancestors) if a.tag == 'Rule'), node)
                    if outer_rule.meta.get('eid'):
                        resolved_entry = '@' + outer_rule.meta['eid']
                    else:
                        raise ValueError(f'Cannot infer an entry for eid {eid!r} in {source.relative}; supply --entry')
            matches.append((source.relative, node.line, resolved_entry))
        source.save_cache()
    if not matches:
        raise ValueError(f'EID_NOT_FOUND: {eid!r} in the selected files/entry')
    if len(matches) != 1:
        locations = ', '.join(f'{name}:{line}' for name, line, _ in matches[:20])
        raise ValueError(f'EID_NOT_UNIQUE: {eid!r} matches {len(matches)} nodes: {locations}; '
                         'use --file (and --entry if needed) to select one')
    name, _, resolved_entry = matches[0]
    return {'file': name, 'field': None, 'rule_eid': eid, 'entry': resolved_entry}


def run_trace(args) -> int:
    started = perf_counter()
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
    cache_dir = None if getattr(args, 'no_cache', False) else (
        getattr(args, 'cache_dir', None) or
        Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'ifp-contract' / 'trace')
    discovery_seconds = 0.0
    if getattr(args, 'eid', None) is not None:
        locating = perf_counter()
        targets = [_eid_target(args.root, args.eid, args.file, args.entry, cache_dir)]
        discovery_seconds = perf_counter() - locating
    elif args.file:
        targets = [{'file': str(args.file), 'field': args.field, 'rule_eid': args.rule_eid, 'entry': args.entry}]
    if (not isinstance(targets, list) or not targets or
        any(not isinstance(t, dict) or not isinstance(t.get('file'), str) or not t['file'] or
            bool(t.get('field')) == bool(t.get('rule_eid')) or
            any(t.get(k) is not None and not isinstance(t[k], str) for k in ('field', 'rule_eid', 'entry')) for t in targets)):
        raise ValueError('Supply --eid, --file with one of --field/--rule-eid, or a request with valid targets')
    max_files = args.max_files if args.max_files is not None else request.get('max_files', 50)
    max_contexts = args.max_contexts if args.max_contexts is not None else request.get('max_contexts', 5000)
    bundles = []
    measurements = []
    failed = False
    # Each target gets independent contexts; never merge branches from separate anchors.
    for target in targets:
        trace = Trace(args.root, rules_config=config_path, path_variables=variables,
                      max_files=max_files, max_contexts=max_contexts, cache_dir=cache_dir)
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
        measurements.append({'target': target, **trace.performance})
        failed |= trace.failed
    if args.output is None:
        base = Path.cwd() / 'trace-output'
        suffix = 1
        while True:
            output = base if suffix == 1 else base.with_name(f'{base.name}-{suffix}')
            try:
                output.mkdir()
                args.output = output
                break
            except FileExistsError:
                suffix += 1
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
    exporting = perf_counter()
    for name, payload in [('evidence.json', evidence), ('next.json', followup)]:
        (args.output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.output / 'report.md').write_text('\n\n---\n\n'.join(markdown(b) for b in bundles), encoding='utf-8')
    (args.output / 'summary.md').write_text('\n\n---\n\n'.join(logic_summary(b) for b in bundles), encoding='utf-8')
    performance = {'schema_version': 1, 'targets': measurements,
                   'discovery_seconds': discovery_seconds,
                   'export_seconds': perf_counter() - exporting,
                   'total_seconds': perf_counter() - started,
                   'cache_enabled': cache_dir is not None}
    (args.output / 'performance.json').write_text(
        json.dumps(performance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"Trace bundle: {args.output}; files={sum(b['coverage']['files_read'] for b in bundles)}; "
          f"contexts={sum(len(b['events']) for b in bundles)}; "
          f"elapsed={performance['total_seconds']:.2f}s; "
          f"cache_hits={sum(m['cache_hits'] for m in measurements)}")
    if failed:
        return 1
    return 2 if args.strict and any(b['issues'] or b['inputs'] for b in bundles) else 0
