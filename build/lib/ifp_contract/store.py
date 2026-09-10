"""Small SQLite store for extracted data contracts, not for an IFP graph."""

from __future__ import annotations

import json
import sqlite3
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
    source_name TEXT,
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

CREATE INDEX IF NOT EXISTS idx_caller_references_file ON caller_references(caller_file);
CREATE INDEX IF NOT EXISTS idx_caller_mappings_reference ON caller_mappings(reference_id);
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

    def reset(self) -> None:
        self.connection.execute("DELETE FROM caller_mappings")
        self.connection.execute("DELETE FROM caller_references")
        self.connection.execute("DELETE FROM odata_operations")
        self.connection.execute("DELETE FROM metadata")

    def put_metadata(self, values: dict[str, str]) -> None:
        self.connection.executemany(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", values.items()
        )

    def add_operation(self, row: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO odata_operations(
                integrator_file, tag_offset, rule_eid, rule_name, rule_class,
                source_name, action, api_path, filter_expr, request_group, target_group,
                results_group, output_group, attributes_json
            ) VALUES (
                :integrator_file, :tag_offset, :rule_eid, :rule_name, :rule_class,
                :source_name, :action, :api_path, :filter_expr, :request_group, :target_group,
                :results_group, :output_group, :attributes_json
            )
            """,
            {**row, "attributes_json": _json(row.get("attributes", {}))},
        )

    def add_reference(self, row: dict[str, Any], mappings: Iterable[dict[str, Any]]) -> None:
        cursor = self.connection.execute(
            """
            INSERT INTO caller_references(
                caller_file, tag_offset, rule_eid, rule_name, rule_class,
                selector, source_name, component_list, attributes_json
            ) VALUES (
                :caller_file, :tag_offset, :rule_eid, :rule_name, :rule_class,
                :selector, :source_name, :component_list, :attributes_json
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
