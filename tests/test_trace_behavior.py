import json

import pytest

from ifp_contract.inspect import inspect_project
from ifp_contract.trace import Trace


def screen(tmp_path, rules):
    (tmp_path / 'Screen.ifp').write_text(
        f'<Project><Product Name="P"><Phase Name="I">{rules}</Phase></Product></Project>')


@pytest.mark.parametrize('value', ['1', ' yes ', 'ON', 'true', 'Y'])
def test_trace_and_inspect_share_disabled_boolean_values(tmp_path, value):
    screen(tmp_path, f'<Rule RuleClassName="SetValueRule" RuleDisabled="{value}" '
                     'PropertyName="Result" FromValue="wrong"/>')
    data = Trace(tmp_path).collect('Screen.ifp', 'Result', None, 'P.I')
    report, _ = inspect_project(tmp_path)
    assert not data['seeds']
    assert report['coverage']['disabled_rules'] == 1


def test_isolated_anchor_respects_mapped_disabled_parent(tmp_path):
    screen(tmp_path, '<Rule RuleClassName="ContainerRule" Stop=" yes ">'
                     '<Rule eid="child" RuleClassName="SetValueRule" PropertyName="Result" FromValue="1"/>'
                     '</Rule>')
    config = tmp_path / 'rules.json'
    config.write_text(json.dumps({'trace': {'rules': [{
        'class': 'ContainerRule', 'kind': 'container', 'override': True,
        'attributes': {'RuleDisabled': 'Stop'},
    }]}}))
    trace = Trace(tmp_path, rules_config=config)
    data = trace.collect('Screen.ifp', None, 'child', '@child')
    assert data['events'][0]['kind'] == 'disabled'
    assert data['events'][0]['writes'] == []
    assert trace.disabled_cache
    report, _ = inspect_project(tmp_path, rules_config=config)
    assert report['coverage']['disabled_rules'] == 2


@pytest.mark.parametrize('spec,attributes,missing', [
    ({'api_rule_attribute_overrides': {'CallComponentRule': {'selector': ['ComponentPath']}}},
     'ComponentPath="Child.ifp"', False),
    ({'trace': {'rules': [{'class': 'CallComponentRule', 'kind': 'call', 'override': True,
                         'defaults': {'SelectComponent': 'Child.ifp'}}]}}, '', False),
    ({'trace': {'rules': [{'class': 'CallComponentRule', 'kind': 'call', 'override': True,
                         'attributes': {'SelectComponent': 'MappedPath'}}]}},
     'SelectComponent="Child.ifp"', True),
])
def test_trace_and_inspect_share_selector_precedence(tmp_path, spec, attributes, missing):
    (tmp_path / 'Child.ifp').write_text(
        '<Project><Product Name="Child"><Phase Name="I"/></Product></Project>')
    screen(tmp_path, f'<Rule eid="call" RuleClassName="CallComponentRule" '
                     f'ComponentList="Child" {attributes}/>')
    config = tmp_path / 'rules.json'
    config.write_text(json.dumps(spec))
    data = Trace(tmp_path, rules_config=config).collect('Screen.ifp', None, 'call', 'P.I')
    report, _ = inspect_project(tmp_path, rules_config=config)
    assert any(i['code'] == 'MISSING_COMPONENT_SELECTOR' for i in data['issues']) == missing
    assert any(i['code'] == 'MISSING_COMPONENT_SELECTOR' for i in report['findings']) == missing
    assert data['coverage']['files_read'] == (1 if missing else 2)


def test_partial_rule_reads_are_connected_without_hiding_unknown_behavior(tmp_path):
    screen(tmp_path, '''
      <Rule Name="source" RuleClassName="SetValueRule" PropertyName="Items[1].Id" FromValue="USD"/>
      <Rule Name="list" eid="list" RuleClassName="AddToListRule" ListType="DYNAMIC"
            DynamicListToAddTo="Currency List" KeyType="Data Item" KeyPropertyName="Items[1].Id"/>
    ''')
    data = Trace(tmp_path).collect('Screen.ifp', None, 'list', 'P.I')
    rule = next(e for e in data['events'] if e['name'] == 'list')
    assert rule['kind'] == 'unknown'
    assert rule['partial_semantics']['list_target']['name'] == 'Currency List'
    assert rule['writes'] == []
    assert rule['semantics']['incomplete']
    assert any(e['name'] == 'source' for e in data['events'])
    assert any(i['code'] == 'UNKNOWN_RULE' for i in data['issues'])
