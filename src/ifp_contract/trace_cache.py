"""Small, defensive JSON cache for :class:`TraceSource` structural indexes."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


CACHE_SCHEMA_VERSION = 2


def file_fingerprint(path: Path) -> dict[str, int]:
    """Return metadata used to detect replacement or mutation of a source file."""
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "inode": stat.st_ino,
        "device": stat.st_dev,
    }


def cache_key(*, relative: str, sha256: str, extra_metadata: tuple[str, ...], max_nodes: int) -> dict[str, Any]:
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "relative": relative,
        "sha256": sha256,
        "extra_metadata": list(extra_metadata),
        "max_nodes": max_nodes,
    }


def cache_filename(key: dict[str, Any]) -> str:
    encoded = json.dumps(key, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest() + ".json"


def load_cache(cache_dir: Path, key: dict[str, Any], fingerprint: dict[str, int]) -> dict[str, Any] | None:
    """Load a cache entry, treating every malformed or stale entry as a miss."""
    try:
        cache_path = cache_dir / cache_filename(key)
        with cache_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or payload.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None
        if payload.get("key") != key or payload.get("fingerprint") != fingerprint:
            return None
        if not isinstance(payload.get("nodes"), list) or not isinstance(payload.get("issues"), list):
            return None
        if not isinstance(payload.get("encoding"), str):
            return None
        return payload
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def save_cache(cache_dir: Path, key: dict[str, Any], fingerprint: dict[str, int], payload: dict[str, Any]) -> bool:
    """Atomically write a cache entry. Cache failures never affect tracing."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / cache_filename(key)
        document = dict(payload)
        document.update({"schema_version": CACHE_SCHEMA_VERSION, "key": key, "fingerprint": fingerprint})
        fd, temporary = tempfile.mkstemp(prefix=".trace-cache-", suffix=".tmp", dir=cache_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, cache_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return True
    except (OSError, TypeError, ValueError):
        return False
