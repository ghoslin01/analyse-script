# IFP Contract Slicer

`ifp-contract` answers one narrow question without building a whole-project
IFP graph:

```text
API/OData request path → Data Integrator Product/output → direct caller mapping
```

It never builds a DOM and never writes every XML node or attribute to SQLite.
The Data Integrator is streamed once for API rules, Products, DataSources and
published mappings. Every other IFP is a bounded-memory byte search for the
Data Integrator reference; only matching `Rule` start tags are inspected.

## Usage

### Install (Windows)

From the project folder, create an isolated Python environment and install the
single local package. It has no third-party runtime dependencies.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install .
```

If PowerShell blocks activation, run the final command as
`.\.venv\Scripts\python.exe -m pip install .`, then use
`.\.venv\Scripts\ifp-contract.exe` in the commands below.

### Build the small SQLite contract database

```powershell
ifp-contract build D:\ifp-corpus `
  --integrator D:\ifp-corpus\Integration\WraDataIntegrator.ifp `
  --db D:\tmp\data-contracts.db

ifp-contract report --db D:\tmp\data-contracts.db --format markdown
```

### Write the detailed Markdown report

```powershell
ifp-contract report --db D:\tmp\data-contracts.db --format markdown `
  --output D:\tmp\data-contracts-detail.md
```

`build` makes the only corpus pass. `report` reads the small SQLite database;
it does not scan the IFP corpus again.

`--reference` is optional. By default it uses the Data Integrator filename
(`WraDataIntegrator.ifp`). Supply it only when callers contain a different or
more specific path fragment, for example
`--reference Integration/WraDataIntegrator.ifp`.

Repeat `--reference` when exports use more than one spelling. Slash and
backslash forms are searched automatically:

```powershell
ifp-contract build D:\ifp-corpus `
  --integrator D:\ifp-corpus\Integration\WraDataIntegrator.ifp `
  --reference WraDataIntegrator.ifp `
  --reference Integration/WraDataIntegrator `
  --ignore-case `
  --db D:\tmp\data-contracts.db
```

During large scans the command includes the Data Integrator in total byte
progress and prints percentage, file number, stage, MiB read, average MiB/s and
ETA every few seconds. Use `--quiet` to disable progress.

Successful results are atomically published to the requested `.db`. The scan
commits one IFP at a time to `<db>.partial`; rerunning the same command reuses
unchanged completed files. `--fresh` forces a complete rescan. If a file is
unreadable, malformed or has an unsupported encoding, the command returns 1,
keeps the partial checkpoint and leaves an existing successful database
untouched. After repairing the file, rerun the same command to retry only that
file.

Exit codes are: `0` complete, `1` incomplete/error, `2` complete but
`--strict` found unknown or unresolved evidence, and `130` interrupted.

Dynamic selectors such as `SelectComponent="$$RuntimeComponent$"` cannot be
proven to target the selected integrator. They are excluded by default to keep
the database focused and small. Add `--include-dynamic-references` when you
want a diagnostic census of them; these rows go to `diagnostics`, not to the
confirmed caller list.

## Storage contract

SQLite stores only:

- OData/IRIS rule declarations from the selected Data Integrator file;
- API/OData DataSource declarations and base endpoints;
- caller Rule tags containing the reference string;
- flattened CallComponent mapping attributes from those matching tags;
- Product/Phase ownership and narrow caller-to-API-operation links;
- unknown Rule/reference evidence and its exact file byte offset.

The original IFP files remain the source of truth.  A 4 GB corpus is scanned
as bytes to find the reference, but is never materialised or semantically
indexed as a global XML graph. For an exact reference hit, the extractor seeks
back to the containing XML start tag and reads just that tag's selected
attributes—there is no second full parse of a candidate IFP.

Unknown/custom `RuleClassName` values do not abort extraction. Rules with a
direct component selector are retained as confirmed reference evidence;
API-shaped custom Rules are retained as structural candidates. Ambiguous,
unclassified, malformed, and unsupported-encoding evidence is written to the
`diagnostics` table and the report's `Unknown / unresolved evidence` section.
References in XML comments, CDATA, declarations and processing instructions are
ignored. UTF-8, ASCII-compatible declared encodings, UTF-16 LE and UTF-16 BE are
handled while preserving original byte offsets. Selected attribute values are
bounded to 1 MiB with an explicit truncation diagnostic; malformed tags have a
bounded 512 MiB search window instead of an unbounded allocation.

## Custom Rule classes and attribute names

Unknown evidence is always retained. Once you know a vendor-specific Rule or
attribute, classify it without changing code:

```powershell
ifp-contract build D:\ifp-corpus `
  --integrator D:\ifp-corpus\DataIntegrator.ifp `
  --rules-config .\examples\rules-config.json `
  --db D:\tmp\data-contracts.db
```

The JSON file extends, rather than replaces, the built-in vocabulary. Supported
alias concepts are `source`, `method`, `path`, `base_url`, `filter`, `request`,
`target`, `result`, `output`, and `selector`.

The opt-in large-file test uses a generated caller IFP and validates the full
SQLite build path with bounded memory:

```powershell
$env:IFP_LARGE_TEST_MB=256
python -m pytest -q -s tests/test_large_streaming.py
```

The generated test file is never loaded as one Python string; peak scanner
memory remains independent of IFP size.

## Development guide

### Local setup

The application has no third-party runtime dependency. Tests use `pytest`.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e . pytest
.venv/bin/python -m pytest -q
```

On Windows, replace `.venv/bin/python` with
`.venv\Scripts\python.exe`.

### Architecture and data flow

```text
CLI / atomic checkpoint
        │
        ├── stream selected Data Integrator ──> API rules, Products, mappings
        │
        └── byte-search caller IFP files ─────> matching Rule start tags only
                                                    │
                                                    v
                                             narrow SQLite rows
                                                    │
                                                    v
                                             Markdown / JSON report
```

| File | Responsibility |
| --- | --- |
| `src/ifp_contract/cli.py` | CLI arguments, progress/ETA, partial database handling, atomic publication, reports and exit codes. |
| `src/ifp_contract/extractor.py` | Scan orchestration, API/caller classification, mappings, Product ownership, operation resolution and cache signatures. |
| `src/ifp_contract/xmlbytes.py` | Encoding-aware bounded-memory byte search, XML visibility states, tag boundaries and selected-attribute reads. |
| `src/ifp_contract/config.py` | Built-in Rule classes, attribute aliases and JSON extension loading. |
| `src/ifp_contract/store.py` | SQLite schema, migrations, per-file checkpoints, compaction and integrity validation. |
| `src/ifp_contract/stream.py` | Legacy low-level compatibility helpers. Production extraction uses `xmlbytes.py`; do not add new extraction behavior here. |
| `tests/test_contract_build.py` | End-to-end extraction, reporting, resume, failure recovery, schema migration and classification tests. |
| `tests/test_xmlbytes.py` | Chunk-boundary, encoding, malformed XML and attribute-bound tests. |
| `tests/test_large_streaming.py` | Opt-in hundreds-of-megabytes full-build memory/performance test. |

The normal build sequence is:

1. Enumerate only `.ifp` paths and collect file size/mtime metadata.
2. Stream the selected Data Integrator once for `DataSource`, `Product`,
   `Phase`, and `Rule` start tags.
3. Search every other IFP for the configured reference bytes. Seek back and
   parse selected attributes only when a visible reference is found.
4. Commit each completed caller IFP to the partial SQLite database.
5. Resolve caller Products to API operations, optionally compact unused pages,
   validate SQLite, then atomically replace the requested database.

### Invariants to preserve

These are deliberate product constraints, not incidental implementation
details:

- Never call `read_text()` or `read_bytes()` on an IFP in production code and
  never build an XML DOM/tree for a complete file.
- Memory use must depend on chunk/tag limits, not corpus or file size.
- Do not create a whole-project node/edge graph or persist unrelated screen
  properties. SQLite contains only the contract slice.
- Keep `tag_offset` in original file bytes. Encoding conversion must not alter
  evidence offsets.
- Ignore matches inside comments, CDATA, declarations, and processing
  instructions.
- Preserve unknown direct-reference evidence instead of silently discarding
  it. Aggregate non-reference Rule classes only inside the selected integrator.
- Keep dynamic references opt-in because they cannot prove an integrator
  target and can create substantial noise.
- A failed or interrupted scan must not replace the last successful database.
  Completed files remain reusable in `<db>.partial`.
- A report reads SQLite only; it must never trigger another corpus scan.

### Extending extraction safely

For a deployment-specific Rule or renamed attribute, prefer a rules JSON file
such as `examples/rules-config.json`. This does not require a code change or
cache-format bump.

When a concept should become a built-in default:

1. Add its Rule suffix or attribute alias in `config.py`.
2. Ensure `is_contract_attribute()` in `extractor.py` captures the evidence.
3. Map it into the operation/caller row in `extractor.py`.
4. Add the field to the Markdown and JSON contract where appropriate.
5. Add a focused fixture and assertion in `test_contract_build.py`.

When changing byte scanning or XML handling, modify `xmlbytes.py` and add a
small-chunk test in `test_xmlbytes.py`. Use tiny chunk sizes in tests so opening
markers, closing markers, references, quotes, and UTF-16 code units cross chunk
boundaries.

When changing persisted fields:

1. Update `SCHEMA` and `_migrate()` in `store.py`.
2. Increment both `PRAGMA user_version` and the `schema_version` metadata value.
3. Test migration from the previous schema without dropping existing rows.

Any code change that alters which caller files or Rules qualify must increment
the `format` value in `build_contracts()`'s `signature_payload`. Otherwise an
existing checkpoint may reuse results produced by the old extraction logic.
During development, `--fresh` is also useful for forcing a clean comparison.

### SQLite contract

| Table | Contents |
| --- | --- |
| `odata_operations` | API/OData rules plus Product, Phase, method, base URL, request path, input and output groups. |
| `data_sources` | API/OData DataSource declarations and base endpoints. |
| `integrator_mappings` | Published Product mappings exposed by the Data Integrator. |
| `caller_references` | Direct caller Rules and their classification/evidence attributes. |
| `caller_mappings` | Flattened input/output mappings owned by a caller reference. |
| `caller_operation_links` | Narrow caller-to-operation resolution with exact/fallback status. |
| `diagnostics` | Unknown, ambiguous, malformed, truncated or unsupported evidence with byte offsets. |
| `rule_class_census` | Aggregated RuleClassName counts from the selected integrator only. |
| `scan_files` | File identity, checkpoint status and per-file result counts used for resume. |
| `metadata` | Scan identity, options, status and summary counts. |

Useful investigation commands:

```bash
# Complete machine-readable evidence, including attributes and diagnostics
ifp-contract report --db contracts.db --format json --output contracts.json

# Human-readable API path and caller mapping chain
ifp-contract report --db contracts.db --format markdown --output detail.md
```

If a build returns 1, inspect the `.partial` database with `report`; its
`scan_status` will be `partial_with_errors`, and the failed paths are recorded
in `scan_files` and `diagnostics`. Repair the source file and rerun the same
build command. Do not rename the partial database: the CLI discovers that exact
name, while its stored scan signature decides which files are safe to reuse.

### Test and release checklist

Before committing an extraction change:

```bash
python -m pytest -q
IFP_LARGE_TEST_MB=256 python -m pytest -q -s tests/test_large_streaming.py
python -m compileall -q src
git diff --check
```

Also verify one clean installation when packaging or CLI code changes:

```bash
python -m venv /tmp/ifp-contract-install-check
/tmp/ifp-contract-install-check/bin/python -m pip install .
/tmp/ifp-contract-install-check/bin/ifp-contract --version
```

For a release, update the version in both `pyproject.toml` and
`src/ifp_contract/__init__.py`. Confirm that a first build scans files, a second
identical build reports them as cached, Markdown includes the effective API
request path, and `git status` is clean after committing.
