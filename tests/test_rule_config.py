import json

import pytest

from ifp_contract.config import load_rule_config
from ifp_contract.extractor import scan_integrator
from ifp_contract.trace import Trace


def test_project_method_mapping_fallback_and_attribute_precedence(tmp_path):
    config_path = tmp_path / "rules.json"
    config_path.write_text(json.dumps({
        "api_rule_classes": ["com.temenosconnect.odata.rule.ReadRule"],
        "method_by_rule_class_suffix": {"ReadRule": " get "},
        "attribute_aliases": {"method": ["Verb"]},
    }))
    config = load_rule_config(config_path)
    integrator = tmp_path / "DataIntegrator.ifp"
    integrator.write_text('''<Project>
        <Rule RuleClassName="com.temenosconnect.odata.rule.ReadRule" />
        <Rule RuleClassName="ReadRule" Verb="PATCH" VendorMethod="POST" />
        <Rule RuleClassName="ReadRule" VendorMethod="POST" />
        <Rule RuleClassName="ReadRule" HTTPMethod="DELETE" />
        <Rule RuleClassName="InvokeIRISRule" />
    </Project>''')

    operations = scan_integrator(integrator, config).operations
    assert [row["action"] for row in operations] == ["GET", "PATCH", "POST", "DELETE", None]
    assert all(row["classification"] == "CONFIRMED" for row in operations)
    assert config.method_for_rule_class("vendor.READRULE") == "GET"
    assert config.method_for_rule_class("OtherReadRule") is None
    assert config.method_for_rule_class(None) is None
    assert load_rule_config(None).method_for_rule_class("ReadRule") is None

    previous_signature = config.signature_payload()
    payload = json.loads(config_path.read_text())
    payload["method_by_rule_class_suffix"] = {"vendor.ReadRule": "POST"}
    config_path.write_text(json.dumps(payload))
    updated = load_rule_config(config_path)
    assert updated.signature_payload() != previous_signature
    assert scan_integrator(integrator, updated).operations[0]["action"] == "POST"


@pytest.mark.parametrize("mapping", [[], None, {"ReadRule": 3}, {"ReadRule": " "}, {"": "GET"}])
def test_invalid_method_mapping_is_rejected(tmp_path, mapping):
    config_path = tmp_path / "rules.json"
    config_path.write_text(json.dumps({"method_by_rule_class_suffix": mapping}))
    with pytest.raises(ValueError, match="method_by_rule_class_suffix"):
        load_rule_config(config_path)


def test_exact_api_classes_methods_and_attributes_do_not_leak_between_packages(tmp_path):
    config_path = tmp_path / "rules.json"
    config_path.write_text(json.dumps({
        "api_rule_classes_exact": ["vendor_a.FetchRule", "vendor_b.FetchRule"],
        "method_by_rule_class": {
            "vendor_a.FetchRule": "GET",
            "vendor_b.FetchRule": "POST",
        },
        "api_rule_attributes_exact": {
            "vendor_a.FetchRule": {"path": ["ReadPath"]},
            "vendor_b.FetchRule": {"path": ["WritePath"]},
        },
    }))
    config = load_rule_config(config_path)

    assert config.is_api_rule("vendor_a.FetchRule")
    assert config.is_api_rule("vendor_b.FetchRule")
    assert not config.is_api_rule("vendor_c.FetchRule")
    assert config.method_for_rule_class("vendor_a.FetchRule") == "GET"
    assert config.method_for_rule_class("vendor_b.FetchRule") == "POST"
    assert config.attribute_names_for_rule("vendor_a.FetchRule", "path")[-1] == "ReadPath"
    assert "WritePath" not in config.attribute_names_for_rule("vendor_a.FetchRule", "path")

    integrator = tmp_path / "DataIntegrator.ifp"
    integrator.write_text('''<Project>
      <Rule RuleClassName="vendor_a.FetchRule" ReadPath="/read" Output="Response" />
      <Rule RuleClassName="vendor_b.FetchRule" WritePath="/write" Output="Response" />
      <Rule RuleClassName="vendor_c.FetchRule" ReadPath="/other" />
    </Project>''')
    operations = scan_integrator(integrator, config).operations
    assert [(row["rule_class"], row["action"], row["api_path"]) for row in operations] == [
        ("vendor_a.FetchRule", "GET", "/read"),
        ("vendor_b.FetchRule", "POST", "/write"),
    ]
    data = Trace(tmp_path, rules_config=config_path).collect(
        integrator.name, 'Response.Value', None, None)
    api_events = [event for event in data['events'] if event['kind'] == 'api']
    assert [event['api']['method'] for event in api_events] == ['GET', 'POST']
    assert all(event['api']['config_resolution']['rule_class']['match'] == 'exact'
               for event in api_events)
    assert all(event['api']['config_resolution']['method']['match'] == 'exact'
               for event in api_events)


def test_legacy_suffix_api_configuration_remains_suffix_scoped(tmp_path):
    config_path = tmp_path / "rules.json"
    config_path.write_text(json.dumps({
        "api_rule_classes": ["legacy.package.FetchRule"],
        "method_by_rule_class_suffix": {"legacy.package.FetchRule": "GET"},
    }))
    config = load_rule_config(config_path)
    assert config.is_api_rule("another.package.FetchRule")
    assert config.method_resolution("another.package.FetchRule") == {
        "method": "GET", "match": "suffix", "key": "fetchrule"
    }


def test_duplicate_normalized_suffix_mapping_is_rejected(tmp_path):
    config_path = tmp_path / "rules.json"
    config_path.write_text(json.dumps({
        "method_by_rule_class_suffix": {
            "vendor_a.FetchRule": "GET",
            "vendor_b.FetchRule": "POST",
        }
    }))
    with pytest.raises(ValueError, match="duplicate normalized"):
        load_rule_config(config_path)


def test_attribute_override_has_explicit_precedence(tmp_path):
    config_path = tmp_path / "rules.json"
    config_path.write_text(json.dumps({
        "api_rule_classes_exact": ["vendor.FetchRule"],
        "api_rule_attribute_overrides_exact": {
            "vendor.FetchRule": {"method": ["VendorVerb"]}
        },
    }))
    config = load_rule_config(config_path)
    integrator = tmp_path / "DataIntegrator.ifp"
    integrator.write_text('''<Project><Rule RuleClassName="vendor.FetchRule"
      HTTPMethod="GET" VendorVerb="PATCH" ResourcePath="/items"/></Project>''')
    operation = scan_integrator(integrator, config).operations[0]
    assert operation["action"] == "PATCH"


def test_api_and_non_api_suffix_conflict_is_rejected_while_loading(tmp_path):
    config_path = tmp_path / 'rules.json'
    config_path.write_text(json.dumps(
        {'api_rule_classes': ['SharedRule'], 'non_api_rule_classes': ['SharedRule']}))
    with pytest.raises(ValueError, match='both API and non-API'):
        load_rule_config(config_path)


def test_exact_api_and_standard_trace_semantics_conflict_is_rejected(tmp_path):
    config_path = tmp_path / 'rules.json'
    config_path.write_text(json.dumps(
        {'api_rule_classes_exact': ['vendor.SetValueRule']}))
    load_rule_config(config_path)
    with pytest.raises(ValueError, match='Conflicting API and trace semantics'):
        Trace(tmp_path, rules_config=config_path)


def test_duplicate_api_class_suffixes_are_rejected(tmp_path):
    config_path = tmp_path / 'rules.json'
    config_path.write_text(json.dumps(
        {'api_rule_classes': ['vendor_a.FetchRule', 'vendor_b.FetchRule']}))
    with pytest.raises(ValueError, match='duplicate normalized'):
        load_rule_config(config_path)


def test_exact_method_precedes_suffix_fallback(tmp_path):
    config_path = tmp_path / 'rules.json'
    config_path.write_text(json.dumps({
        'api_rule_classes': ['FetchRule'],
        'api_rule_classes_exact': ['vendor_a.FetchRule'],
        'method_by_rule_class_suffix': {'FetchRule': 'DELETE'},
        'method_by_rule_class': {'vendor_a.FetchRule': 'GET'},
    }))
    config = load_rule_config(config_path)
    assert config.method_resolution('vendor_a.FetchRule')['match'] == 'exact'
    assert config.method_for_rule_class('vendor_a.FetchRule') == 'GET'
    assert config.method_resolution('vendor_b.FetchRule')['match'] == 'suffix'
    assert config.method_for_rule_class('vendor_b.FetchRule') == 'DELETE'


@pytest.mark.parametrize('payload', [
    {'api_rule_classes_exact': ['FetchRule']},
    {'method_by_rule_class': {'FetchRule': 'GET'}},
    {'api_rule_attributes_exact': {'FetchRule': {'path': ['Route']}}},
    {'api_rule_attribute_overrides_exact': {'FetchRule': {'path': ['Route']}}},
])
def test_exact_api_configuration_requires_qualified_classes(tmp_path, payload):
    config_path = tmp_path / 'rules.json'
    config_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='qualified class names'):
        load_rule_config(config_path)
