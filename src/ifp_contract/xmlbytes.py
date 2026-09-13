"""Encoding-aware, bounded-memory XML byte slicing.

The functions here recognize only enough XML structure to locate relevant
start tags and selected attributes. They never construct an element tree and
keep offsets in the original IFP byte stream.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import BinaryIO


class UnsupportedIFPEncoding(ValueError):
    """The XML encoding cannot be sliced without changing byte offsets."""


class TagScanLimitExceeded(ValueError):
    """A malformed or pathological XML start tag exceeded its safety window."""


class MalformedXMLStructure(ValueError):
    """A comment, CDATA block, declaration, or processing instruction is unclosed."""


@dataclass(frozen=True)
class XMLByteEncoding:
    codec: str
    unit: int = 1

    def encode(self, value: str) -> bytes:
        return value.encode(self.codec)

    def decode(self, value: bytes) -> str:
        return value.decode(self.codec, "replace")


UTF8 = XMLByteEncoding("utf-8")
UTF16_LE = XMLByteEncoding("utf-16-le", 2)
UTF16_BE = XMLByteEncoding("utf-16-be", 2)


def detect_xml_encoding(path: str | Path) -> XMLByteEncoding:
    """Detect UTF-8, UTF-16, or an ASCII-compatible declared XML encoding."""

    with Path(path).open("rb") as handle:
        prefix = handle.read(512)
    if prefix.startswith((b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")):
        raise UnsupportedIFPEncoding(f"UTF-32 XML is not supported: {path}")
    if len(prefix) >= 4 and (
        prefix[:4] == b"<\x00\x00\x00" or prefix[:4] == b"\x00\x00\x00<"
    ):
        raise UnsupportedIFPEncoding(f"UTF-32 XML is not supported: {path}")
    if prefix.startswith(b"\xff\xfe"):
        return UTF16_LE
    if prefix.startswith(b"\xfe\xff"):
        return UTF16_BE
    if len(prefix) >= 4:
        if prefix[0] == ord("<") and prefix[1] == 0:
            return UTF16_LE
        if prefix[0] == 0 and prefix[1] == ord("<"):
            return UTF16_BE
    declaration = re.search(br"encoding\s*=\s*['\"]([^'\"]+)", prefix[:256], re.I)
    if not declaration:
        return UTF8
    codec = declaration.group(1).decode("ascii", "replace")
    try:
        normalized = codecs.lookup(codec).name
    except LookupError as error:
        raise UnsupportedIFPEncoding(f"Unsupported XML encoding {codec!r}: {path}") from error
    if normalized in {"utf-8", "utf-8-sig", "ascii"}:
        return UTF8
    if normalized in {"utf-16", "utf-16-le"}:
        return UTF16_LE
    if normalized == "utf-16-be":
        return UTF16_BE
    probe = "<Rule>".encode(normalized)
    if probe == b"<Rule>":
        return XMLByteEncoding(normalized)
    raise UnsupportedIFPEncoding(
        f"Encoding {codec!r} uses variable-width/non-ASCII XML delimiters and is unsupported: {path}"
    )


@dataclass(frozen=True)
class VisibleMatch:
    offset: int
    pattern: str


def _aligned_find(data: bytes, needle: bytes, start: int, unit: int, base: int) -> int:
    found = data.find(needle, start)
    while found >= 0 and (base + found) % unit:
        found = data.find(needle, found + 1)
    return found


def iter_xml_visible_matches(
    path: str | Path,
    patterns: tuple[str, ...],
    *,
    encoding: XMLByteEncoding | None = None,
    chunk_size: int = 1024 * 1024,
    ignore_case: bool = False,
    on_bytes: Callable[[int], None] | None = None,
) -> Iterator[VisibleMatch]:
    """Find patterns outside comments, CDATA, declarations and XML PIs.

    Search work is performed by ``bytes.find``. A short unprocessed suffix is
    carried between chunks so both patterns and markup delimiters may cross a
    chunk boundary without loading the file.
    """

    file_path = Path(path)
    enc = encoding or detect_xml_encoding(file_path)
    targets = tuple((enc.encode(pattern), pattern) for pattern in patterns)
    specials = {
        "comment_open": enc.encode("<!--"),
        "comment_close": enc.encode("-->"),
        "cdata_open": enc.encode("<![CDATA["),
        "cdata_close": enc.encode("]]>") ,
        "pi_open": enc.encode("<?"),
        "pi_close": enc.encode("?>"),
        "declaration_open": enc.encode("<!"),
        "declaration_close": enc.encode(">"),
        "double_quote": enc.encode('"'),
        "single_quote": enc.encode("'"),
        "subset_open": enc.encode("["),
        "subset_close": enc.encode("]"),
    }
    max_marker = max(
        [len(item[0]) for item in targets] + [len(item) for item in specials.values()]
    )
    carry = b""
    bytes_read = 0
    state = "normal"
    declaration_quote: bytes | None = None
    declaration_brackets = 0

    def searchable(value: bytes) -> bytes:
        return value.lower() if ignore_case else value

    target_search = tuple((searchable(raw), label) for raw, label in targets)
    special_search = {name: searchable(raw) for name, raw in specials.items()}

    with file_path.open("rb") as handle:
        eof = False
        while not eof:
            block = handle.read(chunk_size)
            eof = not block
            data = carry + block
            base = bytes_read - len(carry)
            safe_end = len(data) if eof else max(0, len(data) - max_marker + enc.unit)
            safe_end -= (base + safe_end) % enc.unit
            view = searchable(data)
            cursor = 0
            next_positions: dict[tuple[int, str], int] = {}

            def next_position(raw: bytes, key: tuple[int, str]) -> int:
                # Cache misses and future hits within this chunk. Dense Rule
                # files must not re-search the entire remaining MiB for each
                # absent marker on every match.
                found = next_positions.get(key)
                if found is None or 0 <= found < cursor:
                    found = _aligned_find(view, raw, cursor, enc.unit, base)
                    next_positions[key] = found
                return found

            while cursor < safe_end:
                if state == "comment":
                    found = _aligned_find(
                        view, special_search["comment_close"], cursor, enc.unit, base
                    )
                    if found < 0 or found >= safe_end:
                        cursor = safe_end
                    else:
                        cursor = found + len(specials["comment_close"])
                        state = "normal"
                    continue
                if state == "cdata":
                    found = _aligned_find(
                        view, special_search["cdata_close"], cursor, enc.unit, base
                    )
                    if found < 0 or found >= safe_end:
                        cursor = safe_end
                    else:
                        cursor = found + len(specials["cdata_close"])
                        state = "normal"
                    continue
                if state == "pi":
                    found = _aligned_find(view, special_search["pi_close"], cursor, enc.unit, base)
                    if found < 0 or found >= safe_end:
                        cursor = safe_end
                    else:
                        cursor = found + len(specials["pi_close"])
                        state = "normal"
                    continue
                if state == "declaration":
                    while cursor < safe_end:
                        token = view[cursor : cursor + enc.unit]
                        if declaration_quote is not None:
                            if token == declaration_quote:
                                declaration_quote = None
                        elif token in {
                            special_search["double_quote"],
                            special_search["single_quote"],
                        }:
                            declaration_quote = token
                        elif token == special_search["subset_open"]:
                            declaration_brackets += 1
                        elif token == special_search["subset_close"]:
                            declaration_brackets = max(0, declaration_brackets - 1)
                        elif (
                            token == special_search["declaration_close"]
                            and declaration_brackets == 0
                        ):
                            cursor += enc.unit
                            state = "normal"
                            break
                        cursor += enc.unit
                    continue

                events: list[tuple[int, int, str]] = []
                for raw, label in target_search:
                    found = next_position(raw, (1, label))
                    if 0 <= found < safe_end:
                        events.append((found, 1, label))
                for marker, priority in (
                    ("comment_open", 0), ("cdata_open", 0),
                    ("pi_open", 0), ("declaration_open", 2),
                ):
                    found = next_position(special_search[marker], (priority, marker))
                    if 0 <= found < safe_end:
                        events.append((found, priority, marker))
                if not events:
                    cursor = safe_end
                    continue
                found, priority, event = min(events)
                if priority == 1:
                    yield VisibleMatch(offset=base + found, pattern=event)
                    raw = enc.encode(event)
                    cursor = found + max(enc.unit, len(raw))
                elif event == "comment_open":
                    state = "comment"
                    cursor = found + len(specials[event])
                elif event == "cdata_open":
                    state = "cdata"
                    cursor = found + len(specials[event])
                elif event == "pi_open":
                    state = "pi"
                    cursor = found + len(specials[event])
                elif event == "declaration_open":
                    state = "declaration"
                    declaration_quote = None
                    declaration_brackets = 0
                    cursor = found + len(specials[event])

            carry = data[safe_end:]
            bytes_read += len(block)
            if block and on_bytes is not None:
                on_bytes(bytes_read)
    if state != "normal":
        raise MalformedXMLStructure(
            f"Unclosed XML {state} section at end of {file_path}"
        )


@dataclass(frozen=True)
class XMLTagSpan:
    name: str
    start: int
    end: int
    is_end: bool = False
    self_closing: bool = False


def _aligned_rfind(data: bytes, needle: bytes, unit: int, base: int) -> int:
    found = data.rfind(needle)
    while found >= 0 and (base + found) % unit:
        found = data.rfind(needle, 0, found)
    return found


def _is_self_closing(
    reader: BinaryIO, start: int, end: int, encoding: XMLByteEncoding
) -> bool:
    sample_start = max(start, end - 512 * encoding.unit)
    sample_start -= sample_start % encoding.unit
    reader.seek(sample_start)
    text = encoding.decode(reader.read(end - sample_start))
    return text.rstrip().endswith("/>")


def find_tag_span(
    path: str | Path,
    offset: int,
    *,
    encoding: XMLByteEncoding | None = None,
    handle: BinaryIO | None = None,
    containing: bool = True,
    chunk_size: int = 256 * 1024,
    max_tag_bytes: int = 512 * 1024 * 1024,
) -> XMLTagSpan | None:
    """Find and parse a start/end tag at or containing an original byte offset."""

    file_path = Path(path)
    enc = encoding or detect_xml_encoding(file_path)
    unit = enc.unit
    lt = enc.encode("<")
    own_handle = handle is None
    reader = handle or file_path.open("rb")
    try:
        if containing:
            cursor = offset - (offset % unit)
            lower_bound = max(0, cursor - max_tag_bytes)
            tag_start: int | None = None
            while cursor > lower_bound:
                begin = max(lower_bound, cursor - chunk_size)
                begin -= begin % unit
                reader.seek(begin)
                block = reader.read(cursor - begin)
                found = _aligned_rfind(block, lt, unit, begin)
                if found >= 0:
                    tag_start = begin + found
                    break
                cursor = begin
            if tag_start is None:
                return None
        else:
            tag_start = offset

        reader.seek(tag_start)
        tokens = {
            name: enc.encode(value)
            for name, value in {
                "lt": "<", "gt": ">", "slash": "/", "bang": "!", "question": "?",
                "double": '"', "single": "'", "space": " ", "tab": "\t",
                "cr": "\r", "lf": "\n",
            }.items()
        }
        whitespace = {tokens["space"], tokens["tab"], tokens["cr"], tokens["lf"]}
        state = "open"
        quote: bytes | None = None
        is_end = False
        name = bytearray()
        scanned = 0
        while scanned < max_tag_bytes:
            read_size = min(chunk_size, 4096 if scanned == 0 else chunk_size, max_tag_bytes - scanned)
            read_size -= read_size % unit
            block = reader.read(read_size)
            if not block:
                raise TagScanLimitExceeded(
                    f"XML tag at byte {tag_start} reaches end of file before closing"
                )
            block_start = tag_start + scanned
            index = 0
            while index < len(block):
                token = block[index : index + unit]
                if state == "open":
                    if token != tokens["lt"]:
                        return None
                    state = "after_lt"
                    index += unit
                elif state == "after_lt":
                    if token == tokens["slash"]:
                        is_end = True
                        state = "tag_name"
                        index += unit
                    elif token in {tokens["bang"], tokens["question"], tokens["gt"]}:
                        return None
                    elif token in whitespace:
                        return None
                    else:
                        name.extend(token)
                        state = "tag_name"
                        index += unit
                elif state == "tag_name":
                    if token in whitespace or token in {tokens["slash"], tokens["gt"]}:
                        state = "in_tag"
                        if token == tokens["gt"]:
                            end = block_start + index + unit
                            return XMLTagSpan(
                                name=enc.decode(bytes(name)), start=tag_start, end=end,
                                is_end=is_end, self_closing=False,
                            )
                    elif len(name) < 4096:
                        name.extend(token)
                    index += unit
                elif quote is not None:
                    found = _aligned_find(block, quote, index, unit, block_start)
                    nested = _aligned_find(block, tokens["lt"], index, unit, block_start)
                    if nested >= 0 and (found < 0 or nested < found):
                        raise MalformedXMLStructure(
                            f"Unexpected '<' in quoted attribute of tag at byte {tag_start}"
                        )
                    if found < 0:
                        index = len(block)
                    else:
                        quote = None
                        index = found + unit
                else:
                    candidates = [
                        _aligned_find(block, item, index, unit, block_start)
                        for item in (tokens["double"], tokens["single"], tokens["gt"], tokens["lt"])
                    ]
                    positions = [item for item in candidates if item >= 0]
                    if not positions:
                        index = len(block)
                        continue
                    found = min(positions)
                    token = block[found : found + unit]
                    if token == tokens["lt"]:
                        raise MalformedXMLStructure(f"Unclosed XML tag at byte {tag_start}")
                    if token == tokens["gt"]:
                        end = block_start + found + unit
                        return XMLTagSpan(
                            name=enc.decode(bytes(name)), start=tag_start, end=end,
                            is_end=is_end,
                            self_closing=(
                                False if is_end else _is_self_closing(reader, tag_start, end, enc)
                            ),
                        )
                    quote = token
                    index = found + unit
            scanned += len(block)
        raise TagScanLimitExceeded(
            f"XML tag at byte {tag_start} exceeds {max_tag_bytes} bytes or is not closed"
        )
    finally:
        if own_handle:
            reader.close()


@dataclass(frozen=True)
class TruncatedAttribute:
    name: str
    observed_bytes: int
    tag_offset: int


@dataclass
class AttributeRead:
    values: dict[str, str] = field(default_factory=dict)
    truncated: list[TruncatedAttribute] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    matched_references: set[str] = field(default_factory=set)


def read_tag_attributes(
    path: str | Path,
    span: XMLTagSpan,
    wanted: Callable[[str], bool],
    *,
    encoding: XMLByteEncoding | None = None,
    handle: BinaryIO | None = None,
    force_capture_offsets: tuple[int, ...] = (),
    reference_patterns: tuple[str, ...] = (),
    ignore_case: bool = False,
    max_value_bytes: int = 1024 * 1024,
    chunk_size: int = 64 * 1024,
) -> AttributeRead:
    """Read selected attributes with bounded values and unknown-hit evidence."""

    file_path = Path(path)
    enc = encoding or detect_xml_encoding(file_path)
    unit = enc.unit
    result = AttributeRead()
    state = "open"
    name = bytearray()
    value = bytearray()
    should_capture = False
    forced_late = False
    quote: int | None = None
    value_length = 0
    remaining = span.end - span.start
    position = span.start
    force_offsets = set(force_capture_offsets)
    reference_bytes = tuple((enc.encode(item), item) for item in reference_patterns)
    if ignore_case:
        reference_bytes = tuple((raw.lower(), label) for raw, label in reference_bytes)
    overlap = max((len(raw) - unit for raw, _ in reference_bytes), default=0)
    value_tail = b""
    own_handle = handle is None
    reader = handle or file_path.open("rb")

    def text(raw: bytes) -> str:
        return unescape(enc.decode(raw))

    def current_name() -> str:
        return enc.decode(bytes(name))

    def finish_value() -> None:
        nonlocal forced_late
        if not should_capture:
            return
        attribute_name = current_name()
        rendered = text(bytes(value))
        if forced_late:
            rendered = "…" + rendered
        if value_length > max_value_bytes:
            rendered += (
                f" … [TRUNCATED observed_bytes={value_length} "
                f"tag_offset={span.start} attribute={attribute_name}]"
            )
            result.truncated.append(
                TruncatedAttribute(attribute_name, value_length, span.start)
            )
        if attribute_name in result.values:
            result.duplicates.append(attribute_name)
        result.values[attribute_name] = rendered
        forced_late = False

    try:
        reader.seek(span.start)
        while remaining > 0:
            read_size = min(chunk_size, remaining)
            read_size -= read_size % unit
            if read_size <= 0:
                break
            block = reader.read(read_size)
            if not block:
                break
            remaining -= len(block)
            index = 0
            while index < len(block):
                if state == "quoted_value":
                    # Skip/capture runs of bytes, including values that are not
                    # selected. Decode only the bounded captured value.
                    end = _aligned_find(block, enc.encode(chr(quote)), index, unit, position)
                    stop = len(block) if end < 0 else end
                    segment = block[index:stop]
                    probe = value_tail + segment
                    search = probe.lower() if ignore_case else probe
                    reference_hits = []
                    for raw, label in reference_bytes:
                        found = _aligned_find(search, raw, 0, unit, position + index - len(value_tail))
                        if found >= 0:
                            result.matched_references.add(label)
                            reference_hits.append(found)
                    captured = segment
                    if not should_capture:
                        hits = reference_hits + [len(value_tail) + offset - position - index
                                                for offset in force_offsets
                                                if position + index <= offset < position + stop]
                        if hits:
                            should_capture = True
                            forced_late = True
                            captured = probe[min(hits):]
                    value_length += stop - index
                    if should_capture:
                        available = max(0, max_value_bytes - len(value))
                        available -= available % unit
                        value.extend(captured[:available])
                    value_tail = probe[-overlap:] if overlap else b""
                    if end < 0:
                        break
                    finish_value()
                    state = "before_name"
                    index = end + unit
                    continue
                token = block[index : index + unit]
                absolute = position + index
                # Delimiters are ASCII; do not decode individual UTF-8 bytes
                # or UTF-16 surrogate halves.
                char = int.from_bytes(token, "big" if enc.codec == "utf-16-be" else "little")
                index += unit
                if state == "open":
                    if char == ord("<"):
                        state = "tag_name"
                    continue
                if state == "tag_name":
                    if char == ord(">"):
                        return result
                    if char in b" \t\r\n/>":
                        state = "before_name"
                    continue
                if state == "before_name":
                    if char in b" \t\r\n/":
                        continue
                    if char == ord(">"):
                        return result
                    if char in b"<=\"'":
                        raise MalformedXMLStructure(f"Invalid attribute at byte {absolute}")
                    name.clear()
                    name.extend(token)
                    state = "name"
                    continue
                if state == "name":
                    if char == ord("="):
                        should_capture = wanted(current_name())
                        forced_late = False
                        value.clear()
                        value_length = 0
                        state = "before_value"
                    elif char in b" \t\r\n":
                        state = "after_name"
                    elif char in b"/>":
                        raise MalformedXMLStructure(f"Attribute without a value at byte {absolute}")
                    elif len(name) < 4096:
                        name.extend(token)
                    continue
                if state == "after_name":
                    if char in b" \t\r\n":
                        continue
                    if char == ord("="):
                        should_capture = wanted(current_name())
                        forced_late = False
                        value.clear()
                        value_length = 0
                        state = "before_value"
                    else:
                        raise MalformedXMLStructure(f"Attribute without '=' at byte {absolute}")
                    continue
                if state == "before_value":
                    if char in b" \t\r\n":
                        continue
                    if char in (ord('"'), ord("'")):
                        quote = char
                        value_tail = b""
                        state = "quoted_value"
                    else:
                        raise MalformedXMLStructure(f"Unquoted attribute value at byte {absolute}")
            position += len(block)
    finally:
        if own_handle:
            reader.close()
    raise MalformedXMLStructure(f"Incomplete attributes in tag at byte {span.start}")
