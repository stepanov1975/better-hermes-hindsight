# Recall-quality evaluation

Use the source-checkout evaluator to compare labeled Better Hindsight results without printing queries
or recalled memory text. The committed synthetic file is a regression fixture: it proves the schema,
metrics, and privacy contract, but it is not a retrieval-quality benchmark. A meaningful evaluation
uses owner-only historical queries and responses from the configured real Hindsight bank. Keep every
production-derived corpus under the ignored `.hermes/` directory or outside the repository.

## Offline synthetic evaluation

```bash
.venv/bin/python scripts/evaluate_recall_quality.py \
  tests/fixtures/recall_quality_synthetic.json \
  --compare-prefer-observations
```

The command validates the complete JSON corpus and emits one compact JSON report. It reports only
case counts, labeled-result metrics, returned text byte counts, and aggregate elapsed milliseconds.
It does not print case queries, result IDs, or memory text. `elapsed_ms_total` is `null` unless every
case in the variant has a finite timing. Truncated records with no non-whitespace memory prefix are
reported as `fully_truncated_returns` and are not counted as useful evidence.

Without `--compare-prefer-observations`, only the `baseline` fixture response is evaluated.

## Private historical-query workflow

The reusable code is tracked; real queries, recalled text, result IDs, labels, and captured responses
are not. Create an owner-only query pool from direct Hermes user turns:

```bash
mkdir -m 700 -p "$PWD/.hermes/recall-quality"
.venv/bin/python scripts/prepare_recall_quality_corpus.py \
  --state-db /absolute/hermes-home/state.db \
  --output "$PWD/.hermes/recall-quality/query-pool.json" \
  --days 120 \
  --limit 60
```

The collector opens the state database query-only, considers direct `telegram`, `cli`, and `tui`
user sessions by default—including their compression, reset, and branch continuations—removes the
transport timestamp and a complete appended `<memory-context>...</memory-context>` envelope,
deduplicates normalized queries, and rejects typed internal traffic, compaction-summary scaffolding,
attachments, credential-pattern matches, very large turns, and non-user sources. It streams at most
`100 * --limit` newest matching rows and filters stored turns larger than `--max-chars + 64 KiB` in
SQLite before reading their content. As with normal SQLite WAL readers, opening an active read-only
database may update its existing shared-memory coordination file; it does not change message or
session rows. The corpus stores no session or message identifiers, and the command prints only
aggregate counts. The selected query pool has `labels_complete=false`; review it privately and remove
unsuitable cases before recall capture.

Capture production-processed responses from the configured real bank without mutating it:

```bash
HINDSIGHT_API_KEY=... .venv/bin/python scripts/evaluate_recall_quality.py \
  "$PWD/.hermes/recall-quality/query-pool.json" \
  --hermes-home /absolute/hermes-home \
  --compare-prefer-observations \
  --capture-private "$PWD/.hermes/recall-quality/capture.json"
```

The capture file is created once with mode `0600` inside a mode-`0700` directory. It contains the
private queries and production-projected selected responses needed for labeling; stdout contains only
case and returned-size counts. The destination's absolute path, private parent, and non-existence are
preflighted before live configuration or recall. Live timeout/client failure or malformed model-facing
result data aborts the all-or-nothing capture before the private file is created rather than recording
an unreadable artifact or misclassifying a failed recall as an empty result. Review the union of both
variants and classify every returned result ID as useful, redundant, or irrelevant. Set
`expect_recall` for every case, change `labels_complete` to `true`, then evaluate the labeled capture
offline with the first command above. The evaluator refuses an incomplete corpus before loading live
configuration or issuing recall requests; also require `unlabeled_returns=0` before treating a run as
a completed benchmark.

A captured real run is the reproducible primary comparison. A later live rerun can detect current-bank
changes, but new IDs remain unlabeled until reviewed. No synthetic bank is part of this workflow.

## Read-only live A/B evaluation

Create a private corpus with the same case and label fields, but omit each case's `responses` object.
Then run:

```bash
HINDSIGHT_API_KEY=... .venv/bin/python scripts/evaluate_recall_quality.py \
  /absolute/private/recall-quality.json \
  --hermes-home /absolute/hermes-home \
  --compare-prefer-observations
```

The Hermes home must be explicit and absolute. The configured principal must authorize CLI recall.
For each case, the evaluator performs the two read-only recalls adjacently and alternates which variant
runs first across cases:

1. the configured Hindsight request policy;
2. the same immutable configuration with only `recall.prefer_observations` changed to `true`.

Each variant applies the configured production input projection, recall deadline, redaction, candidate
normalized exact deduplication, and model-context byte bound before scoring labeled results. Reported
live elapsed time includes that production processing. Pairing and counterbalancing reduce live-bank
drift and order bias; use a fixed real-bank snapshot when strict snapshot equivalence is required. Both
variants use the same candidate deduplication; the comparison therefore isolates observation
preference rather than measuring deduplication against the currently deployed formatter. The script
rejects the comparison when the configured baseline already prefers observations. The evaluator never
writes configuration, retained memories, diagnostics, or local runtime state. It does not deploy the
candidate plugin or restart Hermes.

A live corpus may label known stable result IDs as:

- `useful_result_ids`: expected evidence for the query;
- `redundant_result_ids`: valid but duplicative evidence;
- `irrelevant_result_ids`: valid records that should not have been returned.

Returned IDs absent from all three lists are counted as `unlabeled_returns`; they are not silently
classified.

## Corpus schema

```json
{
  "schema_version": 1,
  "cases": [
    {
      "id": "stable-private-case-id",
      "query": "One historical recall question",
      "expect_recall": true,
      "labels_complete": true,
      "useful_result_ids": ["expected-result-id"],
      "redundant_result_ids": [],
      "irrelevant_result_ids": [],
      "responses": {
        "baseline": {
          "elapsed_ms": 10.0,
          "results": [{"id": "expected-result-id", "text": "Synthetic fixture text"}]
        },
        "prefer_observations": {
          "elapsed_ms": 9.0,
          "results": [{"id": "expected-result-id", "text": "Synthetic fixture text"}]
        }
      }
    }
  ]
}
```

`labels_complete` is required. It must remain `false` during collection/capture and become `true` only
after every case and returned ID has been reviewed. `responses` is required for offline evaluation and
omitted for live recall. Unknown fields, duplicate JSON keys, duplicate case/result IDs, overlapping
label sets, malformed types, and useful labels on a negative case are rejected.

## Metrics

Each variant reports:

- expected-memory coverage and hit count;
- useful-result precision in the first three returned slots;
- irrelevant, redundant, and unlabeled returns;
- negative cases correctly returning no context;
- returned record and UTF-8 text-byte totals;
- aggregate elapsed milliseconds when available.

Use the metrics comparatively. The synthetic fixture is not a policy benchmark, and no production
threshold or observation preference should be selected from it.
