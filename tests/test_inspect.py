import json

from ifp_contract.cli import main
from ifp_contract.config import load_rule_config


def test_inspect_exports_compatibility_bundle_without_guessing_unknown_rules(tmp_path):
    (tmp_path / 'Child.ifp').write_text(
        '<Project><Product Name="Child"><Phase Name="Start"/></Product></Project>')
    (tmp_path / 'Screen.ifp').write_text('''<Project><Product Name="Demo"><Phase Name="Start">
      <Rule Name="known" RuleClassName="SetValueRule" PropertyName="Target" FromValue="1"/>
      <Rule Name="gate" RuleClassName="EvaluateRule" Expression="Form[1].Mode == 'Ready'">
        <Rule Name="maybe" RuleClassName="SetValueRule" PropertyName="Target" FromValue="2" RuleType="yes"/>
      </Rule>
      <Rule Name="call" RuleClassName="CallComponentRule" SelectComponent="$$LIBRARY_HOME$\\Child.ifp" ComponentList="Child"/>
      <Rule Name="api" RuleClassName="vendor.FetchRule" Route="/items"/>
      <Rule Name="opaque" RuleClassName="vendor.ScriptRule" Script="doSomething()"/>
    </Phase></Product></Project>''')
    config = tmp_path / 'rules.json'
    config.write_text(json.dumps({
        'api_rule_classes_exact': ['vendor.FetchRule'],
        'method_by_rule_class': {'vendor.FetchRule': 'GET'},
        'api_rule_attributes_exact': {'vendor.FetchRule': {'path': ['Route']}},
    }))
    output = tmp_path / 'inspection'

    assert main(['inspect', str(tmp_path), '--rules-config', str(config),
                 '--path-var', 'LIBRARY_HOME=.', '--output', str(output), '--quiet']) == 0
    assert {path.name for path in output.iterdir()} == {
        'compatibility.md', 'compatibility.json', 'rules-config.draft.json', 'adaptation-todo.md'
    }
    data = json.loads((output / 'compatibility.json').read_text())
    assert data['coverage']['files_scanned'] == 2
    assert data['coverage']['rules'] == 6
    assert data['coverage']['unknown_rules'] == 1
    findings = {item['code']: item for item in data['findings']}
    assert findings['UNKNOWN_RULE']['samples'][0]['rule_class'] == 'vendor.ScriptRule'
    assert findings['UNKNOWN_BRANCH']['samples'][0]['label'] == 'yes'
    assert 'DYNAMIC_COMPONENT' not in findings
    assert json.loads((output / 'rules-config.draft.json').read_text()) == json.loads(config.read_text())
    draft = json.loads((output / 'rules-config.draft.json').read_text())
    assert 'vendor.ScriptRule' not in json.dumps(draft)
    load_rule_config(output / 'rules-config.draft.json')
    assert 'UNKNOWN_RULE' in (output / 'adaptation-todo.md').read_text()


def test_inspect_retains_partial_results_and_reports_file_limit(tmp_path):
    for name in ('A.ifp', 'B.ifp'):
        (tmp_path / name).write_text(
            '<Project><Rule RuleClassName="SetValueRule" PropertyName="X" FromValue="1"/></Project>')
    output = tmp_path / 'inspection'
    assert main(['inspect', str(tmp_path), '--max-files', '1', '--output', str(output), '--quiet']) == 0
    data = json.loads((output / 'compatibility.json').read_text())
    assert data['coverage']['files_discovered'] == 2
    assert data['coverage']['files_scanned'] == 1
    assert next(item for item in data['findings'] if item['code'] == 'SCAN_FILE_LIMIT')['count'] == 1


def test_inspect_rejects_its_root_as_output(tmp_path):
    (tmp_path / 'A.ifp').write_text('<Project/>')
    assert main(['inspect', str(tmp_path), '--output', str(tmp_path), '--quiet']) == 1


def test_inspect_keeps_good_file_evidence_when_another_file_fails(tmp_path):
    (tmp_path / 'A.ifp').write_text(
        '<Project><Rule RuleClassName="SetValueRule" PropertyName="X" FromValue="1"/></Project>')
    (tmp_path / 'B.ifp').write_text('<Project><Rule Name="broken"')
    output = tmp_path / 'inspection'
    assert main(['inspect', str(tmp_path), '--output', str(output), '--quiet']) == 1
    data = json.loads((output / 'compatibility.json').read_text())
    assert data['coverage']['files_scanned'] == 1
    assert data['coverage']['files_failed'] == 1
    assert data['coverage']['rules'] == 1
    assert next(item for item in data['findings'] if item['code'] == 'FILE_ERROR')['count'] == 1


def test_inspect_validates_link_targets_without_requiring_link_attributes(tmp_path):
    (tmp_path / 'A.ifp').write_text('''<Project><Rules>
      <Rule Name="target" eid="shared" RuleClassName="SetValueRule" PropertyName="X" FromValue="1"/>
      <Rule Name="duplicate" eid="shared" RuleClassName="SetValueRule" PropertyName="Y" FromValue="2"/>
    </Rules><Product Name="P"><Phase Name="I">
      <Rule Name="link" LinkReference="shared"/>
      <Rule Name="missing" LinkReference="absent"/>
    </Phase></Product></Project>''')
    output = tmp_path / 'inspection'
    assert main(['inspect', str(tmp_path), '--output', str(output), '--quiet']) == 0
    data = json.loads((output / 'compatibility.json').read_text())
    findings = {item['code']: item for item in data['findings']}
    assert findings['LINK_NOT_UNIQUE']['count'] == 1
    assert findings['LINK_NOT_FOUND']['count'] == 1
    assert 'MISSING_EXPRESSION' not in findings


def test_inspect_honors_explicit_selector_mapping_and_parent_disabled(tmp_path):
    (tmp_path / 'Child.ifp').write_text('<Project/>')
    (tmp_path / 'A.ifp').write_text('''<Project><Product Name="P"><Phase Name="I">
      <Rule Name="parent" RuleClassName="ContainerRule" DisabledFlag="yes">
        <Rule Name="call" RuleClassName="CallComponentRule" LegacySelector="Child.ifp"/>
      </Rule>
    </Phase></Product></Project>''')
    config = tmp_path / 'rules.json'
    config.write_text(json.dumps({'trace': {'rules': [
        {'class': 'ContainerRule', 'kind': 'container', 'override': True,
         'attributes': {'RuleDisabled': 'DisabledFlag'}},
        {'class': 'CallComponentRule', 'kind': 'call', 'override': True,
         'attributes': {'SelectComponent': 'MappedSelector'}},
    ]}}))
    output = tmp_path / 'inspection'
    assert main(['inspect', str(tmp_path), '--rules-config', str(config),
                 '--output', str(output), '--quiet']) == 0
    data = json.loads((output / 'compatibility.json').read_text())
    assert data['coverage']['disabled_rules'] == 2
    findings = {item['code']: item for item in data['findings']}
    assert findings['MISSING_COMPONENT_SELECTOR']['count'] == 1
    assert 'COMPONENT_NOT_FOUND' not in findings


def test_inspect_applies_selector_defaults_and_explicit_mappings(tmp_path):
    (tmp_path / 'Child.ifp').write_text('<Project/>')
    (tmp_path / 'A.ifp').write_text('''<Project><Product Name="P"><Phase Name="I">
      <Rule Name="default" RuleClassName="CallComponentRule"/>
      <Rule Name="mapped" RuleClassName="ContainerRule" DisabledFlag=" YES "/>
    </Phase></Product></Project>''')
    config = tmp_path / 'rules.json'
    config.write_text(json.dumps({'trace': {'rules': [
        {'class': 'CallComponentRule', 'kind': 'call', 'override': True,
         'defaults': {'SelectComponent': 'Child.ifp'}},
        {'class': 'ContainerRule', 'kind': 'container', 'override': True,
         'attributes': {'RuleDisabled': 'DisabledFlag'}},
    ]}}))
    output = tmp_path / 'inspection'
    assert main(['inspect', str(tmp_path), '--rules-config', str(config),
                 '--output', str(output), '--quiet']) == 0
    data = json.loads((output / 'compatibility.json').read_text())
    assert data['coverage']['disabled_rules'] == 1
    findings = {item['code']: item for item in data['findings']}
    assert 'MISSING_COMPONENT_SELECTOR' not in findings


def test_inspect_link_api_does_not_require_api_method(tmp_path):
    (tmp_path / 'A.ifp').write_text('''<Project><Rules>
      <Rule Name="target" eid="api" RuleClassName="InvokeIRISRule" HTTPMethod="GET"/>
    </Rules><Product Name="P"><Phase Name="I">
      <Rule Name="link" LinkReference="api" RuleClassName="InvokeIRISRule"/>
    </Phase></Product></Project>''')
    output = tmp_path / 'inspection'
    assert main(['inspect', str(tmp_path), '--output', str(output), '--quiet']) == 0
    data = json.loads((output / 'compatibility.json').read_text())
    findings = {item['code']: item for item in data['findings']}
    assert 'MISSING_API_METHOD' not in findings


def test_inspect_link_call_class_type_skips_selector_but_checks_target(tmp_path):
    (tmp_path / 'Child.ifp').write_text('<Project/>')
    (tmp_path / 'A.ifp').write_text('''<Project><Rules>
      <Rule Name="target" eid="shared" ClassType="CallComponentRule" SelectComponent="Child.ifp"/>
    </Rules><Product Name="P"><Phase Name="I">
      <Rule Name="link" LinkReference="shared" ClassType="CallComponentRule"/>
    </Phase></Product></Project>''')
    output = tmp_path / 'inspection'
    assert main(['inspect', str(tmp_path), '--output', str(output), '--quiet']) == 0
    data = json.loads((output / 'compatibility.json').read_text())
    findings = {item['code']: item for item in data['findings']}
    assert 'MISSING_COMPONENT_SELECTOR' not in findings
    assert data['coverage']['rules'] == 2


def test_inspect_reports_directory_enumeration_failure(monkeypatch, tmp_path):
    from ifp_contract import inspect as inspect_module

    def failing_walk(root, onerror):
        onerror(OSError(13, 'permission denied', str(root / 'private')))
        return
        yield  # make this a generator

    monkeypatch.setattr(inspect_module.os, 'walk', failing_walk)
    output_dir = tmp_path / 'inspection'
    assert inspect_module.run_inspect(type('Args', (), {
        'path_var': [], 'output': output_dir, 'root': tmp_path,
        'quiet': True, 'rules_config': None, 'max_files': 10000, 'max_samples': 5,
    })()) == 1
    output = json.loads((output_dir / 'compatibility.json').read_text())
    finding = next(item for item in output['findings'] if item['code'] == 'FILE_DISCOVERY_ERROR')
    assert finding['count'] == 1
