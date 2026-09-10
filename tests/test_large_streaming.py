from __future__ import annotations

import os
import resource
import time

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

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    store = ContractStore(tmp_path / "contracts.db")
    try:
        summary = build_contracts(corpus, integrator, "DataIntegrator.ifp", store)
    finally:
        store.close()
    elapsed = time.perf_counter() - started
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert summary.caller_references == 1
    assert summary.odata_operations == 1
    # Linux reports KiB. The scanner uses one-megabyte chunks plus overlap.
    assert rss_after - rss_before < 64 * 1024
    print(f"searched {size_mb} MiB in {elapsed:.3f}s; RSS delta={(rss_after-rss_before)/1024:.1f} MiB")
