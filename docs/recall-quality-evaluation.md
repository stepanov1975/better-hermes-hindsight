# Recall-quality evaluation

Use the source-checkout evaluator to compare labeled Better Hindsight results without printing queries
or recalled memory text. The committed corpus is synthetic and only proves the schema and metrics.
Keep private production-derived corpora outside the repository or under the ignored `.hermes/`
directory.

## Offline synthetic evaluation

```bash
.venv/bin/python scripts/evaluate_recall_quality.py \
  tests/fixtures/recall_quality_synthetic.json \
  --compare-prefer-observations
```

The command validates the complete JSON corpus and emits one compact JSON report. It reports only
case counts, labeled-result metrics, returned text byte counts, and aggregate elapsed milliseconds.
It does not print case queries, result IDs, or memory text. `elapsed_ms_total` is `null` unless every
case in the variant has a finite timing. Marker-only records created by the context-byte bound are
reported as `fully_truncated_returns` and are not counted as useful evidence.

Without `--compare-prefer-observations`, only the `baseline` fixture response is evaluated.

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
The evaluator performs two sequential read-only recall passes:

1. the exact configured recall policy;
2. the same immutable configuration with only `recall.prefer_observations` changed to `true`.

Each pass applies the configured production input projection, recall deadline, redaction, normalized
exact deduplication, and model-context byte bound before scoring labeled results. The script rejects
the comparison when the configured baseline already prefers observations. The evaluator never writes
configuration, retained memories, diagnostics, or local runtime state. It does not deploy the
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

`responses` is required for offline evaluation and omitted for live recall. Unknown fields, duplicate
JSON keys, duplicate case/result IDs, overlapping label sets, malformed types, and useful labels on a
negative case are rejected.

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
