from pathlib import Path

import pytest

from ifp_contract import trace_locator
from ifp_contract.trace_locator import LookupIncompleteError, candidate_files


@pytest.mark.parametrize('chunk_size', range(1, 24))
def test_eid_attribute_survives_every_small_chunk_boundary(tmp_path, monkeypatch, chunk_size):
    monkeypatch.setattr(trace_locator, "_CHUNK_SIZE", chunk_size)
    path = tmp_path / "screen.ifp"
    path.write_text('<Question eid \n=\t "chunked-value-123"/>', encoding="utf-8")
    assert candidate_files(tmp_path, "chunked-value-123") == [path.resolve()]


def test_utf16_and_xml_entity_are_decoded(tmp_path):
    path = tmp_path / "screen.IFP"
    path.write_text('<Question eid="encoded&#45;value"/>', encoding="utf-16-le")
    raw = path.read_bytes()
    path.write_bytes(b"\xff\xfe" + raw)
    assert candidate_files(tmp_path, "encoded-value") == [path.resolve()]


def test_cache_is_incremental_and_root_specific(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    one = first / "a.ifp"
    two = second / "b.ifp"
    one.write_text('<Question eid="same"/>', encoding="utf-8")
    two.write_text('<Question eid="same"/>', encoding="utf-8")
    assert candidate_files(first, "same", cache) == [one.resolve()]
    assert candidate_files(second, "same", cache) == [two.resolve()]
    before = trace_locator._eids_in_file
    monkeypatch.setattr(trace_locator, "_eids_in_file", lambda path: pytest.fail("rescanned"))
    assert candidate_files(first, "same", cache) == [one.resolve()]
    monkeypatch.setattr(trace_locator, "_eids_in_file", before)
    one.write_text('<Question eid="changed"/>', encoding="utf-8")
    assert candidate_files(first, "same", cache) == []
    assert candidate_files(first, "changed", cache) == [one.resolve()]
    added = first / 'added.ifp'
    added.write_text('<Question eid="changed"/>', encoding='utf-8')
    assert candidate_files(first, 'changed', cache) == [one.resolve(), added.resolve()]
    one.unlink()
    assert candidate_files(first, 'changed', cache) == [added.resolve()]


def test_in_root_symlink_is_supported_and_duplicate_is_removed(tmp_path):
    real = tmp_path / "real.ifp"
    link = tmp_path / "link.ifp"
    real.write_text('<Question eid="linked"/>', encoding="utf-8")
    link.symlink_to(real)
    assert candidate_files(tmp_path, "linked") == [real.resolve()]


def test_unreadable_or_changing_file_does_not_become_empty_cache(tmp_path, monkeypatch):
    path = tmp_path / "screen.ifp"
    path.write_text('<Question eid="present"/>', encoding="utf-8")
    original = trace_locator._eids_in_file
    monkeypatch.setattr(trace_locator, "_eids_in_file", lambda _: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(LookupIncompleteError, match="cannot read"):
        candidate_files(tmp_path, "present", tmp_path / "cache")
    monkeypatch.setattr(trace_locator, "_eids_in_file", original)
    assert candidate_files(tmp_path, 'present', tmp_path / 'cache') == [path.resolve()]


def test_changed_during_scan_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / 'screen.ifp'
    path.write_text('<Question eid="old"/>')
    original = trace_locator._eids_in_file

    def mutate(source):
        values = original(source)
        source.write_text('<Question eid="replacement-longer"/>')
        return values

    monkeypatch.setattr(trace_locator, '_eids_in_file', mutate)
    with pytest.raises(LookupIncompleteError, match='changed while scanning'):
        candidate_files(tmp_path, 'old', tmp_path / 'cache')


def test_corrupt_index_falls_back_to_scan(tmp_path):
    path = tmp_path / 'screen.ifp'
    path.write_text('<Question eid="present"/>')
    cache = tmp_path / 'cache'
    assert candidate_files(tmp_path, 'present', cache) == [path.resolve()]
    next(cache.glob('*.sqlite3')).write_bytes(b'not a sqlite database')
    assert candidate_files(tmp_path, 'present', cache) == [path.resolve()]


def test_index_read_failure_is_reported_by_cli(tmp_path, monkeypatch, capsys):
    from ifp_contract.cli import main

    path = tmp_path / 'screen.ifp'
    path.write_text('<Question eid="present"/>')

    def unreadable(source):
        raise OSError('denied')

    monkeypatch.setattr(trace_locator, '_eids_in_file', unreadable)
    assert main(['trace', str(tmp_path), '--eid', 'present', '--no-cache']) == 1
    assert 'cannot read' in capsys.readouterr().err
