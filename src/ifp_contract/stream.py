"""Streaming XML helpers for huge IFP files.

This module deliberately does not build an XML tree.  It recognizes XML start
tags from a binary stream, so memory use stays independent of the IFP file
size.  The tiny attribute reader is then used only on tags already known to be
relevant.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class TagSpan:
    """Byte range for one XML start tag (``end`` is exclusive)."""

    name: str
    start: int
    end: int
    contains_needle: bool


def _lps(needle: bytes) -> list[int]:
    """Return the KMP prefix table used for constant-memory byte matching."""

    table = [0] * len(needle)
    size = 0
    for index in range(1, len(needle)):
        while size and needle[index] != needle[size]:
            size = table[size - 1]
        if needle[index] == needle[size]:
            size += 1
            table[index] = size
    return table


def iter_start_tags(
    path: str | Path,
    *,
    needle: bytes | None = None,
    chunk_size: int = 256 * 1024,
) -> Iterator[TagSpan]:
    """Yield XML start-tag locations without retaining the file or its tags.

    ``needle`` is checked only while reading a start tag.  It is deliberately a
    byte match: callers can use an exact IFP component reference without XML
    parsing or regular-expression backtracking.
    """

    file_path = Path(path)
    prefix_table = _lps(needle) if needle else []
    state = "outside"
    quote: int | None = None
    tag_start = 0
    name = bytearray()
    match_index = 0
    contains_needle = False

    def advance_match(value: int) -> None:
        nonlocal match_index, contains_needle
        if not needle or contains_needle:
            return
        while match_index and value != needle[match_index]:
            match_index = prefix_table[match_index - 1]
        if value == needle[match_index]:
            match_index += 1
            if match_index == len(needle):
                contains_needle = True
                match_index = prefix_table[match_index - 1]

    with file_path.open("rb") as handle:
        offset = 0
        while block := handle.read(chunk_size):
            for value in block:
                if state == "outside":
                    if value == ord("<"):
                        tag_start = offset
                        state = "after_lt"
                    offset += 1
                    continue

                if state == "after_lt":
                    # End tags, declarations and processing instructions are
                    # skipped.  No IFP contract data is stored on them.
                    if value in (ord("/"), ord("!"), ord("?")):
                        state = "skip"
                        quote = None
                    elif value in b" \t\r\n":
                        state = "outside"
                    elif value == ord(">"):
                        state = "outside"
                    else:
                        name.clear()
                        name.append(value)
                        match_index = 0
                        contains_needle = False
                        advance_match(value)
                        quote = None
                        state = "tag_name"
                    offset += 1
                    continue

                if state == "skip":
                    if quote is None and value in (ord('"'), ord("'")):
                        quote = value
                    elif quote == value:
                        quote = None
                    elif quote is None and value == ord(">"):
                        state = "outside"
                    offset += 1
                    continue

                if state == "tag_name":
                    advance_match(value)
                    if value in b" \t\r\n/>":
                        state = "in_tag"
                        if value == ord(">"):
                            yield TagSpan(
                                name=name.decode("utf-8", "replace"),
                                start=tag_start,
                                end=offset + 1,
                                contains_needle=contains_needle,
                            )
                            state = "outside"
                    elif len(name) < 256:
                        name.append(value)
                    offset += 1
                    continue

                # state == "in_tag"
                advance_match(value)
                if quote is None:
                    if value in (ord('"'), ord("'")):
                        quote = value
                    elif value == ord(">"):
                        yield TagSpan(
                            name=name.decode("utf-8", "replace"),
                            start=tag_start,
                            end=offset + 1,
                            contains_needle=contains_needle,
                        )
                        state = "outside"
                elif value == quote:
                    quote = None
                offset += 1


AttributeWanted = Callable[[str], bool]


class TagScanLimitExceeded(ValueError):
    """Raised when a malformed/unclosed start tag exceeds the safety window."""


class UnsupportedIFPEncoding(ValueError):
    """Raised instead of silently missing references in unsupported XML bytes."""


def ensure_supported_encoding(path: str | Path) -> None:
    """Reject UTF-16 IFP input explicitly; the byte slicer currently uses UTF-8."""

    with Path(path).open("rb") as handle:
        prefix = handle.read(4)
    utf16_bom = prefix.startswith((b"\xff\xfe", b"\xfe\xff"))
    utf16_pattern = len(prefix) >= 2 and (
        (prefix[0] == ord("<") and prefix[1] == 0)
        or (prefix[0] == 0 and prefix[1] == ord("<"))
    )
    if utf16_bom or utf16_pattern:
        raise UnsupportedIFPEncoding(
            f"UTF-16 IFP is not supported by the byte slicer: {path}; convert/export it as UTF-8"
        )


def read_selected_attributes(
    path: str | Path,
    start: int,
    end: int,
    wanted: AttributeWanted,
    *,
    chunk_size: int = 64 * 1024,
    handle: BinaryIO | None = None,
) -> dict[str, str]:
    """Read selected attributes from one known start tag.

    Attribute names are buffered only up to 512 bytes.  Attribute values are
    retained only after ``wanted`` accepts their name; irrelevant values are
    skipped byte-for-byte.  IFP contract attributes are normally short paths,
    while the full IFP and any unrelated XML property never enter memory.
    """

    file_path = Path(path)
    result: dict[str, str] = {}
    state = "open"
    name = bytearray()
    value = bytearray()
    should_capture = False
    quote: int | None = None
    remaining = end - start

    def current_name() -> str:
        return name.decode("utf-8", "replace")

    def finish_value() -> None:
        if should_capture:
            result[current_name()] = unescape(value.decode("utf-8", "replace"))

    own_handle = handle is None
    reader = handle or file_path.open("rb")
    try:
        reader.seek(start)
        while remaining > 0:
            block = reader.read(min(chunk_size, remaining))
            if not block:
                break
            remaining -= len(block)
            for item in block:
                if state == "open":
                    if item == ord("<"):
                        state = "tag_name"
                    continue

                if state == "tag_name":
                    if item in b" \t\r\n/>":
                        state = "before_name"
                    continue

                if state == "before_name":
                    if item in b" \t\r\n/":
                        continue
                    if item == ord(">"):
                        return result
                    name.clear()
                    name.append(item)
                    state = "name"
                    continue

                if state == "name":
                    if item == ord("="):
                        should_capture = wanted(current_name())
                        value.clear()
                        state = "before_value"
                    elif item in b" \t\r\n":
                        state = "after_name"
                    elif item in b"/>":
                        state = "before_name"
                    elif len(name) < 512:
                        name.append(item)
                    continue

                if state == "after_name":
                    if item in b" \t\r\n":
                        continue
                    if item == ord("="):
                        should_capture = wanted(current_name())
                        value.clear()
                        state = "before_value"
                    else:
                        state = "before_name"
                    continue

                if state == "before_value":
                    if item in b" \t\r\n":
                        continue
                    if item in (ord('"'), ord("'")):
                        quote = item
                        state = "quoted_value"
                    else:
                        if should_capture:
                            value.append(item)
                        state = "bare_value"
                    continue

                if state == "quoted_value":
                    if item == quote:
                        finish_value()
                        state = "before_name"
                    elif should_capture:
                        value.append(item)
                    continue

                # state == "bare_value"
                if item in b" \t\r\n/>":
                    finish_value()
                    state = "before_name"
                    if item == ord(">"):
                        return result
                elif should_capture:
                    value.append(item)

    finally:
        if own_handle:
            reader.close()
    return result


def contains_bytes(
    path: str | Path,
    needle: bytes,
    *,
    chunk_size: int = 1024 * 1024,
) -> bool:
    """Return whether a file contains ``needle`` using a bounded byte stream."""

    matches = iter_occurrence_offsets(path, needle, chunk_size=chunk_size)
    try:
        return next(matches, None) is not None
    finally:
        matches.close()


def iter_occurrence_offsets(
    path: str | Path,
    needle: bytes,
    *,
    chunk_size: int = 1024 * 1024,
    on_bytes: Callable[[int], None] | None = None,
) -> Iterator[int]:
    """Yield every byte offset where ``needle`` begins, with O(1) file memory.

    The search itself runs inside CPython's C implementation of
    :meth:`bytes.find`; Python only handles actual matches and chunk
    boundaries.  That distinction matters when normal inputs are hundreds of
    megabytes or several gigabytes.
    """

    if not needle:
        return
    overlap_size = max(0, len(needle) - 1)
    overlap = b""
    bytes_read = 0
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk_size):
            window = overlap + block
            window_start = bytes_read - len(overlap)
            search_at = 0
            while True:
                found = window.find(needle, search_at)
                if found < 0:
                    break
                yield window_start + found
                search_at = found + 1
            bytes_read += len(block)
            if on_bytes is not None:
                on_bytes(bytes_read)
            overlap = window[-overlap_size:] if overlap_size else b""


def iter_target_start_tags(
    path: str | Path,
    tag_names: tuple[bytes, ...],
    *,
    chunk_size: int = 1024 * 1024,
    handle: BinaryIO | None = None,
) -> Iterator[TagSpan]:
    """Find selected unprefixed XML start tags using C-level byte searches.

    IFP exports use stable, case-sensitive tag names such as ``Rule`` and
    ``DataSource``. All requested names share one sequential pass; only actual
    candidates invoke the quote-aware tag boundary scanner.
    """

    file_path = Path(path)
    own_handle = handle is None
    reader = handle or file_path.open("rb")
    needles = tuple((b"<" + name, name) for name in tag_names)
    overlap_size = max((len(needle) - 1 for needle, _ in needles), default=0)
    try:
        overlap = b""
        bytes_read = 0
        with file_path.open("rb") as scanner:
            while block := scanner.read(chunk_size):
                window = overlap + block
                window_start = bytes_read - len(overlap)
                candidates: list[tuple[int, bytes]] = []
                for needle, tag_name in needles:
                    search_at = 0
                    while True:
                        found = window.find(needle, search_at)
                        if found < 0:
                            break
                        absolute = window_start + found
                        # Matches wholly inside overlap were emitted with the
                        # previous block; crossing matches are new.
                        if not bytes_read or absolute + len(needle) > bytes_read:
                            candidates.append((absolute, tag_name))
                        search_at = found + 1
                for offset, tag_name in sorted(candidates):
                    span = tag_span_containing(file_path, offset + 1, handle=reader)
                    if span is None or span.start != offset:
                        continue
                    if span.name.encode("utf-8", "replace") != tag_name:
                        continue
                    yield span
                bytes_read += len(block)
                overlap = window[-overlap_size:] if overlap_size else b""
    finally:
        if own_handle:
            reader.close()


def tag_span_containing(
    path: str | Path,
    offset: int,
    *,
    chunk_size: int = 256 * 1024,
    handle: BinaryIO | None = None,
    max_tag_bytes: int = 512 * 1024 * 1024,
) -> TagSpan | None:
    """Find the XML start tag that contains a known byte offset.

    This lets callers use a grep-like corpus pass and then seek only around a
    matching reference, rather than parsing a matched IFP from beginning to
    end.  XML requires ``<`` to be escaped in an attribute value, so the
    closest preceding ``<`` is the containing tag for valid IFP XML.
    """

    file_path = Path(path)
    own_handle = handle is None
    reader = handle or file_path.open("rb")
    try:
        cursor = offset
        tag_start: int | None = None
        while cursor > 0:
            begin = max(0, cursor - chunk_size)
            reader.seek(begin)
            block = reader.read(cursor - begin)
            found = block.rfind(b"<")
            if found >= 0:
                tag_start = begin + found
                break
            cursor = begin
        if tag_start is None:
            return None

        reader.seek(tag_start)
        state = "open"
        quote: int | None = None
        name = bytearray()
        scanned = 0
        while scanned < max_tag_bytes:
            block = reader.read(min(chunk_size, max_tag_bytes - scanned))
            if not block:
                return None
            block_start = tag_start + scanned
            index = 0
            while index < len(block):
                value = block[index]
                if state == "open":
                    if value != ord("<"):
                        return None
                    state = "after_lt"
                    index += 1
                elif state == "after_lt":
                    if value in (ord("<"), ord("/"), ord("!"), ord("?"), ord(">")):
                        return None
                    if value in b" \t\r\n":
                        return None
                    name.append(value)
                    state = "tag_name"
                    index += 1
                elif state == "tag_name":
                    if value in b" \t\r\n/>":
                        state = "in_tag"
                        if value == ord(">"):
                            return TagSpan(
                                name=name.decode("utf-8", "replace"),
                                start=tag_start,
                                end=block_start + index + 1,
                                contains_needle=True,
                            )
                    elif len(name) < 256:
                        name.append(value)
                    index += 1
                elif quote is not None:
                    found = block.find(bytes((quote,)), index)
                    if found < 0:
                        index = len(block)
                    else:
                        quote = None
                        index = found + 1
                else:
                    double_quote = block.find(b'"', index)
                    single_quote = block.find(b"'", index)
                    closing = block.find(b">", index)
                    positions = [item for item in (double_quote, single_quote, closing) if item >= 0]
                    if not positions:
                        index = len(block)
                        continue
                    found = min(positions)
                    value = block[found]
                    if value == ord(">"):
                        return TagSpan(
                            name=name.decode("utf-8", "replace"),
                            start=tag_start,
                            end=block_start + found + 1,
                            contains_needle=True,
                        )
                    quote = value
                    index = found + 1
            scanned += len(block)
        raise TagScanLimitExceeded(
            f"XML start tag at byte {tag_start} exceeds {max_tag_bytes} bytes or is not closed"
        )
    finally:
        if own_handle:
            reader.close()
