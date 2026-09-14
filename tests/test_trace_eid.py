"""CLI contract tests for locating a Question by its node eid."""

import json

from ifp_contract.cli import main


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def screen(question_eid="question-001", result="Result"):
    return f'''<Project>
      <Product Name="OrderScreen" InitialPhase="OrderScreen.Input">
        <Phase Name="Input">
          <Question Name="Result display" QuestionText="Result label"
                    PropertyKey="{result}" ReadOnly="Y" eid="{question_eid}"/>
          <Rule Name="set result" RuleClassName="SetValueRule"
                PropertyName="{result}" FromValue="ready"/>
        </Phase>
      </Product>
    </Project>'''


def test_eid_locates_question_without_file_or_field(tmp_path):
    put(tmp_path, "Screens/OrderScreen.ifp", screen())
    output = tmp_path / "bundle"

    assert main(["trace", str(tmp_path), "--eid", "question-001",
                 "--output", str(output)]) == 0

    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["target"]["file"] == "Screens/OrderScreen.ifp"
    assert evidence["target"]["rule_eid"] == "question-001"
    assert evidence["target"]["entry"] == "OrderScreen.Input"
    assert any(event["node"] in evidence["nodes"] and
               evidence["nodes"][event["node"]].get("eid") == "question-001"
               for event in evidence["events"])


def test_eid_can_be_combined_with_explicit_library_path_variable(tmp_path):
    put(tmp_path, "Screens/OrderScreen.ifp", '''<Project><Product Name="OrderScreen">
      <Phase Name="Input">
        <Rule Name="lookup" RuleClassName="CallComponentRule"
              SelectComponent="$$SHARED_LIBRARY$/Lookup.ifp" ComponentList="Lookup"
              result_ClassType="ComponentMapping" result_PropertyKey="Response"
              result_SolutionDataItemMapping="Result" result_Out="Y"/>
        <Question Name="Result display" eid="question-001" PropertyKey="Result" ReadOnly="Y"/>
      </Phase></Product></Project>''')
    put(tmp_path, "Shared/Lookup.ifp", '''<Project><Product Name="Lookup"><Phase Name="Init">
      <Rule Name="reference source" RuleClassName="SwaggerIntegrationRule"
            HTTPMethod="GET" ResourcePath="/reference/result" Output="Response"/>
    </Phase></Product></Project>''')
    output = tmp_path / "bundle"

    assert main(["trace", str(tmp_path), "--eid", "question-001",
                 "--path-var", "SHARED_LIBRARY=Shared",
                 "--output", str(output)]) == 0

    followup = json.loads((output / "next.json").read_text(encoding="utf-8"))
    assert followup["path_variables"] == {"SHARED_LIBRARY": "Shared"}
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence['conclusions']['anchors'][0]['direct_source_api_events']
    assert any(event.get('api', {}).get('path') == '/reference/result'
               for event in evidence['events'])
    again = tmp_path / 'again'
    assert main(['trace', str(tmp_path), '--request', str(output / 'next.json'),
                 '--output', str(again)]) == 0
    replayed = json.loads((again / 'evidence.json').read_text(encoding='utf-8'))
    assert replayed['events'] == evidence['events']


def test_duplicate_eid_requires_file_disambiguation(tmp_path, capsys):
    put(tmp_path, "First.ifp", screen())
    put(tmp_path, "Second.ifp", screen())

    assert main(["trace", str(tmp_path), "--eid", "question-001",
                 "--output", str(tmp_path / "bundle")]) == 1

    error = capsys.readouterr().err
    assert "First.ifp" in error
    assert "Second.ifp" in error
    assert "--file" in error


def test_missing_eid_reports_searchable_error(tmp_path, capsys):
    put(tmp_path, "OrderScreen.ifp", screen())

    assert main(["trace", str(tmp_path), "--eid", "question-missing",
                 "--output", str(tmp_path / "bundle")]) == 1

    assert "question-missing" in capsys.readouterr().err


def test_eid_with_file_uses_file_as_a_disambiguating_scope(tmp_path):
    put(tmp_path, "First.ifp", screen())
    put(tmp_path, "Second.ifp", screen())
    output = tmp_path / "bundle"

    assert main(["trace", str(tmp_path), "--file", "Second.ifp",
                 "--eid", "question-001", "--output", str(output)]) == 0

    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["target"]["file"] == "Second.ifp"


def test_eid_uses_incrementing_default_output_directory(tmp_path, monkeypatch):
    put(tmp_path, "OrderScreen.ifp", screen())
    monkeypatch.chdir(tmp_path)

    assert main(["trace", str(tmp_path), "--eid", "question-001"]) == 0
    assert main(["trace", str(tmp_path), "--eid", "question-001"]) == 0
    assert (tmp_path / "trace-output" / "evidence.json").is_file()
    assert (tmp_path / "trace-output-2" / "evidence.json").is_file()


def test_rule_eid_inside_phase_keeps_enclosing_branch_guard(tmp_path):
    put(tmp_path, "OrderScreen.ifp", '''<Project>
      <Product Name="OrderScreen"><Phase Name="Input">
        <Rule Name="check mode" RuleClassName="EvaluateRule"
              Expression="$$Mode$ == 'active'">
          <Rule Name="set result" eid="rule-001" RuleType="True"
                RuleClassName="SetValueRule" PropertyName="Result" FromValue="ready"/>
        </Rule>
      </Phase></Product>
    </Project>''')
    output = tmp_path / "bundle"

    assert main(["trace", str(tmp_path), "--eid", "rule-001",
                 "--output", str(output)]) == 0
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    target = evidence["target"]
    assert target["file"] == "OrderScreen.ifp"
    assert target["entry"] == "OrderScreen.Input"
    selected = next(event for event in evidence["events"]
                    if evidence["nodes"][event["node"]].get("eid") == "rule-001")
    assert selected["guards"][-1]["branch"] == "True"


def test_readonly_question_eid_follows_product_level_writer(tmp_path):
    put(tmp_path, "OrderScreen.ifp", '''<Project>
      <Product Name="OrderScreen">
        <Rule Name="set result" eid="writer-001" RuleClassName="SetValueRule"
              PropertyName="Result" FromValue="ready"/>
        <Phase Name="Input"><Question Name="Result display" eid="question-001"
              QuestionText="Result label" PropertyKey="Result" ReadOnly="Y"/></Phase>
      </Product>
    </Project>''')
    output = tmp_path / "bundle"

    assert main(["trace", str(tmp_path), "--eid", "question-001",
                 "--output", str(output)]) == 0
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert any(evidence["nodes"][event["node"]].get("eid") == "writer-001"
               for event in evidence["events"])


def test_shared_rule_eid_keeps_parent_condition(tmp_path):
    put(tmp_path, 'Rules.ifp', '''<Project><Product Name="Rules">
      <Rule Name="condition" eid="guard-001" RuleClassName="EvaluateRule"
            Expression="$$Mode$ == 'active'">
        <Rule Name="assign" eid="rule-001" RuleClassName="SetValueRule"
              PropertyName="Result" FromValue="ready" RuleType="True"/>
      </Rule></Product></Project>''')
    output = tmp_path / 'bundle'
    assert main(['trace', str(tmp_path), '--eid', 'rule-001', '--output', str(output)]) == 0
    evidence = json.loads((output / 'evidence.json').read_text())
    assert evidence['target']['entry'] == '@guard-001'
    anchor = next(e for e in evidence['events'] if e['id'] in evidence['seeds'])
    assert anchor['guards'][-1]['branch'] == 'True'


def test_editable_question_eid_keeps_input_semantics(tmp_path):
    put(tmp_path, 'Screen.ifp', screen().replace('ReadOnly="Y"', 'ReadOnly="N"'))
    output = tmp_path / 'bundle'
    assert main(['trace', str(tmp_path), '--eid', 'question-001', '--output', str(output)]) == 0
    evidence = json.loads((output / 'evidence.json').read_text())
    anchor = next(e for e in evidence['events'] if e['id'] in evidence['seeds'])
    assert anchor['kind'] == 'ui_input'
    assert anchor['writes'][0]['path'] == 'Result'
    assert 'display_field' not in anchor


def test_eid_locator_ignores_comments_and_handles_utf16(tmp_path):
    put(tmp_path, "False.ifp", '''<Project><!-- eid="question-001" -->
      <Product Name="Other"><Phase Name="Input"><Question eid="wrong-001"
      PropertyKey="Ignored" ReadOnly="Y"/></Phase></Product></Project>''')
    content = screen().encode("utf-16")
    (tmp_path / "OrderScreen.ifp").write_bytes(content)
    output = tmp_path / "bundle"

    assert main(["trace", str(tmp_path), "--eid", "question-001",
                 "--output", str(output)]) == 0
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["target"]["file"] == "OrderScreen.ifp"
