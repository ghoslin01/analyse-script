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
commits one matching IFP at a time to `<db>.partial`; rerunning the same command
reuses unchanged matching files. Completed files with no target reference are
not persisted in `scan_files`, so the database never becomes a whole-corpus
file index; those cheap byte searches run again on a later build. `--fresh`
forces a complete rescan. If a file is unreadable, malformed or has an
unsupported encoding, the command returns 1, keeps the partial checkpoint and
leaves an existing successful database untouched. After repairing the file,
rerun the same command to retry it.

Exit codes are: `0` complete, `1` incomplete/error, `2` complete but
`--strict` found unknown or unresolved evidence, and `130` interrupted.

Dynamic selectors such as `SelectComponent="$$RuntimeComponent$"` cannot be
proven to target the selected integrator. They are excluded by default to keep
the database focused and small. Add `--include-dynamic-references` when you
want a diagnostic census of them; these rows go to `diagnostics`, not to the
confirmed caller list.

### Run the complete example

`examples/practical-corpus` contains a runnable banking-style Data Integrator,
two normal screen callers, one unknown custom caller Rule, and one unrelated
screen. It demonstrates the complete field-to-mapping-to-Product-to-API chain:

```bash
ifp-contract build examples/practical-corpus \
  --integrator examples/practical-corpus/BankingDataIntegrator.ifp \
  --db practical-contracts.db --fresh

ifp-contract report --db practical-contracts.db \
  --output practical-report.md
```

Expected totals are two API operations, two DataSources, three direct callers,
four caller mappings, three exact Product-to-operation links, and one retained
unknown-Rule diagnostic. `UnrelatedScreen.ifp` is read as bytes but creates no
contract row.

## Trace a field or rule without a database

`trace` reads the IFP files reached from a selected entry and exports a portable
evidence bundle. It follows assignments, conditions, component mappings and
shared Rule references. It does not require `build` or a SQLite database.

For a field in an application screen (replace these synthetic paths with your project):

```powershell
ifp-contract trace D:\ifp-project\components `
  --file Screens/OrderEntry.ifp `
  --field 'Form[1].Amount' `
  --entry Order.Input `
  --path-var LIBRARY_HOME=. `
  --output D:\tmp\amount-trace
```

The output directory contains:

- `summary.md`: a bounded Chinese reading guide to target writes, their
  prerequisites, scenario exclusions, API candidates and remaining questions.
- `report.md`: the readable trace, execution prerequisites, source dependencies
  and unresolved boundaries.
- `evidence.json`: versioned structural evidence, raw behavioral attributes,
  exact mapping attributes, file offsets/lines, file fingerprints and separate
  contexts for different calls and entries.
- `next.json`: an editable request containing the anchors, limits, path-variable
  substitutions, previous fingerprints and pending evidence. If a rules config
  was supplied, a copy is included as `rules-config.json`.

These files can be moved out of an isolated network for analysis. Follow-up
requests can add entries to `targets`, refine an `entry`, or increase limits.
For example, repeat collection with an increased context budget:

```powershell
ifp-contract trace D:\ifp-project\components `
  --request D:\tmp\amount-trace\next.json `
  --max-contexts 10000 `
  --output D:\tmp\amount-trace-next
```

This **recollects the requested scopes**; it does not resume a suspended runtime
or silently discover new callers. Changes to previously collected files are
reported. The `pending` section is evidence for deciding the next targets, not
an executable work queue. Input fields whose writers are outside the selected
scope remain explicit external/unresolved inputs.

Use `--rule-eid EID` instead of `--field` to anchor a rule. `--entry` normally
selects `Product.Phase`; `--entry @EID` inspects an isolated shared rule without
claiming its caller conditions. Omitting `--entry` considers the file's phases
separately and may require a larger budget. Dynamic component path prefixes
require explicit `--path-var NAME=PATH`; `LIBRARY_HOME` has no built-in value.

Default budgets are 50 reached files and 5,000 expanded contexts per target.
`--max-files` and `--max-contexts` change these. Hitting a limit preserves partial
evidence and diagnostics. The collector indexes structural metadata in reached
files (up to 100,000 structural nodes per file); its memory use depends on that
metadata and the context budget. It does not build a full XML DOM or index the
entire corpus. Individual captured attribute values are limited to 1 MiB and
raw behavioral evidence per node to 128 KiB; omissions are diagnosed explicitly.

The trace preserves both the condition that produced a value and the condition
that consumes it. `RuleType=False` is kept as a false branch, `[C]`/`[A]` remain
symbolic, and instance-setting/increment rules stay attached to loop evidence.
Question and button rules retain their UI event contexts; XML order between
separate UI events is **not** treated as runtime order. UI `ConditionExpression`
and applicability attributes are retained as configuration, without assuming
the engine's activation semantics.

This is conservative static analysis, not a UXP interpreter. Multiple writes,
cross-event dependencies and runtime values may remain unresolved. Phase jumps,
unknown rules and recursive calls are explicit boundaries. Unknown rules retain
raw evidence and are not assumed harmless. Current support includes
ContainerRule, EvaluateRule, SetValueRule, ExpressionRule, RepeatRule,
IncrementorRule, ResetDataRule and CallComponentRule, plus configured API rules.
API inputs include both query groups and payload groups. Call-site `In`/`Out`
flags enable mappings; published `PubIn`/`PubOut` alone do not.

Project-specific classes can opt into an existing supported interpretation:

```json
{
  "trace_rule_kinds": {
    "vendor.CustomEvaluationRule": "evaluate",
    "vendor.CustomSetRule": "set"
  }
}
```

Supported kind names are `container`, `evaluate`, `set`, `expression`, `repeat`,
`increment`, `reset`, `call`, and `goto`. This mapping asserts that the class uses
the supported attribute semantics; it does not infer custom implementation
behavior. Existing API class, HTTP method and API attribute-alias settings in
`--rules-config` also apply. Trace does not add new alias concepts to the API
configuration or change existing `build` results.

### Extend the standard trace rules

The standard rule library stays enabled. `trace.rules` adds project semantics;
it does not replace the whole library. See
[`examples/trace-project-config.json`](examples/trace-project-config.json).
Pass this JSON with `--rules-config`; API settings can coexist in the same file.

Each extension declares `class`, `kind`, optional `attributes`, `defaults`,
`branch_attribute`, and `branches`. Attribute mappings run from the supported
canonical name to the project's XML attribute name. For example,
`"PropertyName": "DestinationPath"` and `"FromPropertyName": "SourcePath"`
adapt an assignment; `"defaults": {"FromType": "Data Item"}` makes its source
a field instead of the standard literal-value default. An explicit attribute
mapping replaces that canonical attribute, including when the project attribute
is absent. Defaults only fill absent canonical attributes. Original XML evidence
and the normalized attributes are both retained.

Supported canonical attributes are:

| Area | Attributes |
| --- | --- |
| Assignment | `Type`, `FromType`, `PropertyName`, `VariableName`, `PropertyGroupName`, `PropertyGroupInstanceName`, `FromPropertyName`, `FromVariableName`, `FromPropertyGroupName`, `FromPropertyGroupInstanceName`, `FromValue`, `Trim` |
| Conditions/expressions | `Expression`, `OutputProperty` |
| Reset | `ResetProperty`, `ResetPropertyGroup`, `ResetVariable` |
| Iteration | `IncrementBy`, `EndInstance`, `DataGroupName` |
| Calls/references | `SelectComponent`, `ComponentList`, `Source`, `LinkReference`, `RuleDisabled` |
| Phase transitions | `Phase`, `OperationType` |

A qualified class matches exactly; a bare class name matches its suffix,
case-insensitively. Exact matches take precedence. Overriding or specializing an
existing class requires `"override": true`. Duplicate classes, unknown keys,
ambiguous attribute mappings and invalid branch definitions fail validation.
`branch_attribute` names the attribute on the condition's **children**;
`branches` maps project labels to JSON booleans, e.g. `{"pass": true, "fail": false}`.
Unrecognized labels on an extended evaluation remain unresolved guards.

The legacy `trace_rule_kinds` setting remains supported for additive suffix
aliases using the standard attributes. Evidence includes an effective semantics
fingerprint and API configuration; the complete custom configuration is copied
into the bundle for offline replay. These extensions declare existing supported
semantics, not arbitrary executable plugins. XML structure, expression syntax,
component mapping format and engine-specific side effects are not universally
configurable in this version.

### Explain a specific scenario

Add `--scenario examples/trace-scenario.json` to a trace. A scenario contains an
optional `name`, a `trigger_eid` identifying a reached Question or Button in the
starting file, and `assumptions`: concrete field paths with `operator: "eq"` and
string `value`. Session fields use the `!` prefix. Select one entry explicitly;
UI scenarios require a unique trigger. For a phase with no UI events the trigger
can be omitted. Scenarios are embedded in `next.json` per target, so replay does
not require the original scenario file. A CLI scenario overrides each requested
target's scenario; otherwise each target retains its own.

Assumptions describe **initial values of the selected activation**, not values
observed at runtime or invariants that survive writes. The analyzer supports
string `==`/`!=`, `AND`/`OR`/`NOT`, parentheses, and whole-field substitutions,
including quoted references. It propagates literal and field assignments and
concrete component mappings in the same event context. It does not execute
expressions or infer numeric coercion. Unsupported functions, escapes, dynamic
instances, loops, resets, unknown effects and unresolved conditional writes
remain unknown. A later known assignment replaces the initial assumed value.
Constants are not propagated between separate UI events. Symbolic mapping
indices are not treated as concrete array instances.

The scenario is an overlay: **excluded paths remain in the original evidence**.
Events are labeled `candidate`, `excluded` (with the excluding condition IDs),
or `other_event_context`. Candidate means possible under remaining prerequisites,
not guaranteed. Condition evidence includes the expression tree, values used,
and their assumption/assignment/copy provenance or an unknown reason.
`summary.md` leads with target paths; `report.md` and `evidence.json` retain the
full reasoning. Existing unresolved-input diagnostics and `--strict` behavior
still describe the full static evidence, including paths excluded by a scenario.

### Temenos OData rule family and conditional request templates

[`examples/temenos-odata-config.json`](examples/temenos-odata-config.json)
adapts the ten configured `com.temenosconnect.odata.rule` classes. It applies
the configured GET/POST/PATCH/DELETE method fallbacks, `QueryOptions` as the
query/filter attribute, and shared request metadata. `ReadRule`,
`ContextualSearchRule`, `CountRule`, and `TranslateRule` treat `DatastoreGroup`
as an output. For the six mutating/action classes it is retained as context with
no assumed direction. This distinction is a profile decision and can be changed
per class through `api_rule_attributes` when a project has firmer semantics.
The file is additive: standard rules stay enabled.

| Rule suffix | Fallback method | `DatastoreGroup` profile |
| --- | --- | --- |
| `ReadRule` | `GET` | output |
| `CreateRule` | `POST` | context, direction unresolved |
| `UpdateRule` | `PATCH` | context, direction unresolved |
| `DeleteRule` | `DELETE` | context, direction unresolved |
| `CompleteRule` | `POST` | context, direction unresolved |
| `InitRule` | `POST` | context, direction unresolved |
| `FunctionRule` | `POST` | context, direction unresolved |
| `ContextualSearchRule` | `GET` | output |
| `CountRule` | `GET` | output |
| `TranslateRule` | `GET` | output |

An explicit method attribute still takes precedence over the suffix fallback.

For every configured API class, a present `Payload` is preserved as a symbolic
JSON template. Its `$%IF` branches and `$$...$` substitutions become request dependencies, but
the JSON is never rendered or sent. `HttpCodeDataItem` and
`HttpMessageDataItem` are trace write targets. A nested API Rule inherits an
ancestor's `RuleDisabled` state in `build` output; `trace` does not enter that
disabled subtree. A child `RuleType` is only a branch label relative to its
parent—it does not by itself establish that an API call will execute.

The synthetic conditional-read example can be traced directly:

```powershell
ifp-contract trace .\examples `
  --file odata-conditional-read.ifp `
  --field 'Response[1].Records[C]' `
  --entry 'Lookup.ConditionalRead' `
  --rules-config .\examples\temenos-odata-config.json `
  --output D:\tmp\conditional-read-trace
```

The same config works with `build --rules-config ...` and its `report` output.
`ServiceRootUri` on a Rule supplies its base URL before any referenced DataSource.
An ambiguous `Endpoint` used as the request path is not also treated as a base
URL. `HTTPHeaderName`, `HTTPHeaderValue`, and `AcceptLanguage` are retained and
their parameter dependencies are traced. Their alias concepts are `header_name`,
`header_value`, and `language`, so projects can add alternative attribute names.

Templates using `$%IF condition$`, `$%ELSE$`, and `$%ENDIF$` retain their raw
text, nested syntax tree, and conditional field dependencies in
`event.api.templates`. Plain paths in predicates such as
`Request[1].AsAtDate != null` are dependencies even without `$$...$`.
Path templates enumerate up to 32 syntactic alternatives with branch guards.
Query templates show conditional fragments, including `and` versus `$filter=`;
they are not expanded into every parameter combination. Malformed or limited
templates carry a diagnostic rather than partial, apparently complete URLs.

The synthetic example has three path alternatives: a selected portfolio, else a
collection when CollectionKey is non-null, else a customer. Its date, region and
archive query fragments preserve their own prerequisites.
Template conditions describe request construction; they are **not** gates for
whether the ReadRule executes. `RuleType="PostPhase"` and the phase's
`ProcessRulesOnly` remain separate configuration evidence.

Null predicates, date functions (`year()`, `month()`, `day()`), URL escaping and
engine whitespace handling are not executed or solved. XML entities are decoded
while raw template whitespace/newlines are preserved. Template alternatives are
symbolic even when a trace scenario is supplied; the scenario solver does not
yet select these routes or render a final URL. Reports label dynamic requests as
templates rather than resolved runtime requests.

Exit status is `0` for successful collection (which can include unresolved
evidence), `1` for file/collection errors, `2` with `--strict` when issues or
external inputs remain, and `130` for interruption. Interruption can be retried
from the same command/request; there is no trace checkpoint database.

## Storage contract

SQLite stores only:

- OData/IRIS rule declarations from the selected Data Integrator file;
- API/OData DataSource declarations and base endpoints;
- caller Rule tags containing the reference string;
- flattened CallComponent mapping attributes from those matching tags;
- Product/Phase ownership and narrow caller-to-API-operation links;
- unknown Rule/reference evidence and its exact file byte offset.

This focused storage policy is global. It does not require a depth, component,
database-size or indexing-mode command-line option. Files with no selected
reference leave no per-file row in the result database.

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

A Rule with incompatible or malformed attributes is recorded as
`INCOMPATIBLE_RULE`, with its file offset and failure reason, and the scanner
continues to later Rules in the same file and to other files. These diagnostics
do not prevent publication; `--strict` returns 2 so they can be reviewed.
Unreadable files, unsupported file encodings and unclosed global XML sections
(such as comments or CDATA) still make the build incomplete. Even a failed
integrator scan does not stop collecting caller evidence in the partial DB.

An explicit caller Product that does not match any API Product remains
unresolved. Single-Product/operation fallback applies only when the caller
does not specify a target. Enabling dynamic-reference diagnostics preserves
explicit references on the same Rule, regardless of attribute order.

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

Use `method_by_rule_class_suffix` to supply project-specific HTTP method defaults,
for example `{"ReadRule": "GET", "CreateRule": "POST"}`. Matching uses the final
class-name segment, ignoring case, and methods are normalized to uppercase.
Explicit method attributes (including configured aliases) take priority, followed
by attributes whose names end in `method`, then this mapping. There are no built-in
class-to-method defaults. Declare API classes in `api_rule_classes` as well;
the method mapping does not itself classify a Rule as an API. Mapping changes
are included in the checkpoint configuration signature.

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

1. Walk `.ifp` metadata to calculate progress totals, then enumerate paths
   again during scanning without retaining a whole-corpus file list.
2. Stream the selected Data Integrator once for `DataSource`, `Product`,
   `Phase`, and `Rule` start tags, writing selected results directly to SQLite.
3. Search every other IFP for the configured reference bytes. Seek back and
   parse selected attributes only when a visible reference is found.
4. Commit each completed caller IFP to the partial SQLite database.
5. Resolve caller Products through a temporary SQLite lookup, optionally compact unused pages,
   validate SQLite, then atomically replace the requested database.

### Invariants to preserve

These are deliberate product constraints, not incidental implementation
details:

- Never call `read_text()` or `read_bytes()` on an IFP in production code and
  never build an XML DOM/tree for a complete file.
- Scan buffers must depend on chunk/tag limits, not corpus or file size.
  Product/Phase nesting and distinct Rule-class vocabulary are tracked in
  memory; operation rows, caller rows and file inventories must not accumulate
  in Python lists during a build.
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
- Isolate recoverable Rule parsing failures and retain an `INCOMPATIBLE_RULE`
  diagnostic; do not stop the remaining Rule or corpus scan.
- A failed or interrupted scan must not replace the last successful database.
  Completed files remain reusable in `<db>.partial`.
- A report opens SQLite read-only, without migrations, and must never trigger
  another corpus scan. Rebuild an older schema before reporting if it lacks
  required fields. Report rendering currently uses memory proportional to
  the extracted contract slice.

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
| `scan_files` | Identity/checkpoint rows for reference hits and failed files only; corpus misses are never indexed. |
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
