from pathlib import Path
import json

from ifp_contract.cli import main
from ifp_contract.trace import Trace


def put(root, name, xml):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(xml, encoding='utf-8')
    return p


def event(data, name, kind=None):
    return next(e for e in data['events'] if e['name'] == name and (kind is None or e['kind'] == kind))


def sample(root):
    put(root, 'Screen.ifp', '''<Project><Rules>
      <Rule Name="shared" eid="shared" RuleClassName="ContainerRule">
        <Rule Name="id" RuleClassName="SetValueRule" PropertyName="Filters[1].Value" FromType="Data Item" FromPropertyName="Selected.Id" RuleType="True"/>
        <Rule Name="date" RuleClassName="SetValueRule" PropertyName="Filters[2].Value" FromValue="today" RuleType="True"/>
        <Rule Name="wrapper" RuleClassName="CallComponentRule" SelectComponent="Wrapper.ifp" ComponentList="Lookup" RuleType="True"
          i_ClassType="ComponentMapping" i_PropertyKey="Input.Filters[A].Value" i_SolutionDataItemMapping="Filters[A].Value" i_In="Y"
          o_ClassType="ComponentMapping" o_PropertyKey="Amounts[A].Value" o_SolutionDataItemMapping="Balances[A].Value" o_Out="Y"
          t_ClassType="ComponentMapping" t_PropertyKey="Types[A].Value" t_SolutionDataItemMapping="Types[A].Value" t_Out="Y">
          <Rule Name="has rows" RuleClassName="EvaluateRule" Expression="$$Types[C].lastInstance()$ &gt; '0'" RuleType="True">
            <Rule Name="loop" RuleClassName="RepeatRule" DataGroupName="Types[C]" EndInstance="$$Types[C].lastInstance()$" RuleType="True">
              <Rule Name="align" RuleClassName="SetValueRule" Type="Data Group Instance" FromType="Data Group Instance" FromPropertyGroupInstanceName="Types[C]" PropertyGroupInstanceName="Balances[C]" RuleType="True"/>
              <Rule Name="not total" RuleClassName="EvaluateRule" Expression="$$Types[C].Value$ != 'TOTALDUE'" RuleType="True">
                <Rule Name="other" RuleClassName="SetValueRule" PropertyName="Other" FromValue="x" RuleType="True"/>
                <Rule Name="total" eid="total" RuleClassName="ExpressionRule" Expression='("$$Balances[C].Value$").substring(1)' OutputProperty="Total" RuleType="False"/>
              </Rule>
            </Rule>
          </Rule>
        </Rule>
      </Rule>
    </Rules><Product Name="Pay" InitialPhase="Pay.Init"><Phase Name="Init">
      <Rule Name="pay gate" RuleClassName="EvaluateRule" Expression="$$!MODE$ == 'Pay'" RuleType="PostPhase">
        <Rule Name="link" LinkReference="shared" RuleType="True"/>
        <Rule Name="fill" RuleClassName="SetValueRule" PropertyName="Amount" FromType="Data Item" FromPropertyName="Total" RuleType="True"/>
      </Rule>
    </Phase></Product></Project>''')
    put(root, 'Wrapper.ifp', '''<Project><Product Name="Lookup" InitialPhase="Lookup.Init"><Phase Name="Init">
      <Rule Name="copy id" RuleClassName="SetValueRule" PropertyName="Id" FromType="Data Item" FromPropertyName="Input.Filters[1].Value"/>
      <Rule Name="api call" RuleClassName="CallComponentRule" SelectComponent="Api.ifp" ComponentList="Read"
        i_ClassType="ComponentMapping" i_PropertyKey="Request.Id" i_SolutionDataItemMapping="Id" i_In="Y"
        a_ClassType="ComponentMapping" a_PropertyKey="Response.Items[A].Amount" a_SolutionDataItemMapping="Items[A].Amount" a_Out="Y"
        t_ClassType="ComponentMapping" t_PropertyKey="Response.Items[A].Type" t_SolutionDataItemMapping="Items[A].Type" t_Out="Y"
        e_ClassType="ComponentMapping" e_PropertyKey="Errors.Text" e_SolutionDataItemMapping="Error" e_Out="Y">
        <Rule Name="no error" RuleClassName="EvaluateRule" Expression="$$Error$ == ''" RuleType="True">
          <Rule Name="split" RuleClassName="RepeatRule" DataGroupName="Items[C]" RuleType="True">
            <Rule Name="amount" RuleClassName="SetValueRule" PropertyName="Amounts[C].Value" FromType="Data Item" FromPropertyName="Items[C].Amount" RuleType="True"/>
            <Rule Name="type" RuleClassName="SetValueRule" PropertyName="Types[C].Value" FromType="Data Item" FromPropertyName="Items[C].Type" RuleType="True"/>
            <Rule Name="advance amounts" RuleClassName="IncrementorRule" Type="Data Group Instance" PropertyGroupName="Amounts[C]" RuleType="True"/>
          </Rule>
        </Rule>
      </Rule>
    </Phase></Product></Project>''')
    put(root, 'Api.ifp', '''<Project><Product Name="Read" InitialPhase="Read.Init"><Phase Name="Init">
      <Rule Name="fetch" RuleClassName="SwaggerIntegrationRule" HTTPMethod="GET" ResourcePath="/loans/$$Request.Id$" Output="Response" AdditionalHTTPDataGroups="Errors"/>
    </Phase></Product></Project>''')


def test_cross_file_conditional_fill_and_instance_projection(tmp_path):
    sample(tmp_path)
    data = Trace(tmp_path).collect('Screen.ifp', 'Amount', None, 'Pay.Init')
    assert data['coverage']['files_read'] == 3
    assert not data['issues']
    total = event(data, 'total')
    evaluation = event(data, 'not total')
    fill = event(data, 'fill')
    assert any(g['event'] == evaluation['id'] and g['branch'] == 'False' for g in total['guards'])
    assert not any(g['event'] == evaluation['id'] for g in fill['guards'])
    assert 'substring(1)' in data['nodes'][total['node']]['attributes']['Expression']
    assert event(data, 'align')['kind'] == 'instance'
    assert event(data, 'advance amounts')['kind'] == 'instance'
    assert event(data, 'fetch')['api']['method'] == 'GET'
    assert any(i['event'] == fill['id'] and i['status'] == 'conditional_write_or_prior_value' for i in data['inputs'])
    # The filter array mapping must project instance 1, not include the unrelated date.
    assert 'date' not in [e['name'] for e in data['events']]
    assert 'other' not in [e['name'] for e in data['events']]
    assert any(i['field']['path'] == 'Selected.Id' for i in data['inputs'])


def test_pubout_is_not_an_enabled_call_mapping(tmp_path):
    put(tmp_path, 'A.ifp', '''<Project><Product Name="P"><Phase Name="I">
      <Rule Name="call" RuleClassName="CallComponentRule" SelectComponent="B.ifp" ComponentList="P"
       o_ClassType="ComponentMapping" o_PropertyKey="Value" o_SolutionDataItemMapping="Amount" o_PubOut="Y" o_Out="N"/>
    </Phase></Product></Project>''')
    put(tmp_path, 'B.ifp', '<Project><Product Name="P"><Phase Name="I"/></Product></Project>')
    data = Trace(tmp_path).collect('A.ifp', 'Amount', None, 'P.I')
    assert not data['seeds']
    assert any(i['code'] == 'ANCHOR_NOT_FOUND' for i in data['issues'])


def test_reused_rule_keeps_entry_conditions_separate(tmp_path):
    put(tmp_path, 'A.ifp', '''<Project><Rules><Rule Name="set" eid="s" RuleClassName="SetValueRule" PropertyName="X" FromValue="1"/></Rules>
      <Product Name="P"><Phase Name="A"><Rule Name="a" RuleClassName="EvaluateRule" Expression="$$Flag$ == 'A'"><Rule Name="la" LinkReference="s" RuleType="True"/></Rule></Phase>
      <Phase Name="B"><Rule Name="b" RuleClassName="EvaluateRule" Expression="$$Flag$ == 'B'"><Rule Name="lb" LinkReference="s" RuleType="False"/></Rule></Phase></Product></Project>''')
    data = Trace(tmp_path).collect('A.ifp', 'X', None, None)
    setters = [e for e in data['events'] if e['name'] == 'set']
    assert len(setters) == 2
    assert {e['entry'] for e in setters} == {'P.A', 'P.B'}
    assert setters[0]['guards'][0]['branch'] == 'True'
    assert setters[1]['guards'][0]['branch'] == 'False'


def test_reset_unknown_disabled_and_recursive_link(tmp_path):
    put(tmp_path, 'A.ifp', '''<Project><Rules><Rule Name="recursive" eid="r" LinkReference="r"/></Rules><Product Name="P"><Phase Name="I">
      <Rule Name="old" RuleClassName="SetValueRule" PropertyName="X" FromValue="old"/>
      <Rule Name="reset" RuleClassName="ResetDataRule" ResetProperty="X"/>
      <Rule Name="disabled" RuleClassName="SetValueRule" PropertyName="X" FromValue="wrong" RuleDisabled="Y"/>
      <Rule Name="unknown" RuleClassName="VendorRule" Custom_Expr="$$X$"/>
      <Rule Name="loop link" LinkReference="r"/>
      <Rule Name="copy" RuleClassName="SetValueRule" PropertyName="Amount" FromType="Data Item" FromPropertyName="X"/>
    </Phase></Product></Project>''')
    data = Trace(tmp_path).collect('A.ifp', 'Amount', None, 'P.I')
    assert 'disabled' not in [e['name'] for e in data['events']]
    assert event(data, 'reset')['kind'] == 'reset'
    assert 'old' not in [e['name'] for e in data['events']]
    assert any(i['code'] == 'UNKNOWN_RULE' for i in data['issues'])
    assert any(i['code'] == 'RECURSIVE_REFERENCE' for i in data['issues'])
    unknown = event(data, 'unknown')
    assert data['nodes'][unknown['node']]['attributes']['Custom_Expr'] == '$$X$'


def test_bundle_request_changed_file_and_limits(tmp_path):
    sample(tmp_path)
    out = tmp_path / 'out'
    args = ['trace', str(tmp_path), '--file', 'Screen.ifp', '--field', 'Amount', '--entry', 'Pay.Init']
    assert main([*args, '--output', str(out)]) == 0
    assert (out / 'report.md').is_file()
    assert main(['trace', str(tmp_path), '--request', str(out / 'next.json'), '--output', str(tmp_path / 'again')]) == 0
    p = tmp_path / 'Api.ifp'
    p.write_text(p.read_text().replace('/loans/', '/newloans/'))
    assert main(['trace', str(tmp_path), '--request', str(out / 'next.json'), '--output', str(tmp_path / 'changed')]) == 0
    changed = json.loads((tmp_path / 'changed/evidence.json').read_text())
    assert any(i['code'] == 'SOURCE_CHANGED_SINCE_REQUEST' for i in changed['issues'])
    assert main([*args, '--output', str(tmp_path / 'limited'), '--max-contexts', '3', '--strict']) == 2
    limited = json.loads((tmp_path / 'limited/evidence.json').read_text())
    assert limited['coverage']['limited']


def test_encoding_comments_and_path_variables(tmp_path):
    sample(tmp_path)
    p = tmp_path / 'Wrapper.ifp'
    content = p.read_text().replace('SelectComponent="Api.ifp"', 'SelectComponent="$$LIBRARY_HOME$/Api.ifp"')
    content = content.replace('<Project>', '<Project><!-- <Rule Name="fake"/> -->')
    p.write_bytes(('<?xml version="1.0" encoding="UTF-16"?>' + content).encode('utf-16'))
    data = Trace(tmp_path, path_variables={'LIBRARY_HOME': '.'}).collect('Screen.ifp', 'Amount', None, 'Pay.Init')
    assert data['coverage']['files_read'] == 3
    assert not data['issues']
    assert 'fake' not in [e['name'] for e in data['events']]


def test_trace_rule_config_and_http_fallback(tmp_path):
    sample(tmp_path)
    p = tmp_path / 'Api.ifp'
    p.write_text(p.read_text().replace('SwaggerIntegrationRule', 'vendor.ReadRule').replace(' HTTPMethod="GET"', ''))
    config = put(tmp_path, 'config.json', json.dumps({'api_rule_classes': ['vendor.ReadRule'], 'method_by_rule_class_suffix': {'ReadRule': 'GET'}}))
    data = Trace(tmp_path, rules_config=config).collect('Screen.ifp', 'Amount', None, 'Pay.Init')
    assert event(data, 'fetch')['api']['method'] == 'GET'


def test_ui_events_are_not_a_single_execution_sequence(tmp_path):
    put(tmp_path, 'A.ifp', '''<Project><Product Name="P"><Phase Name="Input">
      <Button Name="continue"><Rule Name="fill" RuleClassName="SetValueRule" PropertyName="Result" FromType="Data Item" FromPropertyName="X"/></Button>
      <Question Name="choice" PropertyKey="Choice" PostQuestionRules="Y">
        <Rule Name="load" RuleClassName="SetValueRule" PropertyName="X" FromValue="1"/>
      </Question>
    </Phase></Product></Project>''')
    data = Trace(tmp_path).collect('A.ifp', 'Result', None, 'P.Input')
    load, fill = event(data, 'load'), event(data, 'fill')
    edge = next(e for e in data['edges'] if e['from'] == load['id'] and e['to'] == fill['id'])
    assert edge['event_order'] == 'unknown_between_ui_events'
    assert edge['conditional']
    assert load['triggers'] != fill['triggers']


def test_post_payload_and_query_are_both_dependencies(tmp_path):
    put(tmp_path, 'A.ifp', '''<Project><Product Name="P"><Phase Name="I">
      <Rule Name="body" RuleClassName="SetValueRule" PropertyName="Payload.Amount" FromValue="12"/>
      <Rule Name="query" RuleClassName="SetValueRule" PropertyName="Query.Mode" FromValue="check"/>
      <Rule Name="api" RuleClassName="SwaggerIntegrationRule" HTTPMethod="POST" Input="Payload" QueryInputDataGroup="Query" Output="Response"/>
    </Phase></Product></Project>''')
    data = Trace(tmp_path).collect('A.ifp', 'Response.Value', None, 'P.I')
    assert {'body', 'query'} <= {e['name'] for e in data['events']}


def test_file_limit_keeps_partial_evidence_and_request(tmp_path):
    sample(tmp_path)
    data = Trace(tmp_path, max_files=2).collect('Screen.ifp', 'Amount', None, 'Pay.Init')
    assert data['seeds']
    assert data['events']
    assert data['coverage']['limited']
    assert any(i['code'] == 'FILE_LIMIT' for i in data['issues'])


def test_missing_component_file_is_explicit(tmp_path):
    sample(tmp_path)
    (tmp_path / 'Api.ifp').unlink()
    data = Trace(tmp_path).collect('Screen.ifp', 'Amount', None, 'Pay.Init')
    assert any(i['code'] == 'COMPONENT_NOT_UNIQUE' for i in data['issues'])
    assert data['inputs']


def test_dynamic_index_keeps_outer_field_and_index_variable():
    from ifp_contract.trace import refs
    assert refs('$$Accounts[$$!Selected$].Amount$') == ['!Selected', 'Accounts[$$!Selected$].Amount']


def test_repeated_component_calls_have_separate_parameters(tmp_path):
    put(tmp_path, 'A.ifp', '''<Project><Product Name="P"><Phase Name="I">
      <Rule Name="first id" RuleClassName="SetValueRule" PropertyName="Id" FromValue="first"/>
      <Rule Name="first" RuleClassName="CallComponentRule" SelectComponent="B.ifp" ComponentList="P"
        i_ClassType="ComponentMapping" i_PropertyKey="Request.Id" i_SolutionDataItemMapping="Id" i_In="Y"
        o_ClassType="ComponentMapping" o_PropertyKey="Response.Value" o_SolutionDataItemMapping="First" o_Out="Y"/>
      <Rule Name="second id" RuleClassName="SetValueRule" PropertyName="Id" FromValue="second"/>
      <Rule Name="second" RuleClassName="CallComponentRule" SelectComponent="B.ifp" ComponentList="P"
        i_ClassType="ComponentMapping" i_PropertyKey="Request.Id" i_SolutionDataItemMapping="Id" i_In="Y"
        o_ClassType="ComponentMapping" o_PropertyKey="Response.Value" o_SolutionDataItemMapping="Second" o_Out="Y"/>
    </Phase></Product></Project>''')
    put(tmp_path, 'B.ifp', '''<Project><Product Name="P"><Phase Name="I">
      <Rule Name="api" RuleClassName="SwaggerIntegrationRule" ResourcePath="/$$Request.Id$" Output="Response"/>
    </Phase></Product></Project>''')
    data = Trace(tmp_path).collect('A.ifp', 'Second', None, 'P.I')
    names = {e['name'] for e in data['events']}
    assert 'second id' in names and 'first id' not in names and 'first' not in names


def test_malformed_file_exports_error_bundle(tmp_path):
    put(tmp_path, 'A.ifp', '<Project><Product Name="P"><Phase Name="I"><Rule Name="broken"')
    out = tmp_path / 'out'
    assert main(['trace', str(tmp_path), '--file', 'A.ifp', '--field', 'X', '--output', str(out)]) == 1
    data = json.loads((out / 'evidence.json').read_text())
    assert any(i['code'] == 'FILE_ERROR' for i in data['issues'])


def test_large_unknown_attribute_is_reported_as_truncated(tmp_path):
    put(tmp_path, 'A.ifp', '<Project><Product Name="P"><Phase Name="I">'
        '<Rule Name="opaque" RuleClassName="Vendor" Script="' + 'x' * (1024 * 1024 + 100) + '"/>'
        '<Rule Name="set" RuleClassName="SetValueRule" PropertyName="X" FromValue="1"/>'
        '</Phase></Product></Project>')
    data = Trace(tmp_path).collect('A.ifp', 'X', None, 'P.I')
    assert any(i['code'] == 'TRUNCATED_ATTRIBUTE' for i in data['issues'])
    assert any(i['code'] == 'RAW_EVIDENCE_LIMIT' for i in data['issues'])


def test_request_config_is_portable(tmp_path):
    sample(tmp_path)
    config = put(tmp_path, 'config.json', '{}')
    out = tmp_path / 'out'
    assert main(['trace', str(tmp_path), '--file', 'Screen.ifp', '--field', 'Amount', '--entry', 'Pay.Init',
                 '--rules-config', str(config), '--output', str(out)]) == 0
    config.unlink()
    assert main(['trace', str(tmp_path), '--request', str(out / 'next.json'), '--output', str(tmp_path / 'again')]) == 0


def test_container_anchor_includes_descendant_execution_evidence(tmp_path):
    sample(tmp_path)
    data = Trace(tmp_path).collect('Screen.ifp', None, 'shared', 'Pay.Init')
    assert event(data, 'other')['kind'] == 'set'
    assert event(data, 'fetch')['kind'] == 'api'


def test_instance_setter_reads_the_saved_index(tmp_path):
    put(tmp_path, 'A.ifp', '''<Project><Product Name="P"><Phase Name="I">
      <Rule Name="repeat" RuleClassName="RepeatRule" DataGroupName="Accounts[C]">
        <Rule Name="save index" RuleClassName="SetValueRule" FromType="Data Group Instance" FromPropertyGroupInstanceName="Accounts[C]" PropertyName="SelectedIndex"/>
      </Rule>
      <Rule Name="restore index" RuleClassName="SetValueRule" Type="Data Group Instance" PropertyGroupInstanceName="Accounts[C]" FromType="Data Item" FromPropertyName="SelectedIndex"/>
      <Rule Name="fill" RuleClassName="SetValueRule" PropertyName="Amount" FromType="Data Item" FromPropertyName="Accounts[C].Amount"/>
    </Phase></Product></Project>''')
    data = Trace(tmp_path).collect('A.ifp', 'Amount', None, 'P.I')
    restore, save = event(data, 'restore index'), event(data, 'save index')
    assert any(e['from'] == save['id'] and e['to'] == restore['id'] for e in data['edges'])
    assert save['reads'][0]['path'] == '@instance:Accounts[C]'


def test_plain_condition_field_is_a_dependency_but_quoted_dotted_text_is_not(tmp_path):
    put(tmp_path, 'A.ifp', '''<Project><Product Name="P"><Phase Name="I">
      <Rule Name="mode" RuleClassName="SetValueRule" PropertyName="Form[1].Mode" FromValue="Ready"/>
      <Rule Name="gate" RuleClassName="EvaluateRule" Expression="Form[1].Mode == 'release.v1'">
        <Rule Name="fill" RuleClassName="SetValueRule" PropertyName="Target" FromValue="1" RuleType="True"/>
      </Rule>
    </Phase></Product></Project>''')
    data = Trace(tmp_path).collect('A.ifp', 'Target', None, 'P.I')
    gate = event(data, 'gate')
    assert [read['path'] for read in gate['reads']] == ['Form[1].Mode']
    assert gate['condition_dependencies']['status'] == 'lexically_extracted'
    assert 'mode' in {item['name'] for item in data['events']}


def test_unknown_standard_branch_is_retained_as_an_unknown_guard(tmp_path):
    put(tmp_path, 'A.ifp', '''<Project><Product Name="P"><Phase Name="I">
      <Rule Name="old" RuleClassName="SetValueRule" PropertyName="Target" FromValue="old"/>
      <Rule Name="gate" RuleClassName="EvaluateRule" Expression="$$Mode$ == 'Ready'">
        <Rule Name="maybe" RuleClassName="SetValueRule" PropertyName="Target" FromValue="new" RuleType="true"/>
      </Rule>
      <Rule Name="use" RuleClassName="SetValueRule" PropertyName="Result" FromType="Data Item" FromPropertyName="Target"/>
    </Phase></Product></Project>''')
    data = Trace(tmp_path).collect('A.ifp', 'Result', None, 'P.I')
    maybe = event(data, 'maybe')
    assert maybe['guards'][0]['status'] == 'unknown_branch_label'
    assert maybe['guards'][0]['branch'] == 'true'
    assert any(issue['code'] == 'UNKNOWN_BRANCH' and issue['label'] == 'true'
               for issue in data['issues'])
    assert {'old', 'maybe', 'use'} <= {item['name'] for item in data['events']}


def test_unsupported_assignment_type_is_an_explicit_boundary(tmp_path):
    put(tmp_path, 'A.ifp', '''<Project><Product Name="P"><Phase Name="I">
      <Rule Name="session write" eid="session-write" RuleClassName="SetValueRule"
        Type="Http Session Variable" HttpSessionVariableName="Token" FromValue="value"/>
    </Phase></Product></Project>''')
    data = Trace(tmp_path).collect('A.ifp', None, 'session-write', 'P.I')
    assert any(issue['code'] == 'UNSUPPORTED_ASSIGNMENT_TARGET_TYPE'
               and issue['target_type'] == 'Http Session Variable' for issue in data['issues'])
    assert not event(data, 'session write')['writes']
