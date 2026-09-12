from pathlib import Path
import hashlib
import json
import sqlite3

import pytest

from ifp_contract.cli import main
from ifp_contract.extractor import build_contracts, iter_caller_references
from ifp_contract.store import ContractStore


def corpus_with_product(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text(
        '<Project><Product Name="A"><Rule RuleClassName="InvokeIRISRule" '
        'IRISAction="GET" ResourcePath="/a" /></Product></Project>', encoding="utf-8",
    )
    return corpus, integrator


@pytest.mark.parametrize("selector", ["SelectComponent", "VendorPointer"])
@pytest.mark.parametrize("dynamic_first", [False, True])
def test_dynamic_census_preserves_explicit_reference(tmp_path, selector, dynamic_first):
    caller = tmp_path / "caller.ifp"
    attrs = ['Source="$$Runtime$"', f'{selector}="DataIntegrator.ifp"']
    if not dynamic_first:
        attrs.reverse()
    caller.write_text('<Rule RuleClassName="CallComponentRule" ' + " ".join(attrs) + ' />')
    ordinary = list(iter_caller_references(caller, ("DataIntegrator.ifp",)))
    diagnostic = list(iter_caller_references(caller, ("DataIntegrator.ifp",), include_dynamic=True))
    assert len(ordinary) == len(diagnostic) == 1
    assert ordinary[0].caller == diagnostic[0].caller
    assert diagnostic[0].direct_reference


def test_dynamic_whitespace_is_supported_and_non_selectors_are_ignored(tmp_path):
    caller = tmp_path / "caller.ifp"
    caller.write_text(
        '<Project Name="$$Ignore$"><Rule Name="$$AlsoIgnore$" />'
        '<Rule SelectComponent \n = "$$Runtime$" /></Project>'
    )
    evidence = list(iter_caller_references(caller, ("DataIntegrator.ifp",), include_dynamic=True))
    assert len(evidence) == 1
    assert evidence[0].caller is None
    assert evidence[0].diagnostics[0]["code"] == "DYNAMIC_REFERENCE_UNRESOLVED"


def test_resume_removes_deleted_diagnostic_only_file(tmp_path):
    corpus, integrator = corpus_with_product(tmp_path)
    caller = corpus / "dynamic.ifp"
    caller.write_text('<Rule SelectComponent="$$Runtime$" />')
    store = ContractStore(tmp_path / "contracts.db")
    try:
        first = build_contracts(corpus, integrator, integrator.name, store, include_dynamic=True)
        assert first.diagnostics == 1
        assert store.rows("SELECT * FROM scan_files") == []
        caller.unlink()
        second = build_contracts(corpus, integrator, integrator.name, store, include_dynamic=True, resume=True)
        assert second.diagnostics == 0
        assert store.metadata_value("scan_status") == "complete"
    finally:
        store.close()


@pytest.mark.parametrize("target,expected_links", [("B", 0), ("", 1), ("A", 1)])
def test_fallback_requires_absent_target(tmp_path, target, expected_links):
    corpus, integrator = corpus_with_product(tmp_path)
    (corpus / "caller.ifp").write_text(
        f'<Rule RuleClassName="CallComponentRule" SelectComponent="DataIntegrator.ifp" ComponentList="{target}" />'
    )
    store = ContractStore(tmp_path / "contracts.db")
    try:
        summary = build_contracts(corpus, integrator, integrator.name, store)
        assert summary.operation_links == expected_links
        assert summary.diagnostics == (1 if target == "B" else 0)
    finally:
        store.close()


@pytest.mark.parametrize("bad_attribute", ['Bad=unquoted', 'Bad "value"', 'Bad="unclosed'])
def test_incompatible_rules_do_not_stop_integrator_or_caller(tmp_path, bad_attribute):
    corpus, integrator = corpus_with_product(tmp_path)
    integrator.write_text(
        f'<Project><Rule RuleClassName="vendor.Incompatible" {bad_attribute} />'
        '<Product Name="A"><Rule RuleClassName="vendor.FutureApi" HTTPMethod="GET" Path="/custom" />'
        '<Rule Name="good" RuleClassName="InvokeIRISRule" ResourcePath="/good" /></Product></Project>'
    )
    caller = corpus / "caller.ifp"
    caller.write_text(
        f'<Project><Rule SelectComponent="DataIntegrator.ifp" {bad_attribute} />'
        '<Rule RuleClassName="vendor.FutureCall" SelectComponent="DataIntegrator.ifp" ComponentList="A" />'
        '<Rule Name="good" RuleClassName="CallComponentRule" SelectComponent="DataIntegrator.ifp" ComponentList="A" /></Project>'
    )
    database = tmp_path / "contracts.db"
    command = ["build", str(corpus), "--integrator", str(integrator), "--db", str(database), "--quiet"]
    assert main(command) == 0
    store = ContractStore(database, readonly=True)
    try:
        assert store.counts()["odata_operations"] == 2
        assert store.counts()["caller_references"] == 2
        bad = store.rows("SELECT file_path, tag_offset, evidence_json FROM diagnostics WHERE code = 'INCOMPATIBLE_RULE'")
        assert {row["file_path"] for row in bad} == {str(caller), str(integrator)}
        assert all(row["tag_offset"] >= 0 and json.loads(row["evidence_json"])["reason"] for row in bad)
        assert store.metadata_value("files_failed") == "0"
    finally:
        store.close()
    assert main(command + ["--strict"]) == 2


def test_failed_integrator_still_scans_callers_and_preserves_published_db(tmp_path):
    corpus, integrator = corpus_with_product(tmp_path)
    database = tmp_path / "contracts.db"
    command = ["build", str(corpus), "--integrator", str(integrator), "--db", str(database), "--quiet"]
    assert main(command) == 0
    original = hashlib.sha256(database.read_bytes()).digest()
    integrator.write_text('<Project><!-- unclosed')
    (corpus / "caller.ifp").write_text('<Rule SelectComponent="DataIntegrator.ifp" />')
    assert main(command) == 1
    assert hashlib.sha256(database.read_bytes()).digest() == original
    store = ContractStore(Path(str(database) + ".partial"), readonly=True)
    try:
        assert store.counts()["caller_references"] == 1
        assert store.metadata_value("scan_status") == "partial_with_errors"
        assert store.metadata_value("files_failed") == "1"
        assert store.rows("SELECT status FROM scan_files WHERE file_path = ?", (str(integrator),))[0][0] == "failed"
    finally:
        store.close()


@pytest.mark.parametrize("format", ["markdown", "json"])
def test_report_does_not_write_or_migrate_source(tmp_path, format):
    corpus, integrator = corpus_with_product(tmp_path)
    database = tmp_path / "contracts.db"
    store = ContractStore(database)
    build_contracts(corpus, integrator, integrator.name, store)
    store.connection.execute("PRAGMA user_version = 3")
    store.commit()
    store.close()
    before = database.read_bytes()
    before_mtime = database.stat().st_mtime_ns
    assert main(["report", "--db", str(database), "--format", format, "--output", str(tmp_path / 'report.txt')]) == 0
    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == before_mtime
    readonly = ContractStore(database, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            readonly.connection.execute("DELETE FROM metadata")
    finally:
        readonly.close()


def test_old_or_invalid_report_returns_error_without_altering_db(tmp_path, capsys):
    database = tmp_path / "old.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE legacy(value TEXT)")
    connection.close()
    before = database.read_bytes()
    assert main(["report", "--db", str(database)]) == 1
    assert "ERROR:" in capsys.readouterr().err
    assert database.read_bytes() == before


def test_late_data_sources_resolve_without_assigning_unnamed_source(tmp_path):
    corpus, integrator = corpus_with_product(tmp_path)
    integrator.write_text(
        '<Project><Rule Name="named" RuleClassName="InvokeIRISRule" IRISSource="ÄPI" ResourcePath="/a" />'
        '<Rule Name="unnamed" RuleClassName="InvokeIRISRule" ResourcePath="/b" />'
        '<DataSource Name="äpi" BaseURL="https://api.example" />'
        '<DataSource BaseURL="https://wrong.example" /></Project>'
    )
    store = ContractStore(tmp_path / "contracts.db")
    try:
        build_contracts(corpus, integrator, integrator.name, store)
        assert [tuple(row) for row in store.rows("SELECT rule_name, base_url FROM odata_operations ORDER BY tag_offset")] == [
            ("named", "https://api.example"), ("unnamed", None),
        ]
    finally:
        store.close()
