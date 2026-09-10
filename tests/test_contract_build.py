from __future__ import annotations

from ifp_contract.cli import main
from ifp_contract.extractor import build_contracts
from ifp_contract.store import ContractStore


def test_build_keeps_only_direct_odata_contracts_and_caller_mappings(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "WraDataIntegrator.ifp"
    integrator.write_text(
        """<Project>
  <Rule eid="odata-1" Name="Get customer" RuleClassName="InvokeIRISRule"
    IRISSource="CustomerOData" IRISAction="GET" Endpoint="/odata/v1/Customers"
    Filter="Status eq 'Open'" QueryInputDataGroup="Request.CustomerId"
    TargetDataGroup="Request" ResultsDataGroup="Response.Customers" OutputDataGroup="Response" />
  <Rule eid="not-api" Name="local step" RuleClassName="AssignRule" />
</Project>""",
        encoding="utf-8",
    )
    (corpus / "Caller.ifp").write_text(
        """<Project>
  <Rule eid="caller-1" Name="Load customer" RuleClassName="CallComponentRule"
    SelectComponent="Integration/WraDataIntegrator.ifp" Source="Customer Lookup"
    Request_ClassType="ComponentMapping"
    Request_SolutionDataItemMapping="Screen.CustomerId"
    Request_PropertyKey="Request.CustomerId" Request_In="true"
    Result_ClassType="ComponentMapping"
    Result_SolutionDataItemMapping="Screen.CustomerName"
    Result_PropertyKey="Response.Customers[0].Name" Result_Out="true" />
  <Rule eid="not-a-call" RuleClassName="AssignRule">WraDataIntegrator.ifp</Rule>
</Project>""",
        encoding="utf-8",
    )
    # This deliberately large attribute models a screen/property payload.  It
    # has no DI reference and must not create a database row or XML object tree.
    (corpus / "Unrelated.ifp").write_text(
        '<Project><Blob Value="' + "x" * 2_000_000 + '" /></Project>', encoding="utf-8"
    )
    database = tmp_path / "contracts.db"
    store = ContractStore(database)
    try:
        summary = build_contracts(corpus, integrator, "WraDataIntegrator.ifp", store)
        assert summary.files_examined == 2
        assert summary.referencing_files == 1
        assert summary.odata_operations == 1
        assert summary.caller_references == 1
        assert summary.caller_mappings == 2
        assert store.counts() == {
            "odata_operations": 1,
            "caller_references": 1,
            "caller_mappings": 2,
        }
        operation = store.rows("SELECT api_path, filter_expr FROM odata_operations")[0]
        assert dict(operation) == {
            "api_path": "/odata/v1/Customers",
            "filter_expr": "Status eq 'Open'",
        }
        mappings = store.rows(
            "SELECT solution_data_item, property_key, direction FROM caller_mappings ORDER BY mapping_prefix"
        )
        assert [tuple(row) for row in mappings] == [
            ("Screen.CustomerId", "Request.CustomerId", "INPUT"),
            ("Screen.CustomerName", "Response.Customers[0].Name", "OUTPUT"),
        ]
    finally:
        store.close()
    # A two-megabyte irrelevant XML value does not turn into a graph/index DB.
    assert database.stat().st_size < 100_000


def test_cli_writes_markdown_report(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text(
        '<Project><Rule Name="Search" RuleClassName="SwaggerIntegrationRule" '
        'Source="People API" HttpMethod="GET" Path="/people" /></Project>',
        encoding="utf-8",
    )
    (corpus / "caller.ifp").write_text(
        '<Project><Rule Name="Caller" RuleClassName="CallComponentRule" '
        'SelectComponent="DataIntegrator.ifp" /></Project>',
        encoding="utf-8",
    )
    database = tmp_path / "contracts.db"
    report = tmp_path / "detail.md"
    assert main(
        [
            "build",
            str(corpus),
            "--integrator",
            str(integrator),
            "--reference",
            "DataIntegrator.ifp",
            "--db",
            str(database),
        ]
    ) == 0
    assert main(["report", "--db", str(database), "--output", str(report)]) == 0
    content = report.read_text(encoding="utf-8")
    assert "## API / OData requests" in content
    assert "`/people`" in content
    assert "## Direct Data Integrator callers" in content


def test_unknown_rules_and_real_data_source_variants_are_preserved(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text(
        """<Project>
  <DataSource Name="CustomerSource" ClassType="vendor.ODataSource"
    ServiceRootUri="https://api.example.test/odata" />
  <Rule Name="Known IRIS" RuleClassName="com.temenos.InvokeIRISRule"
    IRISSource="CustomerSource" IRISAction="LIST" ResourcePath="Customers"
    ResultsDataGroup="Response.Customers" />
  <Rule Name="Custom HTTP" RuleClassName="vendor.CustomRestRule"
    SourceName="CustomerSource" HTTPMethod="POST" ResourcePath="/Customers/search"
    Output="Response.Search" />
  <Rule Name="Future" RuleClassName="vendor.FutureRule" />
</Project>""",
        encoding="utf-8",
    )
    (corpus / "unknown-callers.ifp").write_text(
        """<Project>
  <Rule Name="Custom selector" RuleClassName="vendor.CustomDispatchRule"
    SelectComponent="DataIntegrator.ifp" />
  <Rule Name="Custom attribute" RuleClassName="vendor.CustomDispatchRule"
    TargetModule="DataIntegrator.ifp" />
</Project>""",
        encoding="utf-8",
    )
    database = tmp_path / "contracts.db"
    store = ContractStore(database)
    try:
        summary = build_contracts(corpus, integrator, "DataIntegrator.ifp", store)
        assert summary.data_sources == 1
        assert summary.odata_operations == 2
        assert summary.caller_references == 2
        operations = store.rows(
            "SELECT rule_name, classification, base_url, api_path FROM odata_operations ORDER BY tag_offset"
        )
        assert [tuple(row) for row in operations] == [
            ("Known IRIS", "CONFIRMED", "https://api.example.test/odata", "Customers"),
            ("Custom HTTP", "STRUCTURAL", "https://api.example.test/odata", "/Customers/search"),
        ]
        callers = store.rows(
            "SELECT rule_name, classification FROM caller_references ORDER BY tag_offset"
        )
        assert [tuple(row) for row in callers] == [
            ("Custom selector", "CONFIRMED_UNKNOWN_RULE"),
            ("Custom attribute", "UNCLASSIFIED_REFERENCE"),
        ]
        codes = {row["code"] for row in store.rows("SELECT code FROM diagnostics")}
        assert {
            "UNKNOWN_API_RULE_CLASS",
            "UNKNOWN_RULE_CLASS",
            "UNKNOWN_CALL_RULE_CLASS",
            "UNCLASSIFIED_REFERENCE_RULE",
        } <= codes
    finally:
        store.close()


def test_strict_mode_returns_two_for_unknown_evidence(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text(
        '<Project><Rule RuleClassName="vendor.FutureRule" /></Project>', encoding="utf-8"
    )
    database = tmp_path / "contracts.db"
    assert main(
        [
            "build", str(corpus), "--integrator", str(integrator),
            "--reference", "DataIntegrator.ifp", "--db", str(database),
            "--strict", "--quiet",
        ]
    ) == 2


def test_utf16_candidate_is_reported_instead_of_silently_missed(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text(
        '<Project><Rule RuleClassName="InvokeIRISRule" ResourcePath="Items" /></Project>',
        encoding="utf-8",
    )
    (corpus / "utf16.ifp").write_text(
        '<Project><Rule SelectComponent="DataIntegrator.ifp" /></Project>',
        encoding="utf-16",
    )
    store = ContractStore(tmp_path / "contracts.db")
    try:
        summary = build_contracts(corpus, integrator, "DataIntegrator.ifp", store)
        assert summary.caller_references == 0
        diagnostic = store.rows(
            "SELECT code, message FROM diagnostics WHERE code = 'FILE_SCAN_FAILED'"
        )[0]
        assert "UTF-16" in diagnostic["message"]
    finally:
        store.close()


def test_reference_defaults_to_integrator_filename(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DefaultIntegrator.ifp"
    integrator.write_text("<Project />", encoding="utf-8")
    (corpus / "caller.ifp").write_text(
        '<Project><Rule RuleClassName="CallComponentRule" '
        'SelectComponent="DefaultIntegrator.ifp" /></Project>',
        encoding="utf-8",
    )
    database = tmp_path / "contracts.db"
    assert main(
        [
            "build", str(corpus), "--integrator", str(integrator),
            "--db", str(database), "--quiet",
        ]
    ) == 0
    store = ContractStore(database)
    try:
        assert store.rows("SELECT selector FROM caller_references")[0]["selector"] == "DefaultIntegrator.ifp"
    finally:
        store.close()
