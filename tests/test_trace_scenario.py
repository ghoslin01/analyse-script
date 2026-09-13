import json

import pytest

from ifp_contract.cli import main
from ifp_contract.trace import Trace
from ifp_contract.trace_config import TraceConfig
from ifp_contract.trace_scenario import UNKNOWN, evaluate, parse_condition


def put(root, name, text):
    path = root / name
    path.write_text(text, encoding='utf-8')
    return path


def screen(root, rules, before=''):
    put(root, 'A.ifp', f'<Project><Product Name="P"><Phase Name="I">{before}'
        f'<Button Name="Continue" eid="button">{rules}</Button></Phase></Product></Project>')


def gate(field='Mode', expected='Full', name='gate'):
    return f'''<Rule Name="{name}" RuleClassName="EvaluateRule" Expression="$${field}$ == '{expected}'">
      <Rule Name="yes" RuleClassName="SetValueRule" PropertyName="Amount" FromValue="100" RuleType="True"/>
      <Rule Name="no" RuleClassName="SetValueRule" PropertyName="Amount" FromValue="50" RuleType="False"/>
    </Rule>'''


def scenario(field='Mode', value='Full'):
    return {'trigger_eid': 'button', 'assumptions': [{'field': field, 'value': value}]}


def collect(root, spec=None, config=None):
    return Trace(root, rules_config=config).collect('A.ifp', 'Amount', None, 'P.I', scenario=spec or scenario())


def statuses(data):
    return {e['name']: e['scenario']['status'] for e in data['events']}


def test_compound_condition_preserves_and_or_and_unknowns():
    expression = "($$Protection$ == 'NonPrincipalProtected' OR $$Alternative$ == 'NonPrincipalProtected') AND ($$Tenor$ == 'UpTo6m' OR $$Tenor$ == 'UpTo1y')"
    tree = parse_condition(expression)
    assert tree[0] == 'and' and tree[1][0] == 'or'
    assert evaluate(tree, lambda p: {'Protection': 'NonPrincipalProtected', 'Tenor': 'UpTo6m'}.get(p, UNKNOWN)) is True
    assert evaluate(tree, lambda p: {'Tenor': 'UpTo2y'}.get(p, UNKNOWN)) is False
    assert evaluate(tree, lambda p: UNKNOWN) is UNKNOWN
    assert evaluate(parse_condition('NOT ("$$Mode$" != \'Full\')'), lambda p: 'Full') is True


@pytest.mark.parametrize('expression', ["$$X$ == 'a' + run()", "$$X$.value() == 'a'", "$$X$ > '1'", "$$X$ == 'a' trailing", "'$$X$ suffix' == 'x'"])
def test_unsupported_syntax_is_not_partially_evaluated(expression):
    with pytest.raises(ValueError):
        parse_condition(expression)


def test_scenario_retains_excluded_branch_and_original_evidence(tmp_path):
    screen(tmp_path, gate())
    data = collect(tmp_path)
    assert statuses(data)['yes'] == 'candidate'
    assert statuses(data)['no'] == 'excluded'
    assert len(data['seeds']) == 2
    assert data['scenario']['conditions'][0]['result'] is True
    assert data['scenario']['specification'] == scenario()
    assert not data['scenario']['runtime_verified']


@pytest.mark.parametrize('writer,expected', [
    ('<Rule RuleClassName="SetValueRule" PropertyName="Mode" FromValue="Partial"/>', False),
    ('<Rule RuleClassName="ResetDataRule" ResetProperty="Mode"/>', 'unknown'),
    ('<Rule RuleClassName="VendorOpaqueRule"/>', 'unknown'),
    ('<Rule RuleClassName="SwaggerIntegrationRule" Output="Mode"/>', 'unknown'),
    ('<Rule RuleClassName="EvaluateRule" Expression="$$Other$ == \'x\'"><Rule RuleClassName="SetValueRule" PropertyName="Mode" FromValue="Partial" RuleType="True"/></Rule>', 'unknown'),
    ('<Rule RuleClassName="RepeatRule" DataGroupName="Items[C]"><Rule RuleClassName="SetValueRule" PropertyName="Mode" FromValue="Partial"/></Rule>', 'unknown'),
])
def test_writes_and_unknown_effects_invalidate_initial_assumptions(tmp_path, writer, expected):
    screen(tmp_path, writer + gate())
    data = collect(tmp_path)
    condition = next(c for c in data['scenario']['conditions'] if c['expression'] == "$$Mode$ == 'Full'")
    assert condition['result'] == expected


def test_ui_events_do_not_share_scenario_or_constant_execution_order(tmp_path):
    before = '<Question eid="choice" PropertyKey="Mode"><Rule RuleClassName="SetValueRule" PropertyName="Mode" FromValue="Partial"/>' + gate(name='other gate') + '</Question>'
    screen(tmp_path, gate(), before)
    data = collect(tmp_path)
    gates = {e['name']: e for e in data['events'] if e['kind'] == 'evaluate'}
    conditions = {c['event']: c for c in data['scenario']['conditions']}
    assert conditions[gates['gate']['id']]['result'] is True
    assert gates['other gate']['scenario']['status'] == 'other_event_context'


def test_constants_propagate_through_component_input_and_output(tmp_path):
    screen(tmp_path, '''<Rule RuleClassName="SetValueRule" PropertyName="Mode" FromValue="Full"/>
      <Rule RuleClassName="CallComponentRule" SelectComponent="B.ifp" ComponentList="Q"
        i_ClassType="ComponentMapping" i_PropertyKey="Input.Mode" i_SolutionDataItemMapping="Mode" i_In="Y"
        o_ClassType="ComponentMapping" o_PropertyKey="Result" o_SolutionDataItemMapping="Answer" o_Out="Y"/>''' + gate('Answer', 'ok'))
    put(tmp_path, 'B.ifp', '''<Project><Product Name="Q"><Phase Name="I">
      <Rule Name="callee check" RuleClassName="EvaluateRule" Expression="$$Input.Mode$ == 'Full'">
        <Rule RuleClassName="SetValueRule" PropertyName="Result" FromValue="ok" RuleType="True"/>
        <Rule RuleClassName="SetValueRule" PropertyName="Result" FromValue="bad" RuleType="False"/>
      </Rule></Phase></Product></Project>''')
    data = collect(tmp_path)
    assert statuses(data)['no'] == 'excluded'
    assert all(c['result'] is True for c in data['scenario']['conditions'])


def test_project_attributes_branches_and_standard_rules_coexist(tmp_path):
    screen(tmp_path, '''<Rule Name="custom check" RuleClassName="vendor.Check" Predicate="$$Mode$ == 'Full'">
      <Rule Name="custom yes" RuleClassName="vendor.Assign" Destination="Amount" InputField="FullAmount" Outcome="pass"/>
      <Rule Name="custom no" RuleClassName="vendor.Assign" Destination="Amount" InputField="PartialAmount" Outcome="fail"/>
    </Rule><Rule Name="standard" RuleClassName="SetValueRule" PropertyName="Amount" FromValue="3"/>''')
    config = put(tmp_path, 'config.json', json.dumps({'trace': {'schema_version': 1, 'rules': [
        {'class': 'vendor.Check', 'kind': 'evaluate', 'attributes': {'Expression': 'Predicate'},
         'branch_attribute': 'Outcome', 'branches': {'pass': True, 'fail': False}},
        {'class': 'vendor.Assign', 'kind': 'set', 'attributes': {'PropertyName': 'Destination', 'FromPropertyName': 'InputField'},
         'defaults': {'FromType': 'Data Item'}}]}}))
    data = collect(tmp_path, config=config)
    assert statuses(data)['custom no'] == 'excluded'
    custom = next(e for e in data['events'] if e['name'] == 'custom yes')
    assert custom['reads'][0]['path'] == 'FullAmount'
    assert custom['guards'][0]['source_branch'] == 'pass'
    assert 'Destination' in data['nodes'][custom['node']]['attributes']
    assert custom['semantics']['attribute_mapping']['PropertyName'] == 'Destination'
    assert next(e for e in data['events'] if e['name'] == 'standard')['semantics']['origin'] == 'standard'


def test_exact_class_precedence_and_explicit_override(tmp_path):
    config = put(tmp_path, 'config.json', json.dumps({'trace': {'rules': [
        {'class': 'Check', 'kind': 'evaluate'}, {'class': 'vendor.Check', 'kind': 'container', 'override': True}]}}))
    semantics = TraceConfig(config)
    assert semantics.resolve('vendor.Check')['kind'] == 'container'
    assert semantics.resolve('another.Check')['kind'] == 'evaluate'
    config.write_text(json.dumps({'trace': {'rules': [{'class': 'SetValueRule', 'kind': 'container'}]}}))
    with pytest.raises(ValueError, match='override=true'):
        TraceConfig(config)
    config.write_text(json.dumps({'trace': {'rules': [{'class': 'SetValueRule', 'kind': 'container', 'override': True}]}}))
    assert TraceConfig(config).resolve('SetValueRule')['kind'] == 'container'


@pytest.mark.parametrize('spec', [
    {'class': 'X', 'kind': 'set', 'attributes': {'Typo': 'Destination'}},
    {'class': 'X', 'kind': 'set', 'attributes': {'PropertyName': 'X', 'FromValue': 'X'}},
    {'class': 'X', 'kind': 'evaluate', 'branches': {'yes': 'true'}},
    {'class': 'X', 'kind': 'made_up'},
])
def test_invalid_project_semantics_fail_early(tmp_path, spec):
    config = put(tmp_path, 'config.json', json.dumps({'trace': {'rules': [spec]}}))
    with pytest.raises(ValueError):
        TraceConfig(config)


def test_scenario_bundle_replays_without_original_scenario_file(tmp_path):
    screen(tmp_path, gate())
    path = put(tmp_path, 'scenario.json', json.dumps(scenario()))
    out = tmp_path / 'out'
    assert main(['trace', str(tmp_path), '--file', 'A.ifp', '--field', 'Amount', '--entry', 'P.I', '--scenario', str(path), '--output', str(out)]) == 0
    path.unlink()
    again = tmp_path / 'again'
    assert main(['trace', str(tmp_path), '--request', str(out / 'next.json'), '--output', str(again)]) == 0
    a, b = [json.loads((p / 'evidence.json').read_text()) for p in (out, again)]
    assert a['scenario'] == b['scenario']
    assert 'Scenario reasoning' in (again / 'report.md').read_text()


def test_scenario_requires_explicit_ui_trigger_and_rejects_conflicting_assumptions(tmp_path):
    screen(tmp_path, gate())
    with pytest.raises(ValueError, match='trigger_eid'):
        collect(tmp_path, {'assumptions': []})
    with pytest.raises(ValueError, match='Duplicate'):
        collect(tmp_path, {'trigger_eid': 'button', 'assumptions': [{'field': 'X', 'value': '1'}, {'field': 'X', 'value': '2'}]})


def test_symbolic_writer_does_not_prove_concrete_instance_value(tmp_path):
    screen(tmp_path, '<Rule RuleClassName="SetValueRule" PropertyName="Rows[C].Mode" FromValue="Full"/>' + gate('Rows[1].Mode'))
    data = collect(tmp_path, scenario('Rows[1].Mode', 'Partial'))
    assert data['scenario']['conditions'][0]['result'] == 'unknown'
    assert data['scenario']['conditions'][0]['facts'][0]['basis']['reason'] == 'conditional_or_symbolic_write'


def test_unknown_custom_branch_cannot_become_an_unconditional_write(tmp_path):
    screen(tmp_path, '''<Rule RuleClassName="vendor.Check" Expression="$$Mode$ == 'Full'">
      <Rule RuleClassName="SetValueRule" PropertyName="X" FromValue="Full" Outcome="unrecognized"/>
    </Rule>''' + gate('X'))
    config = put(tmp_path, 'config.json', json.dumps({'trace': {'rules': [
        {'class': 'vendor.Check', 'kind': 'evaluate', 'branch_attribute': 'Outcome', 'branches': {'yes': True, 'no': False}}]}}))
    data = collect(tmp_path, config=config)
    assert any(i['code'] == 'UNKNOWN_BRANCH' for i in data['issues'])
    assert next(c for c in data['scenario']['conditions'] if c['expression'] == "$$X$ == 'Full'")['result'] == 'unknown'


def test_question_activation_can_assume_its_new_input_value(tmp_path):
    put(tmp_path, 'A.ifp', '<Project><Product Name="P"><Phase Name="I"><Question PropertyKey="Mode" eid="button">'
        + gate() + '</Question></Phase></Product></Project>')
    data = collect(tmp_path)
    assert statuses(data)['no'] == 'excluded'
    assert data['scenario']['conditions'][0]['facts'][0]['basis']['reason'] == 'initial_scenario_assumption'


def test_incomplete_collection_does_not_produce_scenario_exclusions(tmp_path):
    screen(tmp_path, gate())
    data = Trace(tmp_path, max_contexts=2).collect('A.ifp', 'Amount', None, 'P.I', scenario=scenario())
    assert data['scenario'] is None
    assert any(i['code'] == 'SCENARIO_NOT_EVALUATED' for i in data['issues'])


def test_transformed_assignment_does_not_propagate_untransformed_text(tmp_path):
    screen(tmp_path, '<Rule RuleClassName="SetValueRule" PropertyName="Mode" FromType="Data Item" FromPropertyName="Raw" Trim="Y"/>' + gate())
    data = collect(tmp_path, scenario('Raw', ' Full '))
    assert data['scenario']['conditions'][0]['result'] == 'unknown'


def test_api_and_trace_semantics_cannot_silently_compete(tmp_path):
    config = put(tmp_path, 'config.json', json.dumps({'api_rule_classes': ['vendor.X'],
        'trace': {'rules': [{'class': 'vendor.X', 'kind': 'set'}]}}))
    with pytest.raises(ValueError, match='Conflicting'):
        Trace(tmp_path, rules_config=config)
