# Bundled-vs-Better provider shadow benchmark

Use this benchmark for a release-gated, end-to-end comparison of Hermes' bundled Hindsight memory provider and Better Hindsight. It runs both provider orders, retaining the same synthetic corpus through each real provider lifecycle into a fresh disposable Hindsight bank, then runs the same current-query recall cases.

It is intentionally **not** a default CI job. A meaningful quality comparison needs an isolated live Hindsight service and a representative model, which can be nondeterministic and incur model cost. The deterministic Hindsight mock provider is useful only to prove the benchmark's mechanics.

## What it measures

The versioned fixture at `tests/fixtures/provider_shadow_benchmark.json` covers:

- timeless facts and explicitly dated events;
- relative-time phrasing;
- repeated identical events;
- cross-turn pronouns;
- stale facts followed by updates;
- prompt-injection-like text retained only as untrusted synthetic data;
- a negative case with no matching memory.

For each provider, the public report includes:

- factual, temporal, and negative-case accuracy;
- p50 and p95 recall latency;
- aggregate irrelevant-result and duplicate-result counts;
- first-call timeout/fail-open behavior plus the immediate retry;
- explicit `unavailable` usage/cost telemetry when Hermes exposes none.

The report pins the synthetic corpus, mission texts, clean Better and Hermes source identities, the bundled interpreter's `hindsight-client` version, Hindsight API/build identity, model identity, and aligned policy hashes. Dirty source trees are rejected because untracked imports cannot be represented by a commit plus patch digest.

## Safety boundary

- Use an isolated non-production Hindsight service and datastore. Never point this benchmark at a service that contains real memory.
- Writes require `BETTER_HINDSIGHT_ALLOW_BENCHMARK_WRITES=1`.
- Loopback endpoints are allowed directly. A non-loopback origin must use HTTPS and must also be passed exactly with `--allow-endpoint`; URL paths, credentials, queries, fragments, and redirects are rejected.
- The API key is read only from `HINDSIGHT_API_KEY`; it is never accepted on the command line or written to config files.
- Child processes receive a narrow environment and a private temporary Hermes home.
- Each child verifies the parent-selected corpus digest before loading it, and the child deadline scales with the permitted sample count and operation budgets. Both providers receive the same 60-second retention allowance; recall measurements use the same five-second host deadline.
- Each provider runs once in each position of a counterbalanced pair, using a fresh randomly named bank per run. All four banks are created before measurement so cleanup covers partial failures; deletion polls for delayed creation, revalidates ownership, and verifies absence.
- The final JSON and human summary contain aggregate evidence only—no query text, recalled text, audit markers, endpoint, credentials, or bank identifiers.

## Prepare the selected Hermes interpreter

The exact Python executable must be the Hermes interpreter under test. It must be able to import this Better Hindsight checkout, the selected Hermes source tree, and the bundled provider's declared client dependency:

```bash
uv pip install --python /path/to/hermes/python "hindsight-client>=0.6.1"
```

Record the exact Hermes commit and the immutable Hindsight build identifier before running. The benchmark reads and records the selected interpreter's installed `hindsight-client` version. For a container, use its digest rather than a mutable tag. Record the actual model provider, model ID, and model build/revision; these values are operator-declared because the provider lifecycle does not expose them.

## Run

```bash
export HINDSIGHT_API_KEY='[REDACTED]'
export BETTER_HINDSIGHT_ALLOW_BENCHMARK_WRITES=1

HERMES_SOURCE=/path/to/hermes-agent
HERMES_PYTHON=/path/to/hermes/python
HERMES_COMMIT="$(git -C "$HERMES_SOURCE" rev-parse HEAD)"

"$HERMES_PYTHON" scripts/benchmark_provider_shadow.py \
  --api-url http://127.0.0.1:8888 \
  --expected-version 0.9.2 \
  --hermes-python "$HERMES_PYTHON" \
  --hermes-source "$HERMES_SOURCE" \
  --hermes-commit "$HERMES_COMMIT" \
  --hindsight-build 'sha256:<immutable-image-digest>' \
  --model-provider '<provider>' \
  --model-id '<model>' \
  --model-build '<revision>' \
  --samples-per-case 3 \
  --output /tmp/provider-shadow-report.json
```

For an explicitly isolated non-loopback service, add the same origin as an allowlist entry:

```bash
  --api-url https://isolated-hindsight.example.test \
  --allow-endpoint https://isolated-hindsight.example.test
```

`--samples-per-case` accepts 1–20 per counterbalanced round, so model usage is twice the per-provider sample count. Use the same value, service build, model, missions, and source commits when comparing runs.

When `--corpus` selects anything other than the checked-in default fixture, the report labels it `operator_supplied_synthetic` and retains its exact digest rather than claiming checked-in provenance.

## Interpreting results

A successful run proves that both actual Hermes provider lifecycles completed in both execution positions, could retain the complete corpus, recall, fail open under the bounded host timeout, retry without eventually sending another backend request, and clean up owned test state. Quality scores remain descriptive evidence for the pinned corpus and model—not a universal claim that one provider is superior.

The mock provider can legitimately score zero when it does not preserve the fixture's audit labels. That is a model-quality result, not an orchestration failure, as long as both provider paths completed, fail-open probes passed, and cleanup verified.

The benchmark reports usage/cost as unavailable unless provider interfaces expose attributable telemetry. It never estimates or fabricates model cost.
