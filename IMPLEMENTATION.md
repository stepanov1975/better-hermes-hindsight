# Implementation source of truth

This file is the short repository router. Detailed contracts live in the canonical plan and the
linked operator documents; this page records which contract is active so stale session history
cannot restart abandoned work.

## Current checkpoint

- Canonical plan: `.hermes/plans/2026-07-27_071437-best-effort-plugin.md`
- Canonical-plan SHA-256:
  `73471e6c186e99e1b65cd1316de63446fbba8da0b7c6618f5f93376220484a8d`
- Last completed remote candidate checkpoint: `030aeff` (`ci: remove prerelease bootstrap dependency`)
- Released GitHub prerelease checkpoint: `3404516` (`docs: close prerelease candidate evidence`)
- Compatibility rebaseline checkpoint: `2a05a10` (`ci: rebaseline Hermes compatibility gates`)
- Plan state: Tasks 0–8 and the rolling Hermes compatibility/release rebaseline are complete. Task 8
  is the current unreleased follow-up and adds only bounded read-only `better_hindsight_recall`; it
  does not add model-facing retain, reflect, mission, or configuration tools. The first-prerelease
  functional candidate is `030aeff`; evidence-only closure `3404516` is tagged `v0.1.0a1` and published
  as a GitHub-only development prerelease. Initial focused review found a non-portable checksum manifest
  and an incomplete source distribution; both fixes passed exact-candidate re-review. Later reviews
  closed literal truncation artifacts, corrected released-Hermes callback scheduling prose, scoped
  recursive gates away from the nested compatibility checkout, and removed a pre-sync packaging import
  from prerelease identity validation. Candidate `030aeff` passed independent exact-candidate review,
  exact-commit GitHub CI run `31390417755`, and security run `31390417898`; the pushed default branch
  then reported no open Dependabot alerts. This status closure changes no executable, package, or
  release-workflow behavior. Exact-tag build run `31402768287` passed with PyPI jobs skipped; an
  independently reviewed immutable install procedure closed Git checkout and wheel provenance findings
  before the GitHub release was created. Production canary activation and visibility changes remain
  pending separate authorization.
- Completed Task 5 scope: remote segment reconstruction metadata plus a thin root plugin layout for
  the released Hermes Git plugin lifecycle
- Security scope: Better Hindsight code, artifacts, its complete runtime dependency closure, and the
  locked project-owned build/publication tooling are release gates. Hermes `v2026.8.3`'s unrelated
  `cryptography==48.0.1` findings remain upstream host observations because the plugin neither depends
  on that package nor invokes the affected paths.
- Publication boundary: GitHub prerelease `v0.1.0a1` contains the exact workflow-built wheel, sdist,
  and checksum manifest. Trusted PyPI publishing is deferred. The `pypi` environment is restricted to
  exact tag `v0.1.0a1`, while `PYPI_RELEASE_CONFIGURED` remains absent and no PyPI project exists, so
  PyPI publication remains fail-closed. Task 8 is unreleased, and no production or canary deployment
  occurred.

## Completed foundation

Tasks 0–5 provide:

- deterministic config and destination identity;
- first-turn fail-open recall through the released memory-provider lifecycle;
- redacted canonical turn retention with local SQLite admission;
- bounded asynchronous sending, stable replace-mode replay, retry accounting, and process ownership
  controls;
- public-SDK Hindsight 0.8.5 integration;
- operator `status` and `missions` controls, with no retry/drain/dead-letter command or dead-letter
  state; and
- reconstructable remote segments, released host-managed Git plugin installation, and package,
  compatibility, security, and release documentation.

Checkpoint `883ef6c` passed 547 tests, Ruff, formatting, mypy, lock and diff checks, package builds,
Twine validation, disposable released-Hermes install/load/CLI/remove proof, bounded independent
review, and a clean focused re-review.

## Task 5 rebaseline

The previous Task 5 specification and uncommitted RED oracle were abandoned after a read-only
goal-alignment review. The candidate had grown to 10,144 lines and 1,928 test nodes covering an
independent filesystem transaction/package manager: tombstones, quarantine, exact host-source
snapshots, bytecode ownership, xattrs, descriptor behavior, fsync ordering, adversarial mutation,
and a separate rollback engine.

That design was disproportionate to the product. Hermes 0.19.0 already owns Git plugin install,
update, remove, and memory-provider discovery. The abandoned test file was removed; do not recover
or continue it from Git objects, session transcripts, delegation logs, or stale patches.

The active Task 5 contract is deliberately narrow:

1. Forward existing outbox `payload_schema`, `source_sha256`, `segment_index`, and `segment_count`
   through Hindsight's public string metadata so multi-segment sources remain reconstructable after
   local completion.
2. Add root `plugin.yaml`, `__init__.py`, and `cli.py` as thin bridges to the installed Python
   package.
3. Prove released `hermes plugins install` and fresh-process provider/CLI discovery in disposable
   homes and local Git repositories.
4. Keep package and exact SDK transitions explicit, stopped-process operator work.
5. Document rollback using bundled-provider selection, released plugin removal, and `uv` after every
   process sharing the interpreter is stopped. Do not migrate, drain, or delete either bank or
   Better's outbox.
6. No custom installer, transaction tree, provenance manifest, or filesystem rollback engine ships.

## Task 4 authority transition

Task 4's exact authority snapshot was accepted at checkpoint `9843c4b`. Task 5 legitimately edits
some tracked authority documents and tests, so their hash table in
`tests/test_repository_contract.py` must transition together on the reviewed Task 5 candidate.
The behavior contracts remain in force; changing an authority hash alone is never evidence that a
semantic change is valid.

## Later work

Task 6 uses one bounded usefulness and retained-source proof against an operator-supplied dedicated
Hermes interpreter/profile plus isolated Hindsight 0.8.5 development instance and generated bank. The
initial run used only synthetic data, removed its generated bank, and left an authenticated bank count
of zero; its reviewed checkpoint is `3f542d4`.
Profile isolation alone does not isolate the exact Hindsight SDK. The proof must not add repeated-run
aggregation, a ranking framework, or release thresholds. Publication, production canary activation,
migration, reconstruction, pruning, and deletion remain separately authorized work.

Before Task 7, Hermes compatibility is a rolling CI-selected host contract. The current stable release
is the required lifecycle lane; Hermes 0.19.0 remains non-blocking historical characterization. No
published dependency group includes `hermes-agent`. Security audits block on Better Hindsight's code,
artifacts, complete runtime dependency closure, and locked project-owned tooling; unrelated host
findings remain visible in a separate informational audit and become blocking only when the plugin
invokes, exposes, or aggravates the affected path.

## Operational ownership

- Profile: an existing Hermes profile selected with `hermes --profile <name>` or an already
  profile-specific `HERMES_HOME`; there is no `BETTER_HINDSIGHT_PROFILE` setting
- Local path: `$HERMES_HOME/better_hindsight/outbox.sqlite3` for the selected profile
- Runtime owner: one sender per profile/process; cross-process mutation requires the profile lock
- Package owner: `uv`; the wheel and exact SDK are interpreter-global, so every sharing process must
  be stopped before transition
- Existing deployment: unchanged until a separately authorized canary
- Rollback principle: preserve both banks and Better's outbox; switch providers while stopped
- Security principle: no raw session identifiers, credentials, bank values, or source text in
  remote reconstruction metadata, status output, logs, or committed fixtures
