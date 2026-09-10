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

    if not needle:
        return False
    prefix_table = _lps(needle)
    match_index = 0
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk_size):
            for value in block:
                while match_index and value != needle[match_index]:
                    match_index = prefix_table[match_index - 1]
                if value == needle[match_index]:
                    match_index += 1
                    if match_index == len(needle):
                        return True
    return False


def iter_occurrence_offsets(
    path: str | Path,
    needle: bytes,
    *,
    chunk_size: int = 1024 * 1024,
) -> Iterator[int]:
    """Yield every byte offset where ``needle`` begins, with O(1) file memory."""

    if not needle:
        return
    prefix_table = _lps(needle)
    match_index = 0
    offset = 0
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk_size):
            for value in block:
                while match_index and value != needle[match_index]:
                    match_index = prefix_table[match_index - 1]
                if value == needle[match_index]:
                    match_index += 1
                    if match_index == len(needle):
                        yield offset - len(needle) + 1
                        match_index = prefix_table[match_index - 1]
                offset += 1


def tag_span_containing(
    path: str | Path,
    offset: int,
    *,
    chunk_size: int = 256 * 1024,
) -> TagSpan | None:
    """Find the XML start tag that contains a known byte offset.

    This lets callers use a grep-like corpus pass and then seek only around a
    matching reference, rather than parsing a matched IFP from beginning to
    end.  XML requires ``<`` to be escaped in an attribute value, so the
    closest preceding ``<`` is the containing tag for valid IFP XML.
    """

    file_path = Path(path)
    with file_path.open("rb") as handle:
        cursor = offset
        tag_start: int | None = None
        while cursor > 0:
            begin = max(0, cursor - chunk_size)
            handle.seek(begin)
            block = handle.read(cursor - begin)
            found = block.rfind(b"<")
            if found >= 0:
                tag_start = begin + found
                break
            cursor = begin
        if tag_start is None:
            return None

        handle.seek(tag_start)
        state = "open"
        quote: int | None = None
        name = bytearray()
        position = tag_start
        while block := handle.read(chunk_size):
            for value in block:
                if state == "open":
                    if value != ord("<"):
                        return None
                    state = "after_lt"
                elif state == "after_lt":
                    if value in (ord("<"), ord("/"), ord("!"), ord("?"), ord(">")):
                        return None
                    if value in b" \t\r\n":
                        return None
                    name.append(value)
                    state = "tag_name"
                elif state == "tag_name":
                    if value in b" \t\r\n/>":
                        state = "in_tag"
                        if value == ord(">"):
                            return TagSpan(
                                name=name.decode("utf-8", "replace"),
                                start=tag_start,
                                end=position + 1,
                                contains_needle=True,
                            )
                    elif len(name) < 256:
                        name.append(value)
                elif quote is None:
                    if value in (ord('"'), ord("'")):
                        quote = value
                    elif value == ord(">"):
                        return TagSpan(
                            name=name.decode("utf-8", "replace"),
                            start=tag_start,
                            end=position + 1,
                            contains_needle=True,
                        )
                elif value == quote:
                    quote = None
                position += 1
    return None
