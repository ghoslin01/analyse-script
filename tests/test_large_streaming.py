from __future__ import annotations

import os
import time
import tracemalloc

try:
    import resource
except ImportError:  # Windows
    resource = None

import pytest

from ifp_contract.extractor import build_contracts
from ifp_contract.store import ContractStore


@pytest.mark.skipif(
    "IFP_LARGE_TEST_MB" not in os.environ,
    reason="set IFP_LARGE_TEST_MB=256 (or larger) for the large-file verification",
)
def test_hundreds_of_megabytes_are_searched_with_bounded_memory(tmp_path):
    size_mb = int(os.environ["IFP_LARGE_TEST_MB"])
    corpus = tmp_path / "ifp"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    integrator.write_text(
        '<Project><Rule RuleClassName="InvokeIRISRule" IRISAction="LIST" '
        'ResourcePath="Customers" /></Project>',
        encoding="utf-8",
    )
    path = corpus / "large.ifp"
    block = b"x" * (1024 * 1024)
    with path.open("wb") as handle:
        handle.write(b"<Project><Blob>")
        for _ in range(size_mb):
            handle.write(block)
        handle.write(
            b'</Blob><Rule RuleClassName="CallComponentRule" '
            b'SelectComponent="DataIntegrator.ifp" /></Project>'
        )

    if resource is None:
        tracemalloc.start()
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else 0
    started = time.perf_counter()
    store = ContractStore(tmp_path / "contracts.db")
    try:
        summary = build_contracts(corpus, integrator, "DataIntegrator.ifp", store)
    finally:
        store.close()
    elapsed = time.perf_counter() - started
    if resource:
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        memory_mb = (rss_after - rss_before) / (1024 * 1024 if os.uname().sysname == "Darwin" else 1024)
    else:
        memory_mb = tracemalloc.get_traced_memory()[1] / (1024 * 1024)
        tracemalloc.stop()

    assert summary.caller_references == 1
    assert summary.odata_operations == 1
    assert memory_mb < 64
    print(f"searched {size_mb} MiB in {elapsed:.3f}s; memory increase={memory_mb:.1f} MiB")


@pytest.mark.skipif("IFP_LARGE_TEST_MB" not in os.environ, reason="opt-in dense contract verification")
def test_dense_products_and_repeated_references(tmp_path):
    count = 10_000
    corpus = tmp_path / "dense"
    corpus.mkdir()
    integrator = corpus / "DataIntegrator.ifp"
    caller = corpus / "caller.ifp"
    with integrator.open("w", encoding="utf-8") as out:
        out.write("<Project>")
        for index in range(count):
            out.write(f'<Product Name="P{index}"><Rule RuleClassName="InvokeIRISRule" ResourcePath="/{index}" /></Product>')
        out.write("</Project>")
    with caller.open("w", encoding="utf-8") as out:
        out.write("<Project>")
        for index in range(count):
            out.write(f'<Rule RuleClassName="CallComponentRule" SelectComponent="DataIntegrator.ifp" VendorPointer="DataIntegrator.ifp" ComponentList="P{index}" />')
        out.write("</Project>")

    started = time.perf_counter()
    store = ContractStore(tmp_path / "contracts.db")
    try:
        summary = build_contracts(corpus, integrator, integrator.name, store)
        assert summary.odata_operations == count
        assert summary.caller_references == count
        assert summary.operation_links == count
        assert summary.diagnostics == 0
        assert store.rows("SELECT COUNT(*) FROM scan_files")[0][0] == 1
        assert store.rows(
            "SELECT COUNT(*) FROM caller_operation_links l JOIN odata_operations o ON o.id = l.operation_id "
            "JOIN caller_references c ON c.id = l.reference_id WHERE o.product_name != c.component_list"
        )[0][0] == 0
        store.validate()
    finally:
        store.close()
    print(f"extracted {count} Products and callers in {time.perf_counter() - started:.3f}s")
