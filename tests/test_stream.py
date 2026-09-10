from __future__ import annotations

from ifp_contract.stream import iter_start_tags, read_selected_attributes


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
