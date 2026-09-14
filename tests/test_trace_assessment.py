from ifp_contract.trace_assessment import assess_conclusions


def event(identifier, **extra):
    return {'id': identifier, 'kind': 'set', 'parents': [], 'guards': [], 'loops': [], **extra}


def test_unknown_rule_is_a_blocker_only_when_it_can_affect_the_closure():
    events = [event('target'), event('copy'), event('opaque', kind='unknown'), event('unrelated', kind='unknown')]
    result = assess_conclusions(
        events,
        [{'from': 'copy', 'to': 'target'}], [], [], ['target'],
        {'limited': False, 'partial_scope_only': False},
        [{'from': 'opaque', 'to': 'copy', 'relation': 'opaque_rule_may_affect'}],
    )
    anchor = result['anchors'][0]
    assert anchor['status'] == 'blocked'
    assert anchor['blockers'] == [{'code': 'OPAQUE_RULE_MAY_AFFECT', 'count': 1, 'events': ['opaque']}]


def test_conditional_path_is_not_claimed_as_a_static_candidate():
    result = assess_conclusions(
        [event('target'), event('copy')],
        [{'from': 'copy', 'to': 'target', 'conditional': True}], [], [], ['target'],
        {'limited': False, 'partial_scope_only': False}, [],
    )
    anchor = result['anchors'][0]
    assert anchor['status'] == 'conditional'
    assert anchor['conditional_path'] is True


def test_api_request_inputs_do_not_block_response_source_conclusion():
    result = assess_conclusions(
        [event('target'), event('api', api={'path': '/rules'}), event('request')],
        [{'from': 'api', 'to': 'target'}, {'from': 'request', 'to': 'api'}],
        [{'event': 'request', 'field': {'path': 'Country'}, 'status': 'external_or_unresolved'}],
        [], ['target'], {'limited': False, 'partial_scope_only': False}, [],
    )
    anchor = result['anchors'][0]
    assert anchor['status'] == 'static_candidate'
    assert anchor['direct_source_api_events'] == ['api']


def test_control_api_is_not_presented_as_a_direct_value_source():
    result = assess_conclusions(
        [event('target', parents=['control']), event('copy'),
         event('value-api', api={'path': '/value'}), event('control'),
         event('control-api', api={'path': '/control'})],
        [{'from': 'copy', 'to': 'target'}, {'from': 'value-api', 'to': 'copy'},
         {'from': 'control-api', 'to': 'control'}], [], [], ['target'],
        {'limited': False, 'partial_scope_only': False}, [],
    )
    assert result['anchors'][0]['direct_source_api_events'] == ['value-api']


def test_missing_writer_blocks_exact_value_conclusion():
    result = assess_conclusions(
        [event('target')], [],
        [{'event': 'target', 'field': {'path': 'Currency'}, 'status': 'external_or_unresolved'}],
        [], ['target'], {'limited': False, 'partial_scope_only': False}, [],
    )
    assert result['anchors'][0]['status'] == 'blocked'
    assert result['anchors'][0]['blockers'][0]['code'] == 'UNRESOLVED_VALUE'


def test_missing_anchor_is_explicitly_not_assessable():
    result = assess_conclusions([], [], [], [], [],
                                {'limited': False, 'partial_scope_only': False}, [])
    assert result['overall_status'] == 'not_assessable'


def test_blocker_examples_follow_event_order_deterministically():
    arguments = (
        [event('target'), event('late', kind='unknown'), event('early', kind='unknown')],
        [], [], [], ['target'], {'limited': False, 'partial_scope_only': False},
        [{'from': 'late', 'to': 'target'}, {'from': 'early', 'to': 'target'}],
    )
    first = assess_conclusions(*arguments)
    second = assess_conclusions(*arguments)
    assert first == second
    assert first['anchors'][0]['blockers'][0]['events'] == ['late', 'early']
