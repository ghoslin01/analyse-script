"""Command-line entry point for the bounded-memory IFP contract slicer."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from .extractor import build_contracts
from .store import ContractStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ifp-contract",
        description="Extract OData/Data Integrator caller contracts without building an IFP graph.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="stream-scan a corpus into a small contract SQLite database")
    build.add_argument("root", type=Path, help="IFP corpus folder (or one IFP file)")
    build.add_argument("--integrator", type=Path, required=True, help="the Data Integrator IFP")
    build.add_argument(
        "--reference",
        help="exact caller text; defaults to the Data Integrator IFP filename",
    )
    build.add_argument("--db", type=Path, required=True, help="output SQLite database")
    build.add_argument("--quiet", action="store_true", help="disable periodic scan progress")
    build.add_argument(
        "--strict",
        action="store_true",
        help="return exit code 2 when unknown or unclassified evidence is found",
    )

    report = commands.add_parser("report", help="render the extracted contracts")
    report.add_argument("--db", type=Path, required=True, help="database produced by build")
    report.add_argument("--format", choices=("markdown", "json"), default="markdown")
    report.add_argument("--output", type=Path, help="write output to this file instead of stdout")
    return parser


def _markdown(store: ContractStore) -> str:
    metadata = {row["key"]: row["value"] for row in store.rows("SELECT key, value FROM metadata ORDER BY key")}
    operations = store.rows(
        """
        SELECT rule_eid, rule_name, rule_class, classification, rule_type, disabled,
               source_name, base_url, action, api_path, filter_expr, request_group,
               target_group, results_group, output_group
        FROM odata_operations ORDER BY tag_offset
        """
    )
    references = store.rows(
        """
        SELECT id, caller_file, tag_offset, rule_eid, rule_name, rule_class,
               classification, rule_type, disabled, selector, source_name, component_list
        FROM caller_references ORDER BY caller_file, tag_offset
        """
    )
    mappings = store.rows(
        """
        SELECT reference_id, mapping_prefix, solution_data_item, property_key,
               direction, class_type
        FROM caller_mappings ORDER BY reference_id, mapping_prefix
        """
    )
    mappings_by_reference: dict[int, list[Any]] = {}
    for mapping in mappings:
        mappings_by_reference.setdefault(mapping["reference_id"], []).append(mapping)

    data_sources = store.rows(
        """
        SELECT source_name, class_type, base_url, tag_offset
        FROM data_sources ORDER BY source_name, tag_offset
        """
    )
    diagnostics = store.rows(
        """
        SELECT severity, code, file_path, tag_offset, rule_class, message, evidence_json
        FROM diagnostics ORDER BY
          CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END,
          file_path, tag_offset
        """
    )

    lines = [
        "# Data Integrator OData contract",
        "",
        f"- Scan status: `{_inline(metadata.get('scan_status'))}`",
        f"- Data Integrator: `{_inline(metadata.get('integrator_file'))}`",
        f"- Direct-reference text: `{_inline(metadata.get('reference'))}`",
        f"- IFP files byte-scanned: {metadata.get('files_examined', '0')}",
        f"- Files containing the reference: {metadata.get('referencing_files', '0')}",
        "",
        "## API / OData requests",
        "",
    ]
    if not operations:
        lines.append("No OData/API Rule was recognized. Check the RuleClassName or source attributes in the integrator IFP.")
    for operation in operations:
        lines.extend(
            [
                f"### {_inline(operation['rule_name']) or _inline(operation['rule_eid']) or 'Unnamed operation'}",
                "",
                f"- Rule: `{_inline(operation['rule_class'])}`",
                f"- Classification: `{_inline(operation['classification'])}`",
                f"- Disabled: `{'yes' if operation['disabled'] else 'no'}`",
                f"- OData/API source: `{_inline(operation['source_name'])}`",
                f"- Base endpoint: `{_inline(operation['base_url'])}`",
                f"- Method/action: `{_inline(operation['action'])}`",
                f"- API path: `{_inline(operation['api_path'])}`",
                f"- Filter/query: `{_inline(operation['filter_expr'])}`",
                f"- Request input: `{_inline(operation['request_group'])}`",
                f"- Target group: `{_inline(operation['target_group'])}`",
                f"- Result group: `{_inline(operation['results_group'])}`",
                f"- Output group: `{_inline(operation['output_group'])}`",
                "",
            ]
        )

    lines.extend(["## API / OData data sources", ""])
    if not data_sources:
        lines.append("No API/OData DataSource declaration was recognized.")
        lines.append("")
    else:
        lines.extend(
            [
                "| Source | Type | Base endpoint |",
                "| --- | --- | --- |",
            ]
        )
        for source in data_sources:
            lines.append(
                "| `{}` | `{}` | `{}` |".format(
                    _cell(source["source_name"]),
                    _cell(source["class_type"]),
                    _cell(source["base_url"]),
                )
            )
        lines.append("")

    lines.extend(["## Direct Data Integrator callers", ""])
    if not references:
        lines.append("No direct component-call Rule was found.")
    for reference in references:
        lines.extend(
            [
                f"### {_inline(reference['rule_name']) or _inline(reference['rule_eid']) or 'Unnamed caller'}",
                "",
                f"- File: `{_inline(reference['caller_file'])}`",
                f"- Rule: `{_inline(reference['rule_class'])}`",
                f"- Classification: `{_inline(reference['classification'])}`",
                f"- Disabled: `{'yes' if reference['disabled'] else 'no'}`",
                f"- Data Integrator selector: `{_inline(reference['selector'])}`",
                f"- Source: `{_inline(reference['source_name'])}`",
                f"- Component list: `{_inline(reference['component_list'])}`",
                "",
            ]
        )
        reference_mappings = mappings_by_reference.get(reference["id"], [])
        if reference_mappings:
            lines.extend(
                [
                    "| Direction | Caller data item | Integrator property | Mapping |",
                    "| --- | --- | --- | --- |",
                ]
            )
            for mapping in reference_mappings:
                lines.append(
                    "| {direction} | `{solution}` | `{property}` | `{prefix}` |".format(
                        direction=_cell(mapping["direction"]),
                        solution=_inline(mapping["solution_data_item"]),
                        property=_inline(mapping["property_key"]),
                        prefix=_inline(mapping["mapping_prefix"]),
                    )
                )
            lines.append("")
        else:
            lines.extend(["No flattened ComponentMapping attributes were present on this Rule.", ""])

    lines.extend(["## Unknown / unresolved evidence", ""])
    if not diagnostics:
        lines.append("No unknown or unresolved contract evidence was found.")
    else:
        lines.extend(
            [
                "| Severity | Code | Rule class | File offset | Description |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in diagnostics:
            location = f"{item['file_path'] or '—'}@{item['tag_offset'] if item['tag_offset'] is not None else '—'}"
            lines.append(
                "| {} | `{}` | `{}` | `{}` | {} |".format(
                    _cell(item["severity"]),
                    _cell(item["code"]),
                    _cell(item["rule_class"]),
                    _cell(location),
                    _cell(item["message"]),
                )
            )
    return "\n".join(lines).rstrip() + "\n"


def _report_json(store: ContractStore) -> str:
    return json.dumps(
        {
            "metadata": {row["key"]: row["value"] for row in store.rows("SELECT key, value FROM metadata")},
            "odata_operations": [dict(row) for row in store.rows("SELECT * FROM odata_operations ORDER BY tag_offset")],
            "data_sources": [dict(row) for row in store.rows("SELECT * FROM data_sources ORDER BY tag_offset")],
            "caller_references": [
                dict(row) for row in store.rows("SELECT * FROM caller_references ORDER BY caller_file, tag_offset")
            ],
            "caller_mappings": [dict(row) for row in store.rows("SELECT * FROM caller_mappings ORDER BY reference_id, mapping_prefix")],
            "diagnostics": [dict(row) for row in store.rows("SELECT * FROM diagnostics ORDER BY id")],
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def _inline(value: object) -> str:
    if value is None or value == "":
        return "—"
    return str(value).replace("`", "\\`").replace("\n", " ")


def _cell(value: object) -> str:
    return _inline(value).replace("|", "\\|")


class _ProgressPrinter:
    """Time-throttled progress suitable for multi-hundred-megabyte files."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.last_print = self.started
        self.printed = False
        self.completed_bytes = 0
        self.previous_path: Path | None = None
        self.previous_size = 0

    def __call__(self, path: Path, scanned: int, size: int, file_index: int) -> None:
        if self.previous_path is not None and path != self.previous_path:
            self.completed_bytes += self.previous_size
        self.previous_path = path
        self.previous_size = size
        now = time.monotonic()
        if now - self.last_print < 2.0:
            return
        elapsed = max(now - self.started, 0.001)
        total_scanned = self.completed_bytes + scanned
        rate = total_scanned / elapsed / (1024 * 1024)
        print(
            f"[scan] file={file_index} {path.name}: "
            f"{scanned / (1024 * 1024):.1f}/{size / (1024 * 1024):.1f} MiB, "
            f"average={rate:.1f} MiB/s",
            file=sys.stderr,
        )
        self.last_print = now
        self.printed = True

    def finish(self) -> None:
        if self.printed:
            print("[scan] corpus byte scan complete", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        reference = args.reference or args.integrator.name
        progress = None if args.quiet else _ProgressPrinter()
        store: ContractStore | None = None
        try:
            store = ContractStore(args.db)
            summary = build_contracts(
                args.root, args.integrator, reference, store, progress=progress
            )
            counts = store.counts()
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        finally:
            if store is not None:
                store.close()
            if progress is not None:
                progress.finish()
        print(
            "Built contract DB: "
            f"files={summary.files_examined}, matches={summary.referencing_files}, "
            f"sources={summary.data_sources}, "
            f"odata={counts['odata_operations']}, callers={counts['caller_references']}, "
            f"mappings={counts['caller_mappings']}, diagnostics={summary.diagnostics}, db={args.db}"
        )
        for warning in summary.warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
        return 2 if args.strict and summary.diagnostics else 0

    if not args.db.is_file():
        print(f"ERROR: contract database does not exist: {args.db}", file=sys.stderr)
        return 1
    store = ContractStore(args.db)
    try:
        content = _markdown(store) if args.format == "markdown" else _report_json(store)
    finally:
        store.close()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
        print(f"Wrote {args.format} report: {args.output}")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
