"""Fast, conservative lookup of IFP node eids.

The index in this module is only a candidate accelerator.  Its results must
be confirmed by :class:`TraceSource`, since an eid may occur in ordinary text
or in a comment.  Files are fingerprinted with stat metadata so unchanged
files are not opened again on subsequent lookups.
"""

from __future__ import annotations

import html
import codecs
import hashlib
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Iterable

from .xmlbytes import detect_xml_encoding


_CHUNK_SIZE = 1024 * 1024
_MAX_PENDING = 8 * 1024 * 1024
_EID_TOKEN = re.compile(r"\beid", re.IGNORECASE)
_ATTRIBUTE_OPEN = re.compile(r"\s*=\s*(['\"])")
_ATTRIBUTE_PREFIX = re.compile(r"\s*(?:=\s*)?$")


class LookupIncompleteError(ValueError):
    """The candidate scan could not establish a complete index."""


def _ifp_files(root: Path) -> list[Path]:
    """Return deterministic, in-root IFP files without following symlinks."""

    root = root.resolve()
    if root.is_file():
        return [root] if root.suffix.lower() == ".ifp" else []
    if not root.is_dir():
        return []
    result: list[Path] = []
    seen: set[Path] = set()

    def onerror(error: OSError) -> None:
        raise LookupIncompleteError(f"cannot enumerate IFP root {root}: {error}") from error

    for directory, names, files in os.walk(root, followlinks=False, onerror=onerror):
        names[:] = sorted(name for name in names if name != ".git")
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() != ".ifp":
                continue
            try:
                resolved = path.resolve(strict=True)
                if resolved.is_relative_to(root) and resolved.is_file() and resolved not in seen:
                    seen.add(resolved)
                    result.append(resolved)
            except OSError as error:
                raise LookupIncompleteError(f"cannot inspect {path}: {error}") from error
    return sorted(result, key=lambda item: item.as_posix())


def _text_chunks(path: Path) -> Iterable[str]:
    """Decode a file incrementally, preserving UTF-16 character boundaries."""

    encoding = detect_xml_encoding(path)
    decoder = codecs.getincrementaldecoder(encoding.codec)("replace")
    with path.open("rb") as handle:
        while True:
            block = handle.read(_CHUNK_SIZE)
            if not block:
                break
            yield decoder.decode(block, final=False)
        tail = decoder.decode(b"", final=True)
        if tail:
            yield tail


def _eids_in_file(path: Path) -> set[str]:
    """Extract eid attribute values with bounded memory.

    The scan intentionally does not attempt to establish XML structure.  It
    may return a false positive from a comment, while a value longer than the
    bounded safety window raises explicitly instead of silently missing it.
    """

    found: set[str] = set()
    pending = ""
    for chunk in _text_chunks(path):
        data = pending + chunk
        pending_start = None
        # Match each potential attribute independently: text or comments with
        # quotes must not hide a real attribute nested in a candidate match.
        # Avoid comparing every token with every match in large IFP chunks.
        for token in _EID_TOKEN.finditer(data):
            opening = _ATTRIBUTE_OPEN.match(data, token.end())
            if opening:
                end = data.find(opening.group(1), opening.end())
                if end >= 0:
                    value = html.unescape(data[opening.end():end])
                    if value:
                        found.add(value)
                    continue
            elif not _ATTRIBUTE_PREFIX.fullmatch(data, token.end()):
                continue
            if pending_start is None:
                pending_start = token.start()
        pending = ""
        if pending_start is not None:
            pending = data[pending_start:]
        else:
            for length in (2, 1):
                suffix = data[-length:].lower()
                if "eid".startswith(suffix):
                    pending = data[-length:]
                    break
        if len(pending) > _MAX_PENDING:
            raise ValueError(f"eid attribute exceeds {_MAX_PENDING} characters: {path}")
    return found


def _stat(path: Path) -> tuple[int, int, int, int, int]:
    value = path.stat()
    return value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_ino, value.st_dev


def _schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            ctime_ns INTEGER NOT NULL, inode INTEGER NOT NULL, device INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS eids (eid TEXT NOT NULL, path TEXT NOT NULL,
                                         PRIMARY KEY (eid, path));
        CREATE INDEX IF NOT EXISTS eids_by_value ON eids(eid);
        """
    )


def _scan_direct(paths: list[Path], eid: str) -> list[Path]:
    result: list[Path] = []
    progress = time.monotonic()
    for number, path in enumerate(paths, 1):
        try:
            before = _stat(path)
            values = _eids_in_file(path)
            after = _stat(path)
            if before != after:
                raise LookupIncompleteError(f"file changed while scanning: {path}")
            if eid in values:
                result.append(path)
        except (OSError, UnicodeError) as error:
            raise LookupIncompleteError(f"cannot read {path}: {error}") from error
        if time.monotonic() - progress >= 2:
            print(f"trace eid scan: {number} files", file=sys.stderr)
            progress = time.monotonic()
    return result


def candidate_files(root: Path, eid: str, cache_dir: Path | None = None) -> list[Path]:
    """Find files that may contain ``eid``.

    ``cache_dir`` enables a persistent incremental SQLite accelerator.  The
    returned paths are absolute, deterministic, and are only candidates;
    callers must parse them authoritatively.  Index or permission failures
    fall back to a direct scan so lookup remains safe.
    """

    if not isinstance(eid, str) or not eid:
        raise ValueError("eid must be a non-empty string")
    paths = _ifp_files(Path(root))
    if cache_dir is None:
        return _scan_direct(paths, eid)

    root_key = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:24]
    db_path = Path(cache_dir) / f"trace-eids-v1-{root_key}.sqlite3"
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db_path, timeout=30)) as db, db:
            _schema(db)
            current = {str(path.resolve()): path for path in paths}
            rows = db.execute("SELECT path, size, mtime_ns, ctime_ns, inode, device FROM files").fetchall()
            known = {row[0]: row[1:] for row in rows}
            for old in set(known) - set(current):
                db.execute("DELETE FROM eids WHERE path = ?", (old,))
                db.execute("DELETE FROM files WHERE path = ?", (old,))
            progress = time.monotonic()
            for number, (key, path) in enumerate(current.items(), 1):
                try:
                    fingerprint = _stat(path)
                except OSError as error:
                    raise LookupIncompleteError(f"cannot inspect {path}: {error}") from error
                if known.get(key) == fingerprint:
                    if time.monotonic() - progress >= 2:
                        print(f"trace eid scan: {number} files", file=sys.stderr)
                        progress = time.monotonic()
                    continue
                db.execute("DELETE FROM eids WHERE path = ?", (key,))
                try:
                    before = fingerprint
                    values = _eids_in_file(path)
                except (OSError, UnicodeError):
                    raise LookupIncompleteError(f"cannot read {path}")
                if before != _stat(path):
                    raise LookupIncompleteError(f"file changed while scanning: {path}")
                db.executemany("INSERT OR IGNORE INTO eids(eid, path) VALUES (?, ?)",
                               ((value, key) for value in values))
                db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?)", (key, *fingerprint))
                if time.monotonic() - progress >= 2:
                    print(f"trace eid scan: {number} files", file=sys.stderr)
                    progress = time.monotonic()
            return [Path(row[0]) for row in db.execute(
                "SELECT path FROM eids WHERE eid = ? ORDER BY path", (eid,)
            )]
    except (OSError, sqlite3.Error):
        return _scan_direct(paths, eid)
