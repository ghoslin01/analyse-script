# IFP Contract Slicer

`ifp-contract` answers one narrow question without building a whole-project
IFP graph:

```text
OData operation → Data Integrator output → caller mapping
```

It never builds a DOM and never writes every XML node or attribute to SQLite.
The corpus pass is a bounded-memory byte search for a Data Integrator reference.
Only matching `Rule` tags are parsed for CallComponent and mapping attributes.

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

During large scans the command prints the current file, MiB read, and average
MiB/s every few seconds. Use `--quiet` to disable progress. Use `--strict` when
CI should return exit code 2 for unknown/unclassified evidence; the evidence is
still saved to SQLite and included in the report.

## Storage contract

SQLite stores only:

- OData/IRIS rule declarations from the selected Data Integrator file;
- API/OData DataSource declarations and base endpoints;
- caller Rule tags containing the reference string;
- flattened CallComponent mapping attributes from those matching tags.
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

The opt-in large-file test uses a generated caller IFP and validates the full
SQLite build path with bounded memory:

```powershell
$env:IFP_LARGE_TEST_MB=256
python -m pytest -q -s tests/test_large_streaming.py
```
