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
