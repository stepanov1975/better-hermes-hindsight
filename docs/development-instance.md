# Isolated live validation

The live test targets an isolated supported Hindsight service, datastore, credential, and disposable bank namespace from the normal Hermes development environment. It uses synthetic content, requires an exact expected API version, and creates a random bank for each run.

## Required opt-in

The test skips unless all required values are present:

```bash
export BETTER_HINDSIGHT_ALLOW_DEV_WRITES=1
export BETTER_HINDSIGHT_REQUIRE_LIVE_PROOF=1
export BETTER_HINDSIGHT_DEV_API_URL=http://isolated-host:8888
export BETTER_HINDSIGHT_DEV_API_KEY='...'
export BETTER_HINDSIGHT_DEV_EXPECTED_VERSION=0.9.1
export BETTER_HINDSIGHT_DEV_HERMES_PYTHON=/path/to/current/hermes/python
```

For a non-loopback endpoint, also provide an exact comma-separated allowlist:

```bash
export BETTER_HINDSIGHT_DEV_ALLOWED_ENDPOINTS=http://isolated-host:8888
```

Do not reuse production endpoints, credentials, banks, or content. The API key value must never be printed.

The exact version selector also accepts `0.10.0`, not arbitrary newer versions. For that candidate,
start the isolated **server** with `HINDSIGHT_API_TOKENIZER_ENCODING=cl100k_base` and verify its
`HINDSIGHT_API_RECALL_MAX_QUERY_TOKENS` ceiling. Neither this selector nor a healthy `/version`
attests tokenizer policy. Run the one-time [boundary-query check](compatibility.md#verifying-the-server-policy)
with a 500-token server ceiling, and record actual HTTP success/rejection rather than empty
fail-open provider context. The development selector/offline tests alone are not live 0.10.0 proof.
The scheduled/manual live matrix below covers 0.9.2 and 0.10.0 separately; an offline pass or skipped
live test is not a substitute for either version's recorded live result.

## What the smoke test proves

The test:

1. validates the explicit opt-in, supported exact Hindsight version, and endpoint allowlist;
2. checks the selected interpreter exposes the intended Hermes host and self-contained SDK-free Better plugin;
3. generates a random `better-hindsight-live-...` bank and verifies it is absent;
4. creates that bank with a unique synthetic ownership display name;
5. starts the real Hermes memory manager with a temporary home and Better provider;
6. verifies bounded current-query recall;
7. admits synthetic retention, observes durable local rows, and waits for remote delivery;
8. restarts the runtime with pending work and verifies convergence without duplicate document identity;
9. shuts down and finalizes the runtime; and
10. deletes only the generated bank after its ID and ownership display name still match.

The test uses ordinary `try/finally` cleanup. If deletion or absence confirmation fails, it reports the generated bank ID for manual cleanup in the isolated development service. It does not implement process-tree containment, local ownership-marker protocols, or automatic inference about any existing deployment.

## Run

```bash
.venv/bin/python -m pytest \
  -p no:cacheprovider \
  tests/integration/test_isolated_hindsight.py
```

When `BETTER_HINDSIGHT_REQUIRE_LIVE_PROOF=1`, a missing opt-in input is a failure rather than a skip. Otherwise the live test skips so normal deterministic development remains offline.

## Automated compatibility proof

The scheduled and manually dispatchable `Python 3.13 / Hindsight ... live` CI matrix runs this same
test against exact **0.9.2 and 0.10.0** release images pinned by digest in
`.github/workflows/ci.yml`. Each lane owns a disposable PostgreSQL **18.4 / pgvector 0.8.5** service,
uses Hindsight's real API, local embeddings and local reranker, and selects its deterministic mock
LLM, so it needs no third-party credentials. The 0.10.0 lane explicitly selects `cl100k_base` and a
500-token recall ceiling. The Python readiness probe works without curl inside the candidate image.
The job checks out current Hermes `main` and records the exact Better commit, Hermes commit,
Hindsight version response, and Hindsight image digest before testing.

GitHub Actions owns the disposable service-container lifecycle. The test still creates and
ownership-checks a random bank, deletes it in `finally`, and verifies absence; job teardown then removes
the complete container and embedded datastore even when a test fails. This proves transport, schema,
provider discovery, durable admission, restart recovery, remote convergence, current-query recall, and
cleanup against the supported server release. It also verifies mission drift, confirmed apply, exact
readback, and fixed adapter mapping for a real service-generated 404. It does not measure hosted-LLM
quality, cost, or provider credentials.
