import json

import pytest

from ifp_contract.config import load_rule_config
from ifp_contract.extractor import scan_integrator


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
