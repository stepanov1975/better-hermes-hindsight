# Public release checklist

Do not change repository visibility, publish a package, install into an active Hermes environment, or
activate a production canary until every applicable item is verified against one stable release
candidate. Publication and production rollout require separate owner approval.

## Best-effort product contract

- [ ] The package installs and runs on the current stable Hermes release selected by the rolling
      compatibility policy, with no Hermes core patch or Better-owned host fork.
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
- [ ] `codex_app_server` remains explicitly unsupported for Better Hindsight memory behavior on the
      current supported release.
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

- [ ] Follow [development-instance.md](development-instance.md); it defines the environment but does
      not provision Hindsight, Docker, an interpreter, a datastore, or a credential.
- [ ] Fake HTTP, guard, sanitized-subprocess, and fault tests pass before any live write.
- [ ] Development writes use a dedicated Hermes installation/interpreter and temporary profile plus
      an isolated Hindsight 0.8.5 deployment, separate datastore, separate API key, and generated
      disposable bank. Profile isolation alone is not treated as SDK isolation. The installed Better
      distribution is attested to an independently supplied reviewed-wheel SHA-256 and its imported
      modules resolve inside that distribution.
- [ ] The live-proof process contains no production URL, API key, bank ID, or profile state; inherited
      generic `HINDSIGHT_*` values are removed and only explicit development inputs are admitted.
- [ ] `BETTER_HINDSIGHT_ALLOW_DEV_WRITES=1`, literal loopback or exact development-endpoint allowlist,
      independently supplied destination fingerprint, generated-ID shape, and pre-create disposable-
      bank absence all fail closed before mutation. Fake guard failures record zero mutations.
- [ ] The separate development key and datastore remain single-writer for the complete proof because
      Hindsight 0.8.5 exposes create-or-update without conditional creation. A host-local nonblocking
      lock rejects concurrent local proof writers; the run is prohibited if another process can use the
      same key and generated target ID.
- [ ] One explicitly enabled, dedicated-marker proof exercises released-Hermes provider
      discovery/selection,
      first-call current-query recall and bounded fail-open, asynchronous callback admission and sender
      drain, fixed synthetic retention including a long segmented source, public document listing and
      digest-verified reconstruction, byte-identical replay with a positive remote-revision witness, and
      one fresh child-process restart that drains pending work.
- [ ] The same bounded proof performs one fixed usefulness/provenance recall check without aggregation,
      ranking, or thresholds; changes only intended mission fields; disables retention; and proves a
      later callback writes neither local queue rows nor remote documents.
- [ ] The child never deletes the bank: success and failure leave the durable marker for its parent.
      Marker writes loop to completion, are synced and reread before create, and fail closed on partial
      or unverifiable storage.
      Public mission outcomes `runtime_cleanup_failed` and `write_attempted_outcome_unknown` fail the
      proof because they do not prove operator-runtime quiescence. The interval from before process
      launch through descendant-absence proof is exception-total. Timeout and normal leader exit both
      trigger a surviving-descendant check. Interruptions before the proof either settle the known tree
      before propagation or become unsettled; unknown launch outcomes and failed liveness proofs always
      suppress cleanup. After proven absence, propagated Python interrupts run ownership-gated cleanup
      before being re-raised. Parent cleanup is independently bounded and requires a durable local token
      claimed only after the absence guard. Cleanup sends no DELETE on authenticated 404 and deletes a
      present bank only when its ID and public ownership witness both match, so existing-bank rejection
      and timeout before create send no mutation even after the local marker is written. Failed cleanup
      reports only a sanitized generated identifier. The strict one-line result protocol rejects stderr,
      preceding output, extra fields, invalid types, and private development values. No raw endpoint,
      key, bank, profile value, source text, transcript, ownership token, or generated identifier appears
      in repository artifacts or test output.

## Production canary and rollback

- [ ] This checklist documents the proposed production canary but does not activate it; provisioning,
      activation, promotion, publication, and data lifecycle actions require separate authorization.
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

## Rolling compatibility and dependency audits

The release uses a rolling compatibility policy. Hermes is a host application selected by CI, not a
published extra and not a runtime prerequisite. The current stable Hermes release is the required lane;
Hermes 0.19.0 remains a non-blocking historical characterization lane only. Host findings remain
upstream evidence and do not become vulnerabilities in Better Hindsight's distributable package.

- [ ] Better Hindsight code, artifacts, and the complete runtime dependency closure rooted at
      `hindsight-client==0.8.5` pass their blocking security checks.
- [ ] Build or publication tooling findings are resolved when they can compromise the candidate
      artifact or publication process.
- [ ] Every actively supported Hermes compatibility environment passes its required lifecycle suite.
- [ ] A host finding blocks only when the plugin imports or invokes the affected component, exposes
      the affected path, or materially aggravates its risk.
- [ ] Hermes `v2026.8.3`'s `cryptography==48.0.1` findings (`PYSEC-2026-3552`,
      `PYSEC-2026-3553`, and `PYSEC-2026-3554`) remain documented informational upstream evidence:
      they are absent from the plugin runtime closure, and no allowlist or dependency override is used.
- [ ] The complete supported-host audit remains visible as a non-blocking advisory observation;
      failures to execute or collect that audit still fail its diagnostic job.
- [ ] A new stable Hermes release updates the required lifecycle lane, or the documented minimum
      compatible version is raised, before public release.

## Compatibility and artifacts

- [ ] The current stable Hermes release, historical characterization lanes, Hindsight server/client,
      Python, and POSIX platform versions are documented and tested.
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
      inspection, fresh installs, the rolling compatibility matrix, fake-server faults, explicitly enabled
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
