from __future__ import annotations

import pytest

from ifp_contract.xmlbytes import (
    MalformedXMLStructure,
    TagScanLimitExceeded,
    UnsupportedIFPEncoding,
    detect_xml_encoding,
    find_tag_span,
    iter_xml_visible_matches,
    read_tag_attributes,
)


def test_visible_matches_skip_chunk_split_comments_cdata_and_doctype(tmp_path):
    path = tmp_path / "sections.ifp"
    path.write_bytes(
        b'<!DOCTYPE Project [<!ENTITY fake "<Rule Hidden=\'doctype\'>">]>'
        b"<Project><!-- <Rule Hidden='comment'> -->"
        b"<![CDATA[<Rule Hidden='cdata'>]]>"
        b"<Rule Name='real'/></Project>"
    )

    matches = list(iter_xml_visible_matches(path, ("<Rule",), chunk_size=5))
    assert len(matches) == 1
    assert path.read_bytes()[matches[0].offset :].startswith(b"<Rule Name='real'")


def test_utf16_big_endian_offsets_and_attributes_are_preserved(tmp_path):
    path = tmp_path / "utf16be.ifp"
    text = '<Project><Rule Name="查询" SelectComponent="DataIntegrator.ifp" /></Project>'
    path.write_bytes(b"\xfe\xff" + text.encode("utf-16-be"))

    encoding = detect_xml_encoding(path)
    match = next(iter_xml_visible_matches(path, ("DataIntegrator.ifp",), chunk_size=14))
    span = find_tag_span(path, match.offset, encoding=encoding)
    assert span is not None
    attributes = read_tag_attributes(path, span, lambda _name: True, encoding=encoding)
    assert attributes.values["Name"] == "查询"
    assert attributes.values["SelectComponent"] == "DataIntegrator.ifp"


def test_selected_attribute_is_bounded_and_reports_original_size(tmp_path):
    path = tmp_path / "large-attribute.ifp"
    path.write_text('<Rule Payload="' + "x" * 100 + '" Name="ok" />', encoding="utf-8")
    span = find_tag_span(path, 0, containing=False)
    assert span is not None

    result = read_tag_attributes(
        path, span, lambda _name: True, max_value_bytes=16, chunk_size=7
    )
    assert result.values["Name"] == "ok"
    assert result.values["Payload"].startswith("x" * 16)
    assert "TRUNCATED observed_bytes=100" in result.values["Payload"]
    assert result.truncated[0].observed_bytes == 100


def test_malformed_sections_and_tags_fail_boundedly(tmp_path):
    section = tmp_path / "unclosed-comment.ifp"
    section.write_text("<Project><!-- <Rule", encoding="utf-8")
    with pytest.raises(MalformedXMLStructure):
        list(iter_xml_visible_matches(section, ("<Rule",), chunk_size=4))

    tag = tmp_path / "unclosed-tag.ifp"
    tag.write_text('<Rule Name="never closes"', encoding="utf-8")
    with pytest.raises(TagScanLimitExceeded):
        find_tag_span(tag, 0, containing=False, max_tag_bytes=12)


def test_utf32_is_rejected_explicitly_instead_of_misread_as_utf16(tmp_path):
    path = tmp_path / "utf32.ifp"
    path.write_text("<Project />", encoding="utf-32")
    with pytest.raises(UnsupportedIFPEncoding, match="UTF-32"):
        detect_xml_encoding(path)


@pytest.mark.parametrize("codec,bom", [("utf-8", b""), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")])
def test_unknown_reference_crossing_chunks_is_captured(codec, bom, tmp_path):
    path = tmp_path / "reference.ifp"
    content = '<Rule Ignored="' + 'x' * 101 + 'DataIntegrator.ifp/尾部😀" Name="查询😀" />'
    path.write_bytes(bom + content.encode(codec))
    span = find_tag_span(path, len(bom), containing=False)
    result = read_tag_attributes(
        path, span, lambda name: name == "Name", reference_patterns=("DataIntegrator.ifp",), chunk_size=10,
    )
    assert result.values["Name"] == "查询😀"
    assert result.values["Ignored"] == "…DataIntegrator.ifp/尾部😀"
    assert result.matched_references == {"DataIntegrator.ifp"}


def test_large_ignored_attribute_is_skipped_without_per_byte_decoding(tmp_path, monkeypatch):
    from ifp_contract.xmlbytes import XMLByteEncoding

    path = tmp_path / "payload.ifp"
    path.write_bytes(b'<Rule Payload="' + b'x' * (4 * 1024 * 1024) + b'" Name="ok" />')
    span = find_tag_span(path, 0, containing=False)
    decode = XMLByteEncoding.decode
    calls = 0

    def counted(self, raw):
        nonlocal calls
        calls += 1
        return decode(self, raw)

    monkeypatch.setattr(XMLByteEncoding, "decode", counted)
    result = read_tag_attributes(path, span, lambda name: name == "Name")
    assert result.values == {"Name": "ok"}
    assert calls < 20


def test_visible_dense_matches_match_expected_offsets_across_sections(tmp_path):
    path = tmp_path / "dense.ifp"
    content = (b'<Rule Name="ok"/><!-- <Rule hidden="yes"/> -->' * 300)
    path.write_bytes(content)
    expected = [index for index in range(len(content)) if content.startswith(b'<Rule Name=', index)]
    matches = list(iter_xml_visible_matches(path, ("<Rule", "<Product", "<Phase"), chunk_size=73))
    assert [match.offset for match in matches] == expected


@pytest.mark.parametrize('codec,bom', [('utf-8', b''), ('utf-16-le', b'\xff\xfe'), ('utf-16-be', b'\xfe\xff')])
@pytest.mark.parametrize('chunk_size', [2, 6, 14, 65536])
def test_attribute_names_cross_chunks_and_preserve_unicode_and_duplicates(tmp_path, codec, bom, chunk_size):
    # These UTF-16 characters can form delimiter bytes at an unaligned offset.
    key = 'Mapping_\u3d00\u003d'.replace('=', '') + '字段😀_' + 'x' * 300
    path = tmp_path / 'names.ifp'
    path.write_bytes(bom + f'<Rule {key} = "first" Ignored="skip" {key}="second&amp;"/>'.encode(codec))
    span = find_tag_span(path, len(bom), containing=False)
    result = read_tag_attributes(path, span, lambda name: name == key, chunk_size=chunk_size)
    assert result.values == {key: 'second&'}
    assert result.duplicates == [key]


@pytest.mark.parametrize('suffix', ['/>', ' other="value"/>'])
def test_long_attribute_without_value_still_fails(tmp_path, suffix):
    path = tmp_path / 'broken-name.ifp'
    path.write_text('<Rule ' + 'x' * 300 + suffix, encoding='utf-8')
    span = find_tag_span(path, 0, containing=False)
    with pytest.raises(MalformedXMLStructure):
        read_tag_attributes(path, span, lambda _: True, chunk_size=14)
