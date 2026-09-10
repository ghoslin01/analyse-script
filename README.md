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
  --reference WraDataIntegrator.ifp `
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

`--reference` must be an exact stable string used by callers, normally the
Data Integrator IFP filename or its `SelectComponent` path fragment.

## Storage contract

SQLite stores only:

- OData/IRIS rule declarations from the selected Data Integrator file;
- caller Rule tags containing the reference string;
- flattened CallComponent mapping attributes from those matching tags.

The original IFP files remain the source of truth.  A 4 GB corpus is scanned
as bytes to find the reference, but is never materialised or semantically
indexed as a global XML graph. For an exact reference hit, the extractor seeks
back to the containing XML start tag and reads just that tag's selected
attributes—there is no second full parse of a candidate IFP.
