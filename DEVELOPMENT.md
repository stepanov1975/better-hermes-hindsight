# Development Rules

## Purpose

Better Hermes Hindsight is an unofficial memory provider for Alex's Linux, single-principal Hermes deployment. It connects Hermes to a supported external Hindsight service, performs bounded current-query recall, and can retain completed turns through a durable SQLite outbox.

The project follows Hermes development rather than promising compatibility with a permanent release matrix.

## Before changing code

1. Read `README.md`, `DESIGN.md`, and the documents relevant to the change.
2. Run `git status --short --branch` and protect existing work.
3. Identify the smallest change that fixes an observed problem or improves the current deployment.
4. Do not edit the bundled Hermes implementation unless the task explicitly requires a Hermes-core change.

There is no canonical-plan hash, immutable candidate, or mandatory release checklist. Git history, the current issue/task, and verified behavior are the implementation record.

## Product boundary

Keep the provider narrow:

- external/self-hosted Hindsight API versions 0.8.5 and 0.9.1 only;
- Linux/POSIX deployment;
- one configured principal and one static bank;
- multiple Hermes profiles only when each Better-enabled profile runs in its own process;
- normal Hermes memory-provider lifecycle;
- bounded current-query recall;
- optional durable automatic retention;
- bounded model tools for recall, durable retention admission, and passive status/query-free
  diagnostics;
- operator-only mission changes, diagnostic replay, canary, and watchdog commands.

Do not add embedded service management, cloud routing, multi-user bank templates, multiplexed
multi-profile runtime routing, model-facing reflection or policy overrides, migration frameworks,
automatic deletion, Windows support, previous-query background recall, or Hermes-core patches without
a concrete use case.

## Correctness and safety invariants

Preserve these properties:

- recall fails open within configured deadlines and output bounds;
- recalled history is framed as untrusted historical evidence;
- secrets are not committed or intentionally emitted;
- retention is opt-in and local admission performs no remote request;
- outbox rows have stable destination-bound identity and survive restart;
- retries use stable document IDs with replace mode and do not claim exactly-once delivery;
- destination mismatches are never replayed automatically;
- configuration, outbox, and diagnostics remain inside the supplied Hermes home;
- one process accepts only one exact Better Hindsight configuration and fails open on a second;
- mission writes require explicit confirmation and exact readback;
- canary writes require explicit environment opt-in and use synthetic content;
- tests never use production endpoints, credentials, banks, or transcripts.

Do not infer direct-user versus synthetic provenance from text patterns. If Hermes does not provide typed provenance, document and accept that limitation.

## Development approach

- Prefer a surgical fix over a framework.
- Keep working code unless simplification removes a demonstrated maintenance burden.
- Avoid tests that freeze prose, plans, commit hashes, workflow internals, or historical release metadata.
- Test public behavior and realistic failure paths.
- Test the current intended Hermes checkout. Record its version/commit for diagnostics, but do not fail solely because the commit changed when the required interface still works.
- Do not bump the package version for every development commit. Bump it only for an optional tagged snapshot or when deployment identification needs it.

## Verification

For runtime changes, normally run:

```bash
uv lock --check
.venv/bin/python -m ruff check better_hermes_hindsight tests scripts __init__.py cli.py
.venv/bin/python -m ruff format --check better_hermes_hindsight tests scripts __init__.py cli.py
.venv/bin/python -m mypy
.venv/bin/python -m pytest -p no:cacheprovider
rm -rf dist
uv build --out-dir dist
uvx --from twine twine check dist/*.whl dist/*.tar.gz
.venv/bin/python scripts/check_sdist.py dist/*.tar.gz
```

Focused tests may be used during iteration. Run the complete applicable suite before committing runtime or packaging changes. The build/twine/sdist checks are required for packaging or release changes. Documentation-only changes need link/diff review, not the full runtime suite.

Live writes require the already isolated development environment, explicit opt-in, synthetic content, and a disposable or dedicated isolated bank. A failed cleanup should report the resource for manual cleanup rather than expand the test into a transaction manager.

## Completion

Review `git diff --check`, the complete diff, and relevant test output. One independent review is useful for concurrency, durability, authorization, or destructive-data changes; it is not a mandatory checkpoint for every edit.
