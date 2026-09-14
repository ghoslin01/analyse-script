from ifp_contract.trace_partial import partial_rule_evidence


def test_add_to_list_uses_declared_modes_and_does_not_promote_stale_properties():
    result = partial_rule_evidence("com.acquire.intelligentforms.rules.AddToListRule", {
        "ListType": "DYNAMIC", "DynamicListToAddTo": "Currency List", "StaticListToAddTo": "Stale List",
        "KeyType": "Data Item", "KeyPropertyName": "Items[C].Id",
        "ValueType": "Value", "Value": "$$Accounts[C].No$ / $$Accounts[C].Name$",
        "PropertyName": "STALE.Must.Not.Be.Read",
        "ErrorCodeDataItem": "Errors[1].Code",
    })
    assert result["read_paths"] == ["Items[C].Id", "Accounts[C].No", "Accounts[C].Name"]
    assert result["write_paths"] == ["Errors[1].Code"]
    assert result["evidence"]["list_target"]["name"] == "Currency List"
    assert not result["evidence"]["list_target_is_datastore_write"]


def test_compare_preserves_ambiguous_target_without_fabricating_write_or_result():
    result = partial_rule_evidence("CompareMultiListValuesRule", {
        "CurrentListValue": "Items[C].Current", "NewListValues": "Items[C].New",
        "CompareValues": "N", "ValueSeparator": "|",
        "PropertyName": "STALE.Result",
    })
    assert result["read_paths"] == ["Items[C].Current"]
    assert result["write_paths"] == []
    assert result["evidence"]["new_list_values_role"] == "ambiguous"
    assert not result["evidence"]["new_list_values_write_proven"]


def test_question_status_keeps_multiple_ui_targets_and_raw_flags():
    result = partial_rule_evidence("com.acquire.intelligentforms.rules.SetQuestionStatus", {
        "Question": "Screen Question 1,Screen FormButton 2",
        "ReadOnly": "Y", "ChangeReadOnlyStatus": "N", "Mandatory": "N",
        "ChangeMandatoryStatus": "N", "RuleType": "False", "RuleDisabled": "N",
    })
    assert result["read_paths"] == []
    assert result["write_paths"] == []
    assert result["evidence"]["ui_targets"] == ["Screen Question 1", "Screen FormButton 2"]
    assert result["evidence"]["status_flags"]["ChangeReadOnlyStatus"] == "N"
    assert not result["evidence"]["read_only_effect_proven"]
    assert not result["evidence"]["mandatory_effect_proven"]


def test_unknown_rules_are_left_to_existing_unknown_boundary():
    assert partial_rule_evidence("com.acquire.intelligentforms.rules.SetValueRule", {}) is None
