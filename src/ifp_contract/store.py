"""Small SQLite store for extracted data contracts, not for an IFP graph."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS odata_operations (
    id INTEGER PRIMARY KEY,
    integrator_file TEXT NOT NULL,
    tag_offset INTEGER NOT NULL,
    rule_eid TEXT,
    rule_name TEXT,
    rule_class TEXT NOT NULL,
    product_name TEXT,
    product_eid TEXT,
    phase_name TEXT,
    classification TEXT NOT NULL DEFAULT 'CONFIRMED',
    rule_type TEXT,
    disabled INTEGER NOT NULL DEFAULT 0,
    source_name TEXT,
    base_url TEXT,
    action TEXT,
    api_path TEXT,
    filter_expr TEXT,
    request_group TEXT,
    target_group TEXT,
    results_group TEXT,
    output_group TEXT,
    attributes_json TEXT NOT NULL,
    UNIQUE(integrator_file, tag_offset)
);

CREATE TABLE IF NOT EXISTS caller_references (
    id INTEGER PRIMARY KEY,
    caller_file TEXT NOT NULL,
    tag_offset INTEGER NOT NULL,
    rule_eid TEXT,
    rule_name TEXT,
    rule_class TEXT,
    classification TEXT NOT NULL DEFAULT 'CONFIRMED',
    rule_type TEXT,
    disabled INTEGER NOT NULL DEFAULT 0,
    selector TEXT,
    source_name TEXT,
    component_list TEXT,
    attributes_json TEXT NOT NULL,
    UNIQUE(caller_file, tag_offset)
);

CREATE TABLE IF NOT EXISTS caller_mappings (
    id INTEGER PRIMARY KEY,
    reference_id INTEGER NOT NULL REFERENCES caller_references(id) ON DELETE CASCADE,
    mapping_prefix TEXT NOT NULL,
    solution_data_item TEXT,
    property_key TEXT,
    direction TEXT NOT NULL,
    class_type TEXT,
    attributes_json TEXT NOT NULL,
    UNIQUE(reference_id, mapping_prefix)
);

CREATE TABLE IF NOT EXISTS integrator_mappings (
    id INTEGER PRIMARY KEY,
    integrator_file TEXT NOT NULL,
    product_name TEXT,
    product_eid TEXT,
    tag_offset INTEGER NOT NULL,
    mapping_prefix TEXT NOT NULL,
    exported_property TEXT,
    property_key TEXT,
    direction TEXT NOT NULL,
    class_type TEXT,
    attributes_json TEXT NOT NULL,
    UNIQUE(integrator_file, tag_offset, mapping_prefix)
);

CREATE TABLE IF NOT EXISTS caller_operation_links (
    id INTEGER PRIMARY KEY,
    reference_id INTEGER NOT NULL REFERENCES caller_references(id) ON DELETE CASCADE,
    operation_id INTEGER NOT NULL REFERENCES odata_operations(id) ON DELETE CASCADE,
    resolution_status TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE(reference_id, operation_id)
);

CREATE TABLE IF NOT EXISTS data_sources (
    id INTEGER PRIMARY KEY,
    integrator_file TEXT NOT NULL,
    tag_offset INTEGER NOT NULL,
    source_name TEXT,
    class_type TEXT,
    base_url TEXT,
    attributes_json TEXT NOT NULL,
    UNIQUE(integrator_file, tag_offset)
);

CREATE TABLE IF NOT EXISTS rule_class_census (
    integrator_file TEXT NOT NULL,
    rule_class TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL,
    PRIMARY KEY(integrator_file, rule_class)
);

CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    file_path TEXT,
    tag_offset INTEGER,
    rule_class TEXT,
    message TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_files (
    file_path TEXT PRIMARY KEY,
    file_size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    reference_signature TEXT NOT NULL,
    status TEXT NOT NULL,
    reference_hit_count INTEGER NOT NULL DEFAULT 0,
    caller_count INTEGER NOT NULL,
    diagnostic_count INTEGER NOT NULL,
    completed_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_caller_references_file ON caller_references(caller_file);
CREATE INDEX IF NOT EXISTS idx_caller_mappings_reference ON caller_mappings(reference_id);
CREATE INDEX IF NOT EXISTS idx_caller_operation_links_reference ON caller_operation_links(reference_id);
CREATE INDEX IF NOT EXISTS idx_diagnostics_code ON diagnostics(code);
"""


class ContractStore:
    """Fresh, compact SQLite materialization for one extraction run."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = DELETE")
        self.connection.executescript(SCHEMA)
        self._migrate()
        self.connection.commit()

    def _migrate(self) -> None:
        """Add columns introduced after 0.1 without destroying an existing DB."""

        additions = {
            "odata_operations": {
                "product_name": "TEXT",
                "product_eid": "TEXT",
                "phase_name": "TEXT",
                "classification": "TEXT NOT NULL DEFAULT 'CONFIRMED'",
                "rule_type": "TEXT",
                "disabled": "INTEGER NOT NULL DEFAULT 0",
                "base_url": "TEXT",
            },
            "caller_references": {
                "classification": "TEXT NOT NULL DEFAULT 'CONFIRMED'",
                "rule_type": "TEXT",
                "disabled": "INTEGER NOT NULL DEFAULT 0",
            },
            "scan_files": {
                "reference_hit_count": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        for table, columns in additions.items():
            existing = {
                str(row[1]).casefold()
                for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            for name, declaration in columns.items():
                if name.casefold() not in existing:
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )
        self.connection.execute("PRAGMA user_version = 4")

    def reset(self) -> None:
        self.connection.execute("DELETE FROM caller_mappings")
        self.connection.execute("DELETE FROM caller_operation_links")
        self.connection.execute("DELETE FROM caller_references")
        self.connection.execute("DELETE FROM odata_operations")
        self.connection.execute("DELETE FROM integrator_mappings")
        self.connection.execute("DELETE FROM data_sources")
        self.connection.execute("DELETE FROM rule_class_census")
        self.connection.execute("DELETE FROM diagnostics")
        self.connection.execute("DELETE FROM scan_files")
        self.connection.execute("DELETE FROM metadata")

    def reset_integrator_results(self, integrator_file: str) -> None:
        self.connection.execute("DELETE FROM caller_operation_links")
        self.connection.execute("DELETE FROM odata_operations")
        self.connection.execute("DELETE FROM integrator_mappings")
        self.connection.execute("DELETE FROM data_sources")
        self.connection.execute("DELETE FROM rule_class_census")
        self.connection.execute("DELETE FROM diagnostics WHERE file_path = ?", (integrator_file,))

    def metadata_value(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row[0]) if row else None

    def file_is_cached(
        self,
        file_path: str,
        file_size: int,
        mtime_ns: int,
        reference_signature: str,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM scan_files
            WHERE file_path = ? AND file_size = ? AND mtime_ns = ?
              AND reference_signature = ? AND status = 'complete'
            """,
            (file_path, file_size, mtime_ns, reference_signature),
        ).fetchone()
        return row is not None

    def clear_file_results(self, file_path: str) -> None:
        self.connection.execute("DELETE FROM caller_references WHERE caller_file = ?", (file_path,))
        self.connection.execute("DELETE FROM diagnostics WHERE file_path = ?", (file_path,))
        self.connection.execute("DELETE FROM scan_files WHERE file_path = ?", (file_path,))

    def mark_file_scanned(
        self,
        file_path: str,
        file_size: int,
        mtime_ns: int,
        reference_signature: str,
        status: str,
        reference_hit_count: int,
        caller_count: int,
        diagnostic_count: int,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO scan_files(
                file_path, file_size, mtime_ns, reference_signature, status,
                reference_hit_count, caller_count, diagnostic_count, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_path, file_size, mtime_ns, reference_signature, status,
                reference_hit_count, caller_count, diagnostic_count, time.time(),
            ),
        )

    def scanned_paths(self, reference_signature: str) -> list[str]:
        return [
            str(row[0])
            for row in self.connection.execute(
                "SELECT file_path FROM scan_files WHERE reference_signature = ?",
                (reference_signature,),
            )
        ]

    def referencing_file_count(self, reference_signature: str) -> int:
        return int(
            self.connection.execute(
                """
                SELECT COUNT(*) FROM scan_files
                WHERE reference_signature = ? AND reference_hit_count > 0
                  AND status = 'complete'
                """,
                (reference_signature,),
            ).fetchone()[0]
        )

    def delete_diagnostics_by_codes(self, codes: tuple[str, ...]) -> None:
        if not codes:
            return
        placeholders = ",".join("?" for _ in codes)
        self.connection.execute(
            f"DELETE FROM diagnostics WHERE code IN ({placeholders})", codes
        )

    def put_metadata(self, values: dict[str, str]) -> None:
        self.connection.executemany(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", values.items()
        )

    def compact_if_wasteful(self) -> bool:
        """Reclaim pages after a changed scan invalidates many old rows."""

        self.connection.commit()
        page_count = int(self.connection.execute("PRAGMA page_count").fetchone()[0])
        free_count = int(self.connection.execute("PRAGMA freelist_count").fetchone()[0])
        self.connection.execute("PRAGMA optimize")
        if page_count >= 256 and free_count * 4 >= page_count:
            self.connection.execute("VACUUM")
            return True
        return False

    def validate(self) -> None:
        """Refuse to publish a corrupt database or broken narrow relation."""

        result = str(self.connection.execute("PRAGMA quick_check").fetchone()[0])
        if result != "ok":
            raise sqlite3.DatabaseError(f"SQLite quick_check failed: {result}")
        broken = self.connection.execute("PRAGMA foreign_key_check").fetchone()
        if broken is not None:
            raise sqlite3.DatabaseError(
                "SQLite foreign_key_check failed: " + ", ".join(map(str, broken))
            )

    def add_operation(self, row: dict[str, Any]) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO odata_operations(
                integrator_file, tag_offset, rule_eid, rule_name, rule_class,
                product_name, product_eid, phase_name,
                classification, rule_type, disabled, source_name, base_url,
                action, api_path, filter_expr, request_group, target_group,
                results_group, output_group, attributes_json
            ) VALUES (
                :integrator_file, :tag_offset, :rule_eid, :rule_name, :rule_class,
                :product_name, :product_eid, :phase_name,
                :classification, :rule_type, :disabled, :source_name, :base_url,
                :action, :api_path, :filter_expr, :request_group, :target_group,
                :results_group, :output_group, :attributes_json
            )
            """,
            {**row, "attributes_json": _json(row.get("attributes", {}))},
        )
        return int(cursor.lastrowid)

    def add_reference(self, row: dict[str, Any], mappings: Iterable[dict[str, Any]]) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO caller_references(
                caller_file, tag_offset, rule_eid, rule_name, rule_class,
                classification, rule_type, disabled, selector, source_name,
                component_list, attributes_json
            ) VALUES (
                :caller_file, :tag_offset, :rule_eid, :rule_name, :rule_class,
                :classification, :rule_type, :disabled, :selector, :source_name,
                :component_list, :attributes_json
            )
            """,
            {**row, "attributes_json": _json(row.get("attributes", {}))},
        )
        reference_id = cursor.lastrowid
        for mapping in mappings:
            self.connection.execute(
                """
                INSERT INTO caller_mappings(
                    reference_id, mapping_prefix, solution_data_item, property_key,
                    direction, class_type, attributes_json
                ) VALUES (
                    :reference_id, :mapping_prefix, :solution_data_item, :property_key,
                    :direction, :class_type, :attributes_json
                )
                """,
                {
                    **mapping,
                    "reference_id": reference_id,
                    "attributes_json": _json(mapping.get("attributes", {})),
                },
            )
        return int(reference_id)

    def add_integrator_mapping(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO integrator_mappings(
                integrator_file, product_name, product_eid, tag_offset,
                mapping_prefix, exported_property, property_key, direction,
                class_type, attributes_json
            ) VALUES (
                :integrator_file, :product_name, :product_eid, :tag_offset,
                :mapping_prefix, :exported_property, :property_key, :direction,
                :class_type, :attributes_json
            )
            """,
            {**row, "attributes_json": _json(row.get("attributes", {}))},
        )

    def add_operation_link(
        self,
        reference_id: int,
        operation_id: int,
        resolution_status: str,
        evidence: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO caller_operation_links(
                reference_id, operation_id, resolution_status, evidence_json
            ) VALUES (?, ?, ?, ?)
            """,
            (reference_id, operation_id, resolution_status, _json(evidence)),
        )

    def add_data_source(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO data_sources(
                integrator_file, tag_offset, source_name, class_type, base_url,
                attributes_json
            ) VALUES (
                :integrator_file, :tag_offset, :source_name, :class_type, :base_url,
                :attributes_json
            )
            """,
            {**row, "attributes_json": _json(row.get("attributes", {}))},
        )

    def put_rule_class_census(self, integrator_file: str, counts: dict[str, int]) -> None:
        self.connection.executemany(
            """
            INSERT INTO rule_class_census(integrator_file, rule_class, occurrence_count)
            VALUES (?, ?, ?)
            """,
            ((integrator_file, rule_class, count) for rule_class, count in counts.items()),
        )

    def add_diagnostic(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO diagnostics(
                severity, code, file_path, tag_offset, rule_class, message, evidence_json
            ) VALUES (
                :severity, :code, :file_path, :tag_offset, :rule_class, :message,
                :evidence_json
            )
            """,
            {**row, "evidence_json": _json(row.get("evidence", {}))},
        )

    def diagnostic_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM diagnostics").fetchone()[0])

    def operation_link_count(self) -> int:
        return int(
            self.connection.execute("SELECT COUNT(*) FROM caller_operation_links").fetchone()[0]
        )

    def commit(self) -> None:
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, parameters))

    def counts(self) -> dict[str, int]:
        return {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("odata_operations", "caller_references", "caller_mappings")
        }


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
