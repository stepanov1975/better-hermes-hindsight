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
