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
