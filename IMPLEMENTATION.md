# Implementation source of truth

## Current status

- **Canonical plan:** `.hermes/plans/2026-07-27_071437-best-effort-plugin.md`
- **Canonical SHA-256:** `bb575de8723f2a2d70054700c2713d0154ae59181cd5546ee611ecae328ddf62`
- **Plan state:** Active; Tasks 0–4 are complete. The Task 4 contract and SQLite WAL/SHM amendment remain checkpointed as `9579d8af0098899cdb0ebe3447c2bb57fb4519da` and `0bc681cf6ab066bce6a9793c9d72157886aae2e4`; the exact implementation passed independent specification and adversarial review before checkpoint. The review-amended Task 5 contract combines the approved `0.8.5`/`0.6.1` operator package transition with scanner-proof nested transaction trees, root-safe owned checked-hash bytecode, exact command outcomes and host-module provenance, and phase-complete retry-convergent cleanup/uninstall state machines. That contract passed independent specification, quality, and adversarial review and is checkpointed as `6652212a5aaa72833e3df050523652fa3b935583`.
- **Code checkpoint:** `9843a9b802ce54b5483a2adb7e95aff989d1df0f` (`9843a9b`) is the completed Task 4 bounded diagnostics and explicit mission-management checkpoint.
- **Next action:** Write deterministic RED tests for Task 5 transactional install/publication, upgrade rollback, ownership refusal, uninstall/retry cleanup, archive contents, released-host discovery/health, and Better `0.8.5` versus bundled `0.6.1` version agreement. Do not write production installer code until those tests fail for the intended missing behavior. Do not install into a live Hermes home/interpreter, invoke pip from the shim manager, select a provider, restart Hermes, touch profile configuration/outboxes, contact Hindsight, deploy, or roll out production.

The canonical plan is intentionally local under `.hermes/plans/`, which is ignored because Hermes runtime state and private artifacts do not belong in Git. This tracked file is the durable cross-session router. If the canonical file is missing or its hash differs, stop and resolve the plan state instead of selecting another plan.

## Retired plans

These files are historical evidence only and must never drive implementation:

1. `.hermes/plans/2026-07-25_194157-better-hermes-hindsight-implementation.md`
   - Retired because it made broad Hermes-core origin/trust/inline-admission patches and a patched SHA into product prerequisites.
2. `.hermes/plans/2026-07-27_055353-plugin-only-rescope.md`
   - Retired because it overcorrected to recall-only/operator-CLI behavior and removed useful best-effort automatic retention.

Both files carry title-level retirement banners. Instructions below those banners remain only to preserve design history.

## Precedence for every session

1. Read this file before `README.md`, `DESIGN.md`, or implementation code.
2. Verify the canonical plan path and SHA-256 above.
3. Follow only the canonical active plan and current Git evidence.
4. Treat the retired plans as non-authoritative even when their old prose says “required,” “execution rule,” or “production prerequisite.”
5. Keep the separate Hermes-core worktree frozen research. Never import, install, commit, or make its SHA a Better Hindsight prerequisite.
6. Keep the Task 0 product documents aligned with this router and the canonical active plan; resolve any future conflict in favor of the router and canonical plan.
7. Update this router whenever the active plan path, hash, review state, or next task changes.

## Active product direction

The product is a plugin-only, best-effort integration for released Hermes and self-hosted Hindsight 0.8.5:

- bounded automatic current-query recall;
- opt-in automatic retention from released `sync_turn()` callbacks;
- retry durability beginning only after the plugin's own SQLite admission commits;
- passive bounded queue diagnostics and confirmation-gated mission check/apply commands;
- no authoritative human/synthetic-origin claim;
- no pre-callback or pre-turn-return zero-loss claim;
- no Hermes-core prerequisite;
- no model-facing memory tools in the first prerelease;
- isolated Hindsight development/canary instances, preserving the existing deployment for rollback.
