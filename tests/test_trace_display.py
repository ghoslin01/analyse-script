import pytest

from ifp_contract.trace import Trace
from ifp_contract.trace_report import logic_summary, _source_chains


TARGET = (
    "OrderForm[1].Orders[1].Details[1].Instructions[1]."
    "DirectionLabel"
)
SELL_CURRENCY = "OrderForm[1].Orders[1].Details[1].Instructions[1].OutgoingCode"
BUY_CURRENCY = "OrderForm[1].Orders[1].Details[1].Instructions[1].IncomingCode"
ORDER_NATURE = "OrderForm[1].Orders[1].Details[1].Direction"
CONDITION_FIELD = "Context[1].ProductKind"
SUBTYPE_FIELD = "Context[1].Instrument[1].TypeCode"


def put(root, name, xml):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8")
    return path


def events_named(data, name):
    return [event for event in data["events"] if event["name"] == name]


def display_fixture(root, readonly="Y"):
    put(root, "Screen.ifp", f'''<Project>
      <Product Name="OrderScreen" InitialPhase="OrderScreen.Details">
        <Phase Name="Details">
          <Question Name="Direction display" QuestionText="Direction label"
            PropertyKey="{TARGET}" ReadOnly="{readonly}" NotApplicable="Y"
            ConditionExpression="$${CONDITION_FIELD}$ == '10' AND $${SUBTYPE_FIELD}$ != '803'"
            eid="display-label-001">
          </Question>
          <Rule Name="outgoing code source" RuleClassName="SwaggerIntegrationRule"
            HTTPMethod="GET" ResourcePath="/reference/outgoing" Output="{SELL_CURRENCY}" />
          <Rule Name="Evaluate direction"
            RuleClassName="EvaluateRule"
            Expression="$${ORDER_NATURE}$ == 'Outbound'">
            <Rule Name="outbound branch" eid="rule-outbound-001" RuleType="True" RuleClassName="SetValueRule"
              PropertyName="{TARGET}" FromValue="Outbound $${SELL_CURRENCY}$" />
            <Rule Name="inbound branch" eid="rule-inbound-001" RuleType="False" RuleClassName="SetValueRule"
              PropertyName="{TARGET}" FromValue="Inbound $${BUY_CURRENCY}$" />
          </Rule>
          <Rule Name="visibility source" RuleClassName="SwaggerIntegrationRule"
            HTTPMethod="GET" ResourcePath="/reference/visibility" Output="{CONDITION_FIELD}" />
        </Phase>
        <Phase Name="Other">
          <Rule Name="unrelated phase writer" RuleClassName="SetValueRule"
            PropertyName="{TARGET}" FromValue="wrong phase" />
        </Phase>
      </Product>
    </Project>''')


def trace(root):
    return Trace(root).collect("Screen.ifp", TARGET, None, "OrderScreen.Details")


@pytest.mark.parametrize("readonly", ["Y", "y", "Yes", "true", "1", "ON"])
def test_readonly_question_is_a_display_anchor_for_all_true_spellings(tmp_path, readonly):
    display_fixture(tmp_path, readonly)
    data = trace(tmp_path)

    display = events_named(data, "Direction display")
    assert len(display) == 1
    display = display[0]
    assert display["kind"] == "ui_event"
    assert display["writes"] == []
    assert display["reads"] == [
        {"scope": "OrderScreen.Details:root", "path": TARGET, "group": False}
    ]
    assert display["display_field"] == {
        "scope": "OrderScreen.Details:root", "path": TARGET, "group": False
    }
    assert display["ui_condition_fields"] == [
        {"scope": "OrderScreen.Details:root", "path": CONDITION_FIELD, "group": False},
        {"scope": "OrderScreen.Details:root", "path": SUBTYPE_FIELD, "group": False},
    ]
    assert display["ui_condition"]["expression"] == (
        f"$${CONDITION_FIELD}$ == '10' AND $${SUBTYPE_FIELD}$ != '803'"
    )
    assert display["ui_condition"]["not_applicable"] == "Y"

    assert not any(issue["code"] == "ANCHOR_NOT_FOUND" for issue in data["issues"])
    assert "unrelated phase writer" not in [event["name"] for event in data["events"]]
    assert "inbound branch" in [event["name"] for event in data["events"]]
    assert "outbound branch" in [event["name"] for event in data["events"]]


def test_display_and_writer_share_target_but_condition_api_is_control_only(tmp_path):
    display_fixture(tmp_path)
    data = trace(tmp_path)
    display = events_named(data, "Direction display")[0]
    sell = events_named(data, "outbound branch")[0]
    country = events_named(data, "outgoing code source")[0]
    condition = events_named(data, "visibility source")[0]

    assert display["id"] in data["seeds"]
    assert sell["id"] in data["seeds"]
    assert sell["reads"] == [
        {"scope": "OrderScreen.Details:root", "path": SELL_CURRENCY, "group": False}
    ]
    assert any(edge["from"] == country["id"] and edge["to"] == sell["id"]
               and edge.get("dependency_role", "value") == "value"
               for edge in data["edges"])

    condition_edges = [edge for edge in data["edges"]
                       if edge["from"] == condition["id"] and edge["to"] == display["id"]]
    assert condition_edges
    assert all(edge["dependency_role"] == "control" for edge in condition_edges)
    conclusions = data["conclusions"]["anchors"]
    display_conclusion = next(item for item in conclusions if item["seed"] == display["id"])
    assert display_conclusion['direct_source_api_events'] == [country['id']]
    chains, api_ids, _ = _source_chains(data)
    assert api_ids == [country['id']]
    assert chains[0][0] == [display['id'], sell['id'], country['id']]

    summary = logic_summary(data)
    assert "Direction label" in summary
    assert display["node"] in data["nodes"]
    assert data["nodes"][display["node"]]["attributes"]["QuestionText"] == "Direction label"
    assert data["nodes"][display["node"]]["attributes"]["eid"] == "display-label-001"


def test_editable_question_remains_input_and_does_not_claim_display_source(tmp_path):
    display_fixture(tmp_path, "N")
    data = trace(tmp_path)
    display = events_named(data, "Direction display")[0]

    assert display["kind"] == "ui_input"
    assert display["writes"] == [
        {"scope": "OrderScreen.Details:root", "path": TARGET, "group": False}
    ]
    assert "display_field" not in display
    assert display['reads'] == []
    assert not any(edge['from'] == display['id'] and edge['to'] == display['id']
                   for edge in data['edges'])


@pytest.mark.parametrize('attributes', ['ReadOnly="Y"', 'DisableInput="1"'])
def test_display_without_writer_has_an_anchor_and_unresolved_value(tmp_path, attributes):
    put(tmp_path, 'Screen.ifp', f'<Project><Product Name="OrderScreen"><Phase Name="Details">'
        f'<Question PropertyKey="{TARGET}" {attributes} QuestionText="Direction label"/>'
        '</Phase></Product></Project>')
    data = trace(tmp_path)
    assert len(data['seeds']) == 1
    assert not any(i['code'] == 'ANCHOR_NOT_FOUND' for i in data['issues'])
    assert any(i['field']['path'] == TARGET for i in data['inputs'])
    assert data['conclusions']['overall_status'] == 'blocked'


def test_product_writer_is_a_candidate_without_importing_sibling_phases(tmp_path):
    put(tmp_path, 'Screen.ifp', f'''<Project><Product Name="OrderScreen">
      <Rule Name="product source" RuleClassName="SwaggerIntegrationRule" HTTPMethod="GET"
            ResourcePath="/reference/outgoing" Output="{SELL_CURRENCY}"/>
      <Rule Name="Evaluate direction" RuleClassName="EvaluateRule"
            Expression="$${ORDER_NATURE}$ == 'Outbound'">
        <Rule Name="product outbound" eid="rule-outbound-001" RuleClassName="SetValueRule"
              RuleType="True" PropertyName="{TARGET}" FromValue="Outbound $${SELL_CURRENCY}$"/>
      </Rule>
      <Phase Name="Other"><Rule Name="sibling writer" RuleClassName="SetValueRule"
              PropertyName="{TARGET}" FromValue="DO NOT COLLECT"/></Phase>
      <Phase Name="Details"><Question Name="display" QuestionText="Direction label"
              eid="display-label-001" PropertyKey="{TARGET}" ReadOnly="Y"/></Phase>
    </Product></Project>''')
    data = trace(tmp_path)
    display = events_named(data, 'display')[0]
    sell = events_named(data, 'product outbound')[0]
    source = events_named(data, 'product source')[0]
    assert not events_named(data, 'sibling writer')
    edge = next(e for e in data['edges'] if e['from'] == sell['id'] and e['to'] == display['id'])
    assert edge['conditional']
    assert edge['event_order'] == 'unknown_product_scheduling'
    conclusion = next(c for c in data['conclusions']['anchors'] if c['seed'] == display['id'])
    assert conclusion['direct_source_api_events'] == [source['id']]
    assert any(c['code'] == 'PRODUCT_RULE_SCHEDULING_UNKNOWN' for c in conclusion['blockers'])


def test_product_rule_candidates_do_not_override_phase_scenario_facts(tmp_path):
    put(tmp_path, 'Screen.ifp', '''<Project><Product Name="OrderScreen">
      <Rule RuleClassName="SetValueRule" PropertyName="Flag" FromValue="Yes"/>
      <Phase Name="Details">
        <Rule Name="phase guard" RuleClassName="EvaluateRule" Expression="$$Flag$ == 'Yes'">
          <Rule RuleClassName="SetValueRule" RuleType="True" PropertyName="Result" FromValue="1"/>
        </Rule>
      </Phase></Product></Project>''')
    data = Trace(tmp_path).collect('Screen.ifp', 'Result', None, 'OrderScreen.Details',
                                  scenario={'assumptions': []})
    assert data['scenario']['conditions'][0]['result'] == 'unknown'


def test_product_rules_are_also_candidates_inside_called_components(tmp_path):
    put(tmp_path, 'Caller.ifp', '''<Project><Product Name="Caller"><Phase Name="I">
      <Rule RuleClassName="CallComponentRule" SelectComponent="Child.ifp" ComponentList="Child"
            o_ClassType="ComponentMapping" o_PropertyKey="Value"
            o_SolutionDataItemMapping="Result" o_Out="Y"/>
    </Phase></Product></Project>''')
    put(tmp_path, 'Child.ifp', '''<Project><Product Name="Child">
      <Rule Name="product writer" RuleClassName="SetValueRule" PropertyName="Value" FromValue="x"/>
      <Phase Name="I"/>
    </Product></Project>''')
    data = Trace(tmp_path).collect('Caller.ifp', 'Result', None, 'Caller.I')
    writer = events_named(data, 'product writer')[0]
    assert any(edge['from'] == writer['id'] and edge['event_order'] == 'unknown_product_scheduling'
               for edge in data['edges'])
    assert data['conclusions']['overall_status'] == 'blocked'


def test_local_direction_assignments_keep_all_three_unresolved_inputs(tmp_path):
    put(tmp_path, 'Screen.ifp', f'''<Project><Product Name="OrderScreen">
      <Rule Name="Direction check" eid="evaluate-direction-001"
            RuleClassName="EvaluateRule" Expression="$${ORDER_NATURE}$ == 'Outbound'">
        <Rule Name="Outbound" eid="rule-outbound-001" RuleClassName="SetValueRule"
              RuleType="True" PropertyName="{TARGET}" FromValue="Outbound $${SELL_CURRENCY}$"/>
        <Rule Name="Inbound" eid="rule-inbound-001" RuleClassName="SetValueRule"
              RuleType="False" PropertyName="{TARGET}" FromValue="Inbound $${BUY_CURRENCY}$"/>
      </Rule>
      <Phase Name="Details"><Question Name="display" QuestionText="Direction label"
            eid="display-label-001" ReadOnly="Y" PropertyKey="{TARGET}"/></Phase>
    </Product></Project>''')
    data = trace(tmp_path)
    sell = events_named(data, 'Outbound')[0]
    buy = events_named(data, 'Inbound')[0]
    display = events_named(data, 'display')[0]
    assert sell['guards'][-1]['branch'] == 'True'
    assert buy['guards'][-1]['branch'] == 'False'
    assert data['nodes'][sell['node']]['eid'] == 'rule-outbound-001'
    assert data['nodes'][buy['node']]['eid'] == 'rule-inbound-001'
    assert {(edge['from'], edge['to']) for edge in data['edges']} >= {
        (sell['id'], display['id']), (buy['id'], display['id']),
    }
    unresolved = {item['field']['path'] for item in data['inputs']
                  if item['status'] == 'external_or_unresolved'}
    assert {ORDER_NATURE, BUY_CURRENCY, SELL_CURRENCY} <= unresolved
    assert all(not item['direct_source_api_events'] for item in data['conclusions']['anchors'])
    assert data['conclusions']['overall_status'] == 'blocked'
    summary = logic_summary(data)
    assert f'配置值：`Outbound $${SELL_CURRENCY}$`' in summary
    assert f'配置值：`Inbound $${BUY_CURRENCY}$`' in summary
    assert '未从锚点沿数据依赖边找到 API 来源' in summary
