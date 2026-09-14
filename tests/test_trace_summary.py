from ifp_contract.trace_report import logic_summary


def test_long_chain_omits_a_gap_without_mislabeling_edges():
    from ifp_contract.trace_report import _format_chain

    path = [f'e{i}' for i in range(40)]
    events = {eid: {'id': eid, 'name': eid, 'node': eid, 'guards': []} for eid in path}
    nodes = {eid: {'file': 'A.ifp', 'line': i + 1} for i, eid in enumerate(path)}
    edges = [{'field': {'path': f'Field{i}'}} for i in range(39)]
    text = _format_chain(path, edges, events, nodes)
    assert '省略中间 8 个依赖事件' in text
    assert '`Field14`' in text and '`Field24`' in text
    assert '`Field15`' not in text and '`Field23`' not in text
    assert 'e39' in text


def bundle():
    events = [
        {'id': 'target', 'name': '页面输出', 'kind': 'output_mapping', 'node': 'page',
         'writes': [], 'reads': [], 'guards': [], 'api': None},
        {'id': 'copy', 'name': '复制币种', 'kind': 'set', 'node': 'copy',
         'writes': [], 'reads': [], 'guards': [], 'api': None},
        {'id': 'api', 'name': '国家规则', 'kind': 'api', 'node': 'api',
         'writes': [], 'reads': [], 'guards': [],
         'api': {'method': 'GET', 'path': '/countryRules'}},
    ]
    return {
        'target': {'field': 'Payment.Currency', 'entry': 'Input'},
        'seeds': ['target'], 'events': events,
        'nodes': {key: {'file': f'{key}.ifp', 'line': number}
                  for key, number in [('page', 10), ('copy', 20), ('api', 30)]},
        'edges': [
            {'from': 'copy', 'to': 'target', 'field': {'path': 'Currency'},
             'relation': 'definition', 'conditional': False},
            {'from': 'api', 'to': 'copy', 'field': {'path': 'Currency'},
             'relation': 'definition', 'conditional': False},
        ],
        'inputs': [], 'issues': [],
    }


def test_summary_follows_data_edges_to_api_and_keeps_locations():
    summary = logic_summary(bundle())
    assert '目标的直接来源链' in summary
    assert 'GET /countryRules' in summary
    assert 'api.ifp:30' in summary
    assert 'copy.ifp:20' in summary
    assert '共 1 个 API 候选；其中 1 个沿目标数据依赖边可达' in summary


def test_summary_renders_machine_readable_conclusion_boundaries():
    data = bundle()
    data['conclusions'] = {
        'anchors': [{'seed': 'target', 'status': 'blocked', 'direct_source_api_events': ['api'],
                     'dependency_event_count': 3,
                     'blockers': [{'code': 'UNKNOWN_RULE_SEMANTICS', 'count': 1,
                                   'events': ['opaque']}]}]
    }
    summary = logic_summary(data)
    assert '## 结论可靠性' in summary
    assert '受阻：存在可改变结论的未知边界' in summary
    assert '`UNKNOWN_RULE_SEMANTICS`：1 项；代表：`opaque`。' in summary


def test_summary_bounds_unrelated_apis_and_diagnostics():
    data = bundle()
    for index in range(20):
        data['events'].append({
            'id': f'other-{index}', 'name': f'其他 {index}', 'kind': 'api',
            'node': 'api', 'writes': [], 'reads': [], 'guards': [],
            'api': {'method': 'GET', 'path': f'/other/{index}'},
        })
    data['issues'] = [{'code': f'ISSUE_{index}'} for index in range(20)]
    summary = logic_summary(data)
    assert '共 21 个 API 候选；其中 1 个沿目标数据依赖边可达' in summary
    assert '另有 20 个 API 候选未列出，见 report.md' in summary
    assert '诊断共 20 条，按 code 汇总' in summary
    assert '`ISSUE_0`：1 条' in summary
    assert '`ISSUE_9`：' not in summary


def test_source_search_is_linear_on_diamond_and_cycle():
    data = bundle()
    # The two branches merge before the API and then point back to the target,
    # exercising both repeated frontiers and a cycle.
    data['events'].extend([
        {'id': 'left', 'name': '左支', 'kind': 'set', 'node': 'copy',
         'writes': [], 'reads': [], 'guards': [], 'api': None},
        {'id': 'right', 'name': '右支', 'kind': 'set', 'node': 'copy',
         'writes': [], 'reads': [], 'guards': [], 'api': None},
    ])
    data['edges'] = [
        {'from': 'left', 'to': 'target', 'field': {'path': 'Currency'}},
        {'from': 'right', 'to': 'target', 'field': {'path': 'Currency'}},
        {'from': 'api', 'to': 'left', 'field': {'path': 'Currency'}},
        {'from': 'api', 'to': 'right', 'field': {'path': 'Currency'}},
        {'from': 'target', 'to': 'api', 'field': {'path': 'Currency'}},
    ]
    summary = logic_summary(data)
    assert summary.count('GET /countryRules') == 2  # overview plus representative chain


def test_reachable_api_count_is_event_count_and_contexts_are_retained():
    data = bundle()
    data['events'].append({
        'id': 'api-context-2', 'name': '国家规则第二上下文', 'kind': 'api', 'node': 'api',
        'writes': [], 'reads': [], 'guards': [],
        'api': {'method': 'GET', 'path': '/countryRules'},
    })
    data['edges'].append({'from': 'api-context-2', 'to': 'copy',
                          'field': {'path': 'Currency'}})
    summary = logic_summary(data)
    assert '共 2 个 API 候选；其中 2 个沿目标数据依赖边可达' in summary
    assert '`api`' in summary and '`api-context-2`' in summary


def test_api_request_dependencies_are_not_direct_value_sources():
    data = bundle()
    data['events'].append({
        'id': 'request-input', 'name': '请求参数', 'kind': 'set', 'node': 'copy',
        'writes': [], 'reads': [], 'guards': [], 'api': None,
    })
    data['edges'].append({'from': 'request-input', 'to': 'api',
                          'field': {'path': 'Request.Country'}})
    summary = logic_summary(data)
    assert '共 1 个 API 候选；其中 1 个沿目标数据依赖边可达' in summary
    assert '请求参数（copy.ifp' not in summary
