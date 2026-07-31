# Public release checklist

Do not change repository visibility, publish a package, install into an active Hermes environment, or
activate a production canary until every applicable item is verified against one stable release
candidate. Publication and production rollout require separate owner approval.

## Best-effort product contract

- [ ] The package installs and runs on unmodified released Hermes `v2026.7.20` / package 0.19.0,
      with no Hermes core patch or patched SHA.
- [ ] Provider identity is only `better_hindsight`; bundled `hindsight` remains selectable and
      untouched.
- [ ] Current-query recall is bounded, fail-open, redacted, and enabled by default in the normal
      conversation loop.
- [ ] Automatic retention is disabled by default and requires explicit profile opt-in on an
      authorized primary handle.
- [ ] Enabled retention accepts non-empty user/final-assistant text from the released `sync_turn()`
      callback without origin heuristics.
- [ ] Documentation and tests make no direct-user provenance claim and state that durability begins
      only after the provider admission commit.
- [ ] Documentation and tests state there is no pre-return or no-loss guarantee for callbacks Hermes
      never invokes or local admissions that fail.
- [ ] The callback path performs only bounded local construction and one atomic SQLite transaction;
      it makes no Hindsight request and does not wait for remote drain.
- [ ] `codex_app_server` remains unsupported for Better Hindsight memory behavior on the pinned
      release.
- [ ] No model-facing memory tools ship in the first prerelease.

## Queue and remote delivery

- [ ] All-or-none admission, duplicate handling, collision rejection, path confinement, logical row
      and payload caps, SQLite contention, and saturation are tested with sanitized outcomes.
- [ ] `retain.segment_max_bytes` cannot exceed `outbox.max_pending_bytes`; initial retry cannot exceed
      maximum retry; every configured queue and retain-deadline number has its documented finite
      ceiling.
- [ ] One profile-wide POSIX lock owner recovers and sends rows, including rows admitted by another
      process observed through bounded polling.
- [ ] Only rows matching the current destination fingerprint and payload schema are claimed;
      mismatches stay blocked and visible.
- [ ] Every retry preserves the same stable document ID and `update_mode="replace"` payload.
- [ ] Multi-segment reconstruction metadata is sent through public Hindsight item metadata; shuffled
      remote segments reconstruct the original source and verify its digest after local completion.
- [ ] Completion requires the exact audited synchronous response predicate. Timeout, malformed
      response, process stop, or ambiguous remote completion leaves the row retryable.
- [ ] The product claims replace-safe replay, not exactly-once transport or global FIFO.
- [ ] Bounded shutdown stops new work and leaves unconfirmed admitted rows recoverable.

## Trust, credentials, and missions

- [ ] Recall formatting identifies content as potentially stale, untrusted historical evidence.
- [ ] Deterministic high-confidence redaction runs before recall egress and retention admission; its
      limitations are documented without claiming universal secret detection.
- [ ] API keys remain environment-only, secret-bearing JSON keys are rejected, and public errors,
      representations, tests, and reports expose no secrets or private destination values.
- [ ] Exact principal authorization, `single_principal`, `shared`-scope gating, and primary-context
      retain gating pass focused tests.
- [ ] Hindsight OSS 0.8.5 shared-key risk is explicit; missing tools and confirmation flags are not
      represented as server-enforced authorization.
- [ ] Retain and observation mission text remain distinct. Check/apply is an explicit operator action,
      initialization does not apply policy, and apply requires confirmation plus readback.

## Isolated development proof

- [ ] Fake HTTP contract and fault tests pass before any live write.
- [ ] Development writes use an isolated Hindsight instance and Hermes profile, separate datastore,
      separate API key, and generated disposable bank.
- [ ] The live-proof process contains no production URL, API key, bank ID, or profile state.
- [ ] Explicit write opt-in, endpoint allowlist, independently supplied destination fingerprint, and
      pre-upsert disposable-bank absence all fail closed before mutation.
- [ ] Real released-Hermes callback proof observes admission only after asynchronous callback
      execution and does not reinterpret it as inline turn-return durability.
- [ ] Disposable resources are cleaned up in `finally`; failed cleanup reports only a sanitized
      generated identifier.

## Production canary and rollback

- [ ] Production rollout uses a dedicated Hermes interpreter/profile and separate canary instance
      and bank, preserving the old deployment.
- [ ] The existing Hindsight service, bank, provider configuration, and data remain running and
      untouched as the rollback source; prerelease proof performs no migration, reconstruction,
      deduplication, reconsolidation, pruning, or deletion.
- [ ] Canary activation is separately authorized after isolated proof and evaluates recall usefulness
      plus retained-source quality without mutating the old bank.
- [ ] Rollback is not called configuration-only: it stops every Hermes process sharing the
      interpreter, selects bundled `hindsight` for the target named profile, removes that profile's
      host-owned Git plugin, uses `uv pip --python` to remove the Better wheel and restore exact
      `hindsight-client==0.6.1`, restarts only compatible profiles, verifies first recall without lazy
      package installation, and preserves Better's outbox plus both banks.
- [ ] Returning to Better explicitly reinstalls the wheel and `hindsight-client==0.8.5`, installs the
      reviewed Git plugin, verifies discovery before selection/restart, and does not migrate or delete
      either bank/outbox.
- [ ] Released `hermes plugins remove` owns only the Git plugin directory; profile configuration,
      Python packages, outbox data, and remote bank data remain outside that ownership.

## Compatibility and artifacts

- [ ] Supported Hermes, Hindsight server/client, Python, and POSIX platform versions are documented
      and tested.
- [ ] Imports, discovery, availability checks, and configuration loading perform no network call,
      package installation, bank mutation, or service restart.
- [ ] Host-managed Git plugin installation and fresh-process provider/CLI discovery pass in a
      disposable repository and temporary `HERMES_HOME`.
- [ ] Explicit Better `0.8.5` and bundled `0.6.1` SDK states are exercised through the documented
      `uv pip --python` transition after every process sharing the interpreter is stopped.
- [ ] Wheel/sdist metadata, packaged provider/CLI resources, license, docs, and third-party notices
      are verified without freezing host source bytes or package-manager internals.
- [ ] Pre-alpha warnings are replaced with accurate prerelease status and install instructions.

## Public safety and repository health

- [ ] Full history and candidate artifacts pass Gitleaks and repository-configured Semgrep rules.
- [ ] Public files contain no private endpoints, credentials, bank IDs, principal IDs, paths,
      memories, transcripts, databases, logs, or generated runtime state.
- [ ] README, design, configuration, compatibility, operations, rollback, examples, release notes,
      and generated diagnostics agree on the best-effort boundary.
- [ ] `uv lock --check`, full pytest, Ruff lint, Ruff formatting, mypy, build, Twine, archive
      inspection, fresh installs, released-Hermes integration, fake-server faults, explicitly enabled
      isolated live proof, security scans, and `git diff --check` pass on one stable candidate.
- [ ] CI and security workflows pass for the exact release commit.
- [ ] Dependabot alerts and dependency review are clear or explicitly resolved.
- [ ] Independent contract/architecture and implementation/operations reviews report no unresolved
      Blocking or Important finding within the agreed best-effort scope.

## Release

- [ ] Replace version `0.0.0` with an explicit prerelease version.
- [ ] Update `CHANGELOG.md` and prepare release notes naming limitations and rollback.
- [ ] Build artifacts from a clean checkout and verify them in fresh environments.
- [ ] Add a guarded release workflow only after release behavior is reviewed.
- [ ] Change repository visibility or publish only after explicit owner approval.
- [ ] Read back visibility, tag, assets, checks, package metadata, and security settings.
