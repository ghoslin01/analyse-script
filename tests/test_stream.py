from __future__ import annotations

from ifp_contract.stream import (
    iter_occurrence_offsets,
    iter_start_tags,
    read_selected_attributes,
)


def test_scanner_handles_chunk_boundaries_and_gt_inside_quoted_values(tmp_path):
    path = tmp_path / "quoted.ifp"
    path.write_bytes(
        b'<Project><Rule Name="a > b" SelectComponent="DataIntegrator.ifp" '
        b'Keep="unneeded" /></Project>'
    )

    tags = list(iter_start_tags(path, needle=b"DataIntegrator.ifp", chunk_size=7))
    rule = next(tag for tag in tags if tag.name == "Rule")
    assert rule.contains_needle is True
    attributes = read_selected_attributes(
        path, rule.start, rule.end, lambda name: name in {"Name", "SelectComponent"}, chunk_size=5
    )
    assert attributes == {"Name": "a > b", "SelectComponent": "DataIntegrator.ifp"}


def test_fast_occurrence_search_handles_boundaries_and_overlaps(tmp_path):
    path = tmp_path / "bytes.ifp"
    path.write_bytes(b"xxABABAyy")
    assert list(iter_occurrence_offsets(path, b"ABA", chunk_size=4)) == [2, 4]
