import os
import json

from ifp_contract.trace import Trace
from ifp_contract.trace_source import TraceSource


XML = '''<?xml version="1.0"?><Project><Product Name="P"><Phase Name="I">
<Rule Name="set" eid="set" Long="value"><Question Name="q" Broken="&amp;"/></Rule>
</Phase></Product></Project>'''


def write(path, content=XML, encoding="utf-8"):
    path.write_text(content, encoding=encoding)


def shape(source):
    return [(n.key, n.tag, n.offset, n.line, n.meta, n.parent.key if n.parent else None,
             [c.key for c in n.children], n.attrs) for n in source.nodes]


def test_cold_warm_restore_preserves_structure_and_lazy_attributes(tmp_path):
    source_path = tmp_path / "a.ifp"
    write(source_path)
    cache = tmp_path / "cache"
    cold = TraceSource(tmp_path, source_path, cache_dir=cache, extra_metadata=("Long",))
    # Force a lazy attribute and an issue into the persisted payload.
    question = next(n for n in cold.nodes if n.tag == "Question")
    cold.attributes(question)
    assert cold.save_cache()

    warm = TraceSource(tmp_path, source_path, cache_dir=cache, extra_metadata=("Long",))
    assert warm.cache_hit
    assert shape(warm) == shape(cold)
    assert warm.eids["set"][0].key == cold.eids["set"][0].key
    assert warm.nodes[0].children[0].parent is warm.nodes[0]
    assert warm.issues == []


def test_same_size_and_restored_mtime_with_changed_content_is_a_miss(tmp_path):
    source_path = tmp_path / "a.ifp"
    write(source_path)
    cache = tmp_path / "cache"
    cold = TraceSource(tmp_path, source_path, cache_dir=cache)
    assert cold.save_cache()
    original_stat = source_path.stat()
    changed = XML.replace('Name="set"', 'Name="get"')
    assert len(changed.encode()) == len(XML.encode())
    source_path.write_text(changed, encoding="utf-8")
    os.utime(source_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    warm = TraceSource(tmp_path, source_path, cache_dir=cache)
    assert not warm.cache_hit
    assert next(n for n in warm.nodes if n.tag == "Rule").meta["Name"] == "get"


def test_metadata_and_node_limit_are_part_of_cache_key(tmp_path):
    source_path = tmp_path / "a.ifp"
    write(source_path)
    cache = tmp_path / "cache"
    first = TraceSource(tmp_path, source_path, cache_dir=cache, extra_metadata=("Long",))
    assert first.save_cache()
    assert not TraceSource(tmp_path, source_path, cache_dir=cache, extra_metadata=("Other",)).cache_hit
    assert not TraceSource(tmp_path, source_path, cache_dir=cache, max_nodes=100001, extra_metadata=("Long",)).cache_hit


def test_corrupt_cache_and_unwritable_cache_fall_back(tmp_path):
    source_path = tmp_path / "a.ifp"
    write(source_path)
    cache = tmp_path / "cache"
    cold = TraceSource(tmp_path, source_path, cache_dir=cache)
    assert cold.save_cache()
    cache_file = next(cache.glob("*.json"))
    cache_file.write_text("{broken", encoding="utf-8")
    assert not TraceSource(tmp_path, source_path, cache_dir=cache).cache_hit


def test_utf16_source_can_be_cached(tmp_path):
    source_path = tmp_path / "utf16.ifp"
    write(source_path, XML, "utf-16")
    cache = tmp_path / "cache"
    cold = TraceSource(tmp_path, source_path, cache_dir=cache)
    assert cold.save_cache()
    warm = TraceSource(tmp_path, source_path, cache_dir=cache)
    assert warm.cache_hit
    assert shape(warm) == shape(cold)


def test_node_diagnostics_are_replayed_only_when_node_is_read(tmp_path):
    source_path = tmp_path / "a.ifp"
    source_path.write_text(
        '<Project><Product Name="P"><Phase Name="A"><Rule Name="bad" Foo="1" Foo="2"/></Phase>'
        '<Phase Name="B"><Rule Name="other" Foo="3"/></Phase></Product></Project>', encoding="utf-8")
    cache = tmp_path / "cache"
    cold = TraceSource(tmp_path, source_path, cache_dir=cache)
    bad = next(n for n in cold.nodes if n.meta.get("Name") == "bad")
    cold.attributes(bad)
    assert [issue["code"] for issue in cold.issues] == ["DUPLICATE_ATTRIBUTE"]
    assert cold.save_cache()

    warm = TraceSource(tmp_path, source_path, cache_dir=cache)
    assert warm.issues == []
    other = next(n for n in warm.nodes if n.meta.get("Name") == "other")
    warm.attributes(other)
    assert warm.issues == []
    warm.attributes(next(n for n in warm.nodes if n.meta.get("Name") == "bad"))
    assert [issue["code"] for issue in warm.issues] == ["DUPLICATE_ATTRIBUTE"]


def test_trace_cold_warm_third_evidence_is_identical(tmp_path):
    source_path = tmp_path / "a.ifp"
    source_path.write_text(
        '<Project><Product Name="P"><Phase Name="A"><Rule Name="set" RuleClassName="SetValueRule" PropertyName="X" Foo="1" Foo="2"/></Phase>'
        '<Phase Name="B"><Rule Name="set" RuleClassName="SetValueRule" PropertyName="Y"/></Phase></Product></Project>', encoding="utf-8")
    cache = tmp_path / "cache"
    cold_a = Trace(tmp_path, cache_dir=cache).collect("a.ifp", "X", None, "P.A")
    warm_b = Trace(tmp_path, cache_dir=cache).collect("a.ifp", "Y", None, "P.B")
    warm_a = Trace(tmp_path, cache_dir=cache).collect("a.ifp", "X", None, "P.A")
    cold_b = Trace(tmp_path, cache_dir=None).collect("a.ifp", "Y", None, "P.B")
    cold_a_again = Trace(tmp_path, cache_dir=None).collect("a.ifp", "X", None, "P.A")
    assert warm_b == cold_b
    assert warm_a == cold_a_again
    assert any(issue["code"] == "DUPLICATE_ATTRIBUTE" for issue in cold_a["issues"])
    assert not any(issue["code"] == "DUPLICATE_ATTRIBUTE" for issue in warm_b["issues"])


def test_diagnostics_survive_phase_switch_and_return_to_original(tmp_path):
    source_path = tmp_path / "a.ifp"
    source_path.write_text(
        '<Project><Product Name="P"><Phase Name="A"><Rule Name="bad" Foo="1" Foo="2"/></Phase>'
        '<Phase Name="B"><Rule Name="ok" Foo="3"/></Phase></Product></Project>', encoding="utf-8")
    cache = tmp_path / "cache"
    first = TraceSource(tmp_path, source_path, cache_dir=cache)
    bad = next(n for n in first.nodes if n.meta.get("Name") == "bad")
    first.attributes(bad)
    assert first.save_cache()
    second = TraceSource(tmp_path, source_path, cache_dir=cache)
    second.attributes(next(n for n in second.nodes if n.meta.get("Name") == "ok"))
    assert second.issues == []
    second.save_cache()
    third = TraceSource(tmp_path, source_path, cache_dir=cache)
    third.attributes(next(n for n in third.nodes if n.meta.get("Name") == "bad"))
    assert [issue["code"] for issue in third.issues] == ["DUPLICATE_ATTRIBUTE"]


def test_truncated_attribute_diagnostic_is_cached_per_node(tmp_path):
    source_path = tmp_path / "a.ifp"
    huge = "x" * (1024 * 1024 + 64)
    source_path.write_text(
        f'<Project><Product Name="P"><Phase Name="I"><Rule Name="large" Data="{huge}"/></Phase>'
        '</Product></Project>', encoding="utf-8")
    cache = tmp_path / "cache"
    cold = TraceSource(tmp_path, source_path, cache_dir=cache)
    node = next(n for n in cold.nodes if n.meta.get("Name") == "large")
    cold.attributes(node)
    assert [issue["code"] for issue in cold.issues] == ["TRUNCATED_ATTRIBUTE"]
    assert cold.save_cache()
    warm = TraceSource(tmp_path, source_path, cache_dir=cache)
    assert warm.issues == []
    warm.attributes(next(n for n in warm.nodes if n.meta.get("Name") == "large"))
    assert [issue["code"] for issue in warm.issues] == ["TRUNCATED_ATTRIBUTE"]


def test_warm_new_attributes_are_persisted(tmp_path, monkeypatch):
    source_path = tmp_path / "a.ifp"
    write(source_path)
    cache = tmp_path / "cache"
    cold = TraceSource(tmp_path, source_path, cache_dir=cache)
    assert cold.save_cache()
    warm = TraceSource(tmp_path, source_path, cache_dir=cache)
    question = next(n for n in warm.nodes if n.tag == "Question")
    warm.attributes(question)
    assert warm.save_cache()

    def parser_must_not_run(*args, **kwargs):
        raise AssertionError("cached attributes were reparsed")

    monkeypatch.setattr("ifp_contract.trace_source.read_tag_attributes", parser_must_not_run)
    restored = TraceSource(tmp_path, source_path, cache_dir=cache)
    restored.attributes(next(n for n in restored.nodes if n.tag == "Question"))
    assert restored.cache_hit


def test_file_cache_path_is_safe_and_trace_still_succeeds(tmp_path):
    source_path = tmp_path / "a.ifp"
    write(source_path)
    cache_file = tmp_path / "cache-file"
    cache_file.write_text("cache is a file", encoding="utf-8")
    data = Trace(tmp_path, cache_dir=cache_file).collect("a.ifp", "q", None, "P.I")
    assert data["coverage"]["files_read"] == 1


def test_nested_corrupt_cache_falls_back(tmp_path):
    source_path = tmp_path / "a.ifp"
    write(source_path)
    cache = tmp_path / "cache"
    source = TraceSource(tmp_path, source_path, cache_dir=cache)
    assert source.save_cache()
    path = next(cache.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["nodes"][0]["children"] = ["missing@999"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    restored = TraceSource(tmp_path, source_path, cache_dir=cache)
    assert not restored.cache_hit
    assert restored.nodes


def test_metadata_set_order_has_same_cache_key(tmp_path):
    source_path = tmp_path / "a.ifp"
    write(source_path)
    cache = tmp_path / "cache"
    first = TraceSource(tmp_path, source_path, cache_dir=cache, extra_metadata={"Long", "Other"})
    assert first.save_cache()
    second = TraceSource(tmp_path, source_path, cache_dir=cache, extra_metadata={"Other", "Long"})
    assert second.cache_hit


def test_shared_rule_diagnostics_match_uncached_across_activations(tmp_path):
    (tmp_path / 'a.ifp').write_text(
        '<Project><Rule eid="shared" RuleClassName="SetValueRule" PropertyName="X" Foo="1" Foo="2"/>'
        '<Product Name="P"><Phase Name="I"><Rule LinkReference="shared"/>'
        '<Rule LinkReference="shared"/></Phase></Product></Project>', encoding='utf-8')
    expected = Trace(tmp_path).collect('a.ifp', 'X', None, 'P.I')
    cache = tmp_path / 'cache'
    for _ in range(3):
        trace = Trace(tmp_path, cache_dir=cache)
        assert trace.collect('a.ifp', 'X', None, 'P.I') == expected
