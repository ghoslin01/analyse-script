from __future__ import annotations

from pathlib import Path
import json
import sqlite3

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
    assert database.stat().st_size < 256_000


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
    assert "Effective API request: `GET /people`" in content
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


def test_utf16_candidate_is_streamed_and_extracted(tmp_path):
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
        assert summary.caller_references == 1
        caller = store.rows("SELECT selector FROM caller_references")[0]
        assert caller["selector"] == "DataIntegrator.ifp"
    finally:
        store.close()


def test_rules_inside_comments_and_cdata_are_not_extracted(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text(
        """<Project>
<!-- <Rule Name="Comment API" RuleClassName="SwaggerIntegrationRule"
  HTTPMethod="GET" ResourcePath="/comment" /> -->
<![CDATA[<Rule Name="CDATA API" RuleClassName="InvokeIRISRule"
  ResourcePath="/cdata" />]]>
<Rule Name="Real API" RuleClassName="InvokeIRISRule" ResourcePath="/real" />
</Project>""",
        encoding="utf-8",
    )
    (corpus / "caller.ifp").write_text(
        """<Project>
<!-- <Rule RuleClassName="CallComponentRule" SelectComponent="DataIntegrator.ifp" /> -->
<![CDATA[<Rule RuleClassName="CallComponentRule" SelectComponent="DataIntegrator.ifp" />]]>
<Rule Name="Real caller" RuleClassName="CallComponentRule"
  SelectComponent="DataIntegrator.ifp" />
</Project>""",
        encoding="utf-8",
    )
    store = ContractStore(tmp_path / "contracts.db")
    try:
        summary = build_contracts(corpus, integrator, "DataIntegrator.ifp", store)
        assert summary.odata_operations == 1
        assert summary.caller_references == 1
        assert store.rows("SELECT rule_name FROM odata_operations")[0]["rule_name"] == "Real API"
        assert store.rows("SELECT rule_name FROM caller_references")[0]["rule_name"] == "Real caller"
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


def test_product_operation_links_and_integrator_exports_are_exact(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text(
        """<Project><Products>
  <Product Name="Lookup" eid="PRODUCT-A">
    <Phase Name="Main"><Rule Name="Lookup API" RuleClassName="SwaggerIntegrationRule"
      HTTPMethod="GET" ResourcePath="/lookup" /></Phase>
  </Product>
  <Product Name="Submit" eid="PRODUCT-B"
    Export0_ClassType="ComponentMapping"
    Export0_ExportedProperty="Response.Status"
    Export0_PropertyKey="Public.Status" Export0_PubOut="Y">
    <Phase Name="Send"><Rule Name="Submit API" RuleClassName="SwaggerIntegrationRule"
      HTTPMethod="POST" ResourcePath="/submit" /></Phase>
  </Product>
</Products></Project>""",
        encoding="utf-8",
    )
    (corpus / "caller.ifp").write_text(
        '<Project><Rule Name="Call submit" RuleClassName="CallComponentRule" '
        'SelectComponent="DataIntegrator.ifp" ComponentList="Submit" /></Project>',
        encoding="utf-8",
    )
    store = ContractStore(tmp_path / "contracts.db")
    try:
        summary = build_contracts(corpus, integrator, "DataIntegrator.ifp", store)
        assert summary.operation_links == 1
        operations = store.rows(
            "SELECT product_name, phase_name, rule_name FROM odata_operations ORDER BY tag_offset"
        )
        assert [tuple(row) for row in operations] == [
            ("Lookup", "Main", "Lookup API"),
            ("Submit", "Send", "Submit API"),
        ]
        exported = store.rows(
            "SELECT product_name, exported_property, property_key, direction FROM integrator_mappings"
        )[0]
        assert tuple(exported) == ("Submit", "Response.Status", "Public.Status", "OUTPUT")
        linked = store.rows(
            """
            SELECT o.product_name, o.rule_name, l.resolution_status
            FROM caller_operation_links l
            JOIN odata_operations o ON o.id = l.operation_id
            """
        )[0]
        assert tuple(linked) == ("Submit", "Submit API", "EXACT_PRODUCT")
    finally:
        store.close()


def test_case_alias_dynamic_reference_and_duplicate_filename_diagnostics(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text("<Project />", encoding="utf-8")
    duplicate = corpus / "duplicate"
    duplicate.mkdir()
    (duplicate / "DataIntegrator.ifp").write_text("<Project />", encoding="utf-8")
    (corpus / "caller.ifp").write_text(
        """<Project>
  <Rule Name="Case caller" RuleClassName="CallComponentRule"
    SelectComponent="DATAINTEGRATOR.IFP" />
  <Rule Name="Dynamic caller" RuleClassName="CallComponentRule"
    SelectComponent="$$RuntimeComponent$" />
</Project>""",
        encoding="utf-8",
    )
    store = ContractStore(tmp_path / "contracts.db")
    try:
        summary = build_contracts(
            corpus,
            integrator,
            "DataIntegrator.ifp",
            store,
            ignore_case=True,
            include_dynamic=True,
        )
        assert summary.caller_references == 1
        codes = [row["code"] for row in store.rows("SELECT code FROM diagnostics")]
        assert "DYNAMIC_REFERENCE_UNRESOLVED" in codes
        assert "AMBIGUOUS_INTEGRATOR_FILENAME" in codes
    finally:
        store.close()


def test_cli_reuses_checkpoint_and_atomically_publishes(tmp_path, capsys):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text("<Project />", encoding="utf-8")
    caller = corpus / "caller.ifp"
    caller.write_text(
        '<Project><Rule RuleClassName="CallComponentRule" '
        'SelectComponent="DataIntegrator.ifp" /></Project>',
        encoding="utf-8",
    )
    database = tmp_path / "contracts.db"
    command = [
        "build", str(corpus), "--integrator", str(integrator),
        "--db", str(database), "--quiet",
    ]
    assert main(command) == 0
    capsys.readouterr()
    assert database.is_file()
    assert not Path(str(database) + ".partial").exists()

    assert main(command) == 0
    output = capsys.readouterr().out
    assert "scanned=0" in output
    assert "cached=1" in output

    caller.write_text(caller.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert main(command) == 0
    output = capsys.readouterr().out
    assert "scanned=1" in output


def test_custom_rule_vocabulary_resolves_api_and_caller(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text(
        '<Project><Product Name="CustomProduct"><Rule Name="Custom API" '
        'RuleClassName="vendor.SpecialApi" Verb="PATCH" Route="/custom" '
        'Reply="Response.Custom" /></Product></Project>',
        encoding="utf-8",
    )
    (corpus / "caller.ifp").write_text(
        '<Project><Rule Name="Custom call" RuleClassName="vendor.SpecialCall" '
        'TargetModule="DataIntegrator.ifp" ComponentList="CustomProduct" /></Project>',
        encoding="utf-8",
    )
    config = tmp_path / "rules.json"
    config.write_text(
        json.dumps(
            {
                "api_rule_classes": ["vendor.SpecialApi"],
                "component_rule_classes": ["vendor.SpecialCall"],
                "attribute_aliases": {
                    "method": ["Verb"],
                    "path": ["Route"],
                    "output": ["Reply"],
                    "selector": ["TargetModule"],
                },
            }
        ),
        encoding="utf-8",
    )
    database = tmp_path / "contracts.db"

    assert main(
        [
            "build", str(corpus), "--integrator", str(integrator),
            "--db", str(database), "--rules-config", str(config), "--quiet",
        ]
    ) == 0
    store = ContractStore(database)
    try:
        operation = store.rows(
            "SELECT classification, action, api_path, output_group FROM odata_operations"
        )[0]
        assert tuple(operation) == ("CONFIRMED", "PATCH", "/custom", "Response.Custom")
        caller = store.rows(
            "SELECT classification, selector FROM caller_references"
        )[0]
        assert tuple(caller) == ("CONFIRMED", "DataIntegrator.ifp")
        link = store.rows(
            "SELECT resolution_status FROM caller_operation_links"
        )[0]
        assert link["resolution_status"] == "EXACT_PRODUCT"
    finally:
        store.close()


def test_unknown_reference_attribute_is_kept_as_exact_evidence(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text("<Project />", encoding="utf-8")
    (corpus / "caller.ifp").write_text(
        '<Project><Rule RuleClassName="vendor.FutureCall" '
        'CompletelyNewPointer="prefix/DataIntegrator.ifp" /></Project>',
        encoding="utf-8",
    )
    store = ContractStore(tmp_path / "contracts.db")
    try:
        build_contracts(corpus, integrator, "DataIntegrator.ifp", store)
        row = store.rows(
            "SELECT attributes_json FROM caller_references"
        )[0]
        attributes = json.loads(row["attributes_json"])
        assert "CompletelyNewPointer" in attributes
        diagnostic = store.rows(
            "SELECT evidence_json FROM diagnostics WHERE code = 'UNCLASSIFIED_REFERENCE_RULE'"
        )[0]
        evidence = json.loads(diagnostic["evidence_json"])
        assert "CompletelyNewPointer" in evidence["matching_attributes"]
    finally:
        store.close()


def test_failed_file_keeps_partial_and_is_retried_after_repair(tmp_path, capsys):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text("<Project />", encoding="utf-8")
    caller = corpus / "caller.ifp"
    caller.write_text(
        '<Project><Rule RuleClassName="CallComponentRule" '
        'SelectComponent="DataIntegrator.ifp" /><!-- never closed',
        encoding="utf-8",
    )
    database = tmp_path / "contracts.db"
    command = [
        "build", str(corpus), "--integrator", str(integrator),
        "--db", str(database), "--quiet",
    ]

    assert main(command) == 1
    assert not database.exists()
    partial = Path(str(database) + ".partial")
    assert partial.exists()
    assert "scan is incomplete" in capsys.readouterr().err
    store = ContractStore(partial)
    try:
        assert store.metadata_value("scan_status") == "partial_with_errors"
        assert store.rows("SELECT status FROM scan_files")[0]["status"] == "failed"
    finally:
        store.close()

    caller.write_text(
        '<Project><Rule RuleClassName="CallComponentRule" '
        'SelectComponent="DataIntegrator.ifp" /></Project>',
        encoding="utf-8",
    )
    assert main(command) == 0
    output = capsys.readouterr().out
    assert "scanned=1" in output
    assert database.exists()
    assert not partial.exists()


def test_schema_v1_database_is_migrated_without_data_loss(tmp_path):
    database = tmp_path / "old.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE odata_operations (
          id INTEGER PRIMARY KEY, integrator_file TEXT NOT NULL, tag_offset INTEGER NOT NULL,
          rule_eid TEXT, rule_name TEXT, rule_class TEXT NOT NULL, source_name TEXT,
          action TEXT, api_path TEXT, filter_expr TEXT, request_group TEXT,
          target_group TEXT, results_group TEXT, output_group TEXT,
          attributes_json TEXT NOT NULL, UNIQUE(integrator_file, tag_offset)
        );
        CREATE TABLE caller_references (
          id INTEGER PRIMARY KEY, caller_file TEXT NOT NULL, tag_offset INTEGER NOT NULL,
          rule_eid TEXT, rule_name TEXT, rule_class TEXT, selector TEXT, source_name TEXT,
          component_list TEXT, attributes_json TEXT NOT NULL,
          UNIQUE(caller_file, tag_offset)
        );
        CREATE TABLE caller_mappings (
          id INTEGER PRIMARY KEY, reference_id INTEGER NOT NULL,
          mapping_prefix TEXT NOT NULL, solution_data_item TEXT, property_key TEXT,
          direction TEXT NOT NULL, class_type TEXT, attributes_json TEXT NOT NULL,
          UNIQUE(reference_id, mapping_prefix)
        );
        INSERT INTO metadata VALUES ('legacy', 'preserved');
        """
    )
    connection.close()

    store = ContractStore(database)
    try:
        assert store.metadata_value("legacy") == "preserved"
        operation_columns = {
            row["name"] for row in store.rows("PRAGMA table_info(odata_operations)")
        }
        assert {"product_name", "classification", "disabled", "base_url"} <= operation_columns
        assert store.rows("PRAGMA user_version")[0][0] == 4
    finally:
        store.close()


def test_dynamic_references_are_opt_in_to_avoid_corpus_noise(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text("<Project />", encoding="utf-8")
    (corpus / "dynamic.ifp").write_text(
        '<Project><Rule RuleClassName="CallComponentRule" '
        'SelectComponent="$$RuntimeComponent$" /></Project>',
        encoding="utf-8",
    )
    store = ContractStore(tmp_path / "contracts.db")
    try:
        summary = build_contracts(corpus, integrator, "DataIntegrator.ifp", store)
        assert summary.caller_references == 0
        assert summary.diagnostics == 0
    finally:
        store.close()


def test_multiple_products_without_a_caller_target_are_reported_ambiguous(tmp_path):
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text(
        """<Project>
        <Product Name="A"><Rule RuleClassName="SwaggerIntegrationRule"
          HTTPMethod="GET" Path="/a" /></Product>
        <Product Name="B"><Rule RuleClassName="SwaggerIntegrationRule"
          HTTPMethod="GET" Path="/b" /></Product>
        </Project>""",
        encoding="utf-8",
    )
    (corpus / "caller.ifp").write_text(
        '<Project><Rule RuleClassName="CallComponentRule" '
        'SelectComponent="DataIntegrator.ifp" /></Project>',
        encoding="utf-8",
    )
    store = ContractStore(tmp_path / "contracts.db")
    try:
        summary = build_contracts(corpus, integrator, "DataIntegrator.ifp", store)
        assert summary.operation_links == 0
        row = store.rows(
            "SELECT evidence_json FROM diagnostics "
            "WHERE code = 'AMBIGUOUS_OPERATION_TARGET'"
        )[0]
        assert json.loads(row["evidence_json"])["available_products"] == ["a", "b"]
    finally:
        store.close()


def test_practical_example_keeps_the_complete_contract_chain(tmp_path):
    corpus = Path(__file__).parents[1] / "examples" / "practical-corpus"
    integrator = corpus / "BankingDataIntegrator.ifp"
    store = ContractStore(tmp_path / "contracts.db")
    try:
        summary = build_contracts(
            corpus, integrator, "BankingDataIntegrator.ifp", store
        )
        assert (
            summary.odata_operations,
            summary.data_sources,
            summary.caller_references,
            summary.caller_mappings,
            summary.operation_links,
            summary.diagnostics,
        ) == (2, 2, 3, 4, 3, 1)
        requests = store.rows(
            "SELECT action, base_url, api_path FROM odata_operations ORDER BY tag_offset"
        )
        assert [tuple(row) for row in requests] == [
            ("GET", "https://api.bank.example/odata/v2", "customers"),
            ("POST", "https://api.bank.example/v1", "/orders"),
        ]
        links = store.rows(
            "SELECT resolution_status FROM caller_operation_links"
        )
        assert {row["resolution_status"] for row in links} == {"EXACT_PRODUCT"}
    finally:
        store.close()
