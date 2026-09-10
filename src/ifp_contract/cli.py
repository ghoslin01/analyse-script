"""Command-line entry point for the bounded-memory IFP contract slicer."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_rule_config
from .extractor import ProgressUpdate, build_contracts
from .store import ContractStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ifp-contract",
        description="Extract OData/Data Integrator caller contracts without building an IFP graph.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="stream-scan a corpus into a small contract SQLite database")
    build.add_argument("root", type=Path, help="IFP corpus folder (or one IFP file)")
    build.add_argument("--integrator", type=Path, required=True, help="the Data Integrator IFP")
    build.add_argument(
        "--reference",
        action="append",
        help="exact caller text; defaults to the Data Integrator IFP filename",
    )
    build.add_argument(
        "--ignore-case",
        action="store_true",
        help="match ASCII component references without case sensitivity",
    )
    build.add_argument(
        "--include-dynamic-references",
        action="store_true",
        help="also census unresolved dynamic selector expressions (may be noisy)",
    )
    build.add_argument("--db", type=Path, required=True, help="output SQLite database")
    build.add_argument("--quiet", action="store_true", help="disable periodic scan progress")
    build.add_argument(
        "--fresh",
        action="store_true",
        help="discard the resumable partial scan and rescan every IFP file",
    )
    build.add_argument(
        "--strict",
        action="store_true",
        help="return exit code 2 when unknown or unclassified evidence is found",
    )
    build.add_argument(
        "--rules-config",
        type=Path,
        help="JSON file extending API/component Rule classes and attribute aliases",
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
        SELECT id, rule_eid, rule_name, rule_class, product_name, product_eid,
               phase_name, classification, rule_type, disabled,
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
    links = store.rows(
        """
        SELECT l.reference_id, l.resolution_status, o.product_name, o.rule_name,
               o.action, o.base_url, o.api_path, o.output_group, o.results_group
        FROM caller_operation_links l
        JOIN odata_operations o ON o.id = l.operation_id
        ORDER BY l.reference_id, o.tag_offset
        """
    )
    links_by_reference: dict[int, list[Any]] = {}
    for link in links:
        links_by_reference.setdefault(link["reference_id"], []).append(link)

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
    integrator_mappings = store.rows(
        """
        SELECT product_name, mapping_prefix, exported_property, property_key,
               direction, class_type
        FROM integrator_mappings ORDER BY product_name, mapping_prefix
        """
    )
    rule_classes = store.rows(
        """
        SELECT rule_class, occurrence_count
        FROM rule_class_census ORDER BY occurrence_count DESC, rule_class
        """
    )

    lines = [
        "# Data Integrator OData contract",
        "",
        f"- Scan status: `{_inline(metadata.get('scan_status'))}`",
        f"- Data Integrator: `{_inline(metadata.get('integrator_file'))}`",
        f"- Direct-reference text: `{_inline(metadata.get('reference'))}`",
        f"- Caller IFP files examined: {metadata.get('files_examined', '0')}",
        f"- Files read this run: {metadata.get('files_scanned', metadata.get('files_examined', '0'))}",
        f"- Files reused from checkpoint: {metadata.get('files_skipped', '0')}",
        f"- Files that failed: {metadata.get('files_failed', '0')}",
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
                f"- Product/operation: `{_inline(operation['product_name'])}`",
                f"- Phase: `{_inline(operation['phase_name'])}`",
                f"- Classification: `{_inline(operation['classification'])}`",
                f"- Disabled: `{'yes' if operation['disabled'] else 'no'}`",
                f"- OData/API source: `{_inline(operation['source_name'])}`",
                f"- Base endpoint: `{_inline(operation['base_url'])}`",
                f"- Method/action: `{_inline(operation['action'])}`",
                f"- API path: `{_inline(operation['api_path'])}`",
                f"- Effective API request: `"
                f"{_inline(operation['action'])} "
                f"{_inline(_joined_api_path(operation['base_url'], operation['api_path']))}`",
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

    lines.extend(["## Data Integrator published mappings", ""])
    if not integrator_mappings:
        lines.extend(["No flattened Product export mapping was found.", ""])
    else:
        lines.extend(
            [
                "| Product | Direction | Internal/exported item | Public property | Mapping |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for mapping in integrator_mappings:
            lines.append(
                "| `{}` | {} | `{}` | `{}` | `{}` |".format(
                    _cell(mapping["product_name"]),
                    _cell(mapping["direction"]),
                    _cell(mapping["exported_property"]),
                    _cell(mapping["property_key"]),
                    _cell(mapping["mapping_prefix"]),
                )
            )
        lines.append("")

    lines.extend(["## Direct Data Integrator callers", ""])
    if not references:
        lines.extend(["No direct component-call Rule was found.", ""])
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
        resolved_operations = links_by_reference.get(reference["id"], [])
        if resolved_operations:
            lines.extend(
                [
                    "Resolved API/OData operations:",
                    "",
                    "| Resolution | Product | Operation | Method | API path | Output |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            for operation in resolved_operations:
                full_path = _joined_api_path(operation["base_url"], operation["api_path"])
                output = operation["output_group"] or operation["results_group"]
                lines.append(
                    "| `{}` | `{}` | `{}` | `{}` | `{}` | `{}` |".format(
                        _cell(operation["resolution_status"]),
                        _cell(operation["product_name"]),
                        _cell(operation["rule_name"]),
                        _cell(operation["action"]),
                        _cell(full_path),
                        _cell(output),
                    )
                )
            lines.append("")
        else:
            lines.extend(["Resolved API/OData operations: **none**", ""])
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
                "| Severity | Code | Rule class | File offset | Description | Evidence |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in diagnostics:
            location = f"{item['file_path'] or '—'}@{item['tag_offset'] if item['tag_offset'] is not None else '—'}"
            lines.append(
                "| {} | `{}` | `{}` | `{}` | {} | `{}` |".format(
                    _cell(item["severity"]),
                    _cell(item["code"]),
                    _cell(item["rule_class"]),
                    _cell(location),
                    _cell(item["message"]),
                    _cell(_compact_json(item["evidence_json"])),
                )
            )
    lines.extend(["", "## RuleClassName census (Data Integrator only)", ""])
    if not rule_classes:
        lines.append("No RuleClassName values were found.")
    else:
        lines.extend(["| Rule class | Count |", "| --- | ---: |"])
        for item in rule_classes:
            lines.append(f"| `{_cell(item['rule_class'])}` | {item['occurrence_count']} |")
    return "\n".join(lines).rstrip() + "\n"


def _report_json(store: ContractStore) -> str:
    def decoded_rows(sql: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in store.rows(sql):
            item = dict(row)
            for key in tuple(item):
                if key.endswith("_json"):
                    raw = item.pop(key)
                    try:
                        item[key.removesuffix("_json")] = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        item[key.removesuffix("_json")] = raw
            rows.append(item)
        return rows

    return json.dumps(
        {
            "metadata": {row["key"]: row["value"] for row in store.rows("SELECT key, value FROM metadata")},
            "odata_operations": decoded_rows("SELECT * FROM odata_operations ORDER BY tag_offset"),
            "data_sources": decoded_rows("SELECT * FROM data_sources ORDER BY tag_offset"),
            "integrator_mappings": decoded_rows("SELECT * FROM integrator_mappings ORDER BY product_name, mapping_prefix"),
            "caller_references": decoded_rows("SELECT * FROM caller_references ORDER BY caller_file, tag_offset"),
            "caller_mappings": decoded_rows("SELECT * FROM caller_mappings ORDER BY reference_id, mapping_prefix"),
            "caller_operation_links": decoded_rows("SELECT * FROM caller_operation_links ORDER BY reference_id, operation_id"),
            "diagnostics": decoded_rows("SELECT * FROM diagnostics ORDER BY id"),
            "rule_class_census": decoded_rows("SELECT * FROM rule_class_census ORDER BY occurrence_count DESC"),
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


def _joined_api_path(base_url: object, api_path: object) -> str:
    base = "" if base_url is None else str(base_url)
    path = "" if api_path is None else str(api_path)
    if not base:
        return path or "—"
    if not path:
        return base
    if path.startswith(("http://", "https://", "$$")):
        return path
    return base.rstrip("/") + "/" + path.lstrip("/")


def _compact_json(value: object, limit: int = 320) -> str:
    try:
        rendered = json.dumps(json.loads(str(value)), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, json.JSONDecodeError):
        rendered = str(value or "")
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + "…"


class _ProgressPrinter:
    """Time-throttled progress suitable for multi-hundred-megabyte files."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.last_print = self.started
        self.printed = False
        self.last_update: ProgressUpdate | None = None

    def __call__(self, update: ProgressUpdate) -> None:
        self.last_update = update
        now = time.monotonic()
        if now - self.last_print < 2.0:
            return
        elapsed = max(now - self.started, 0.001)
        total_scanned = update.completed_bytes + min(
            update.scanned_bytes, update.file_size
        )
        rate_bytes = total_scanned / elapsed
        rate = rate_bytes / (1024 * 1024)
        percent = (
            100.0 * total_scanned / update.total_bytes if update.total_bytes else 100.0
        )
        remaining = max(0, update.total_bytes - total_scanned)
        eta = remaining / rate_bytes if rate_bytes > 0 else 0
        print(
            f"[scan] {percent:5.1f}% file={update.file_index}/{update.file_count} "
            f"stage={update.stage} {update.path.name}: "
            f"{update.scanned_bytes / (1024 * 1024):.1f}/"
            f"{update.file_size / (1024 * 1024):.1f} MiB, "
            f"average={rate:.1f} MiB/s, eta={_duration(eta)}",
            file=sys.stderr,
        )
        self.last_print = now
        self.printed = True

    def finish(self, completed: bool) -> None:
        elapsed = time.monotonic() - self.started
        if completed:
            print(f"[scan] corpus byte scan complete in {_duration(elapsed)}", file=sys.stderr)
        elif self.printed:
            print(f"[scan] stopped after {_duration(elapsed)}", file=sys.stderr)


def _duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes:d}m{seconds:02d}s"
    return f"{seconds:d}s"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        references = args.reference or [args.integrator.name]
        progress = None if args.quiet else _ProgressPrinter()
        store: ContractStore | None = None
        build_succeeded = False
        partial_db = Path(str(args.db) + ".partial")
        try:
            rule_config = load_rule_config(args.rules_config)
            if args.fresh and partial_db.exists():
                partial_db.unlink()
            if not partial_db.exists() and args.db.is_file() and not args.fresh:
                partial_db.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(args.db, partial_db)
            store = ContractStore(partial_db)
            summary = build_contracts(
                args.root,
                args.integrator,
                references,
                store,
                progress=progress,
                ignore_case=args.ignore_case,
                include_dynamic=args.include_dynamic_references,
                resume=not args.fresh,
                rule_config=rule_config,
            )
            store.validate()
            counts = store.counts()
            build_succeeded = summary.files_failed == 0
        except KeyboardInterrupt:
            print(
                f"INTERRUPTED: checkpoint kept at {partial_db}; rerun the same command to resume",
                file=sys.stderr,
            )
            return 130
        except (OSError, ValueError, sqlite3.Error) as error:
            checkpoint = (
                f"; partial database kept at {partial_db}" if partial_db.exists() else ""
            )
            print(f"ERROR: {error}{checkpoint}", file=sys.stderr)
            return 1
        finally:
            if store is not None:
                store.close()
            if progress is not None:
                progress.finish(build_succeeded)
        if summary.files_failed:
            print(
                "ERROR: contract scan is incomplete: "
                f"{summary.files_failed} file(s) failed; checkpoint kept at {partial_db}",
                file=sys.stderr,
            )
            return 1
        try:
            args.db.parent.mkdir(parents=True, exist_ok=True)
            os.replace(partial_db, args.db)
        except OSError as error:
            print(
                f"ERROR: could not atomically publish {args.db}: {error}; partial kept at {partial_db}",
                file=sys.stderr,
            )
            return 1
        print(
            "Built contract DB: "
            f"files={summary.files_examined}, scanned={summary.files_scanned}, "
            f"cached={summary.files_skipped}, failed={summary.files_failed}, "
            f"matches={summary.referencing_files}, "
            f"sources={summary.data_sources}, "
            f"odata={counts['odata_operations']}, callers={counts['caller_references']}, "
            f"mappings={counts['caller_mappings']}, links={summary.operation_links}, "
            f"diagnostics={summary.diagnostics}, db={args.db}"
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
