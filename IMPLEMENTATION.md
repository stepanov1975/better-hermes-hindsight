# Implementation source of truth

## Current status

- **Canonical plan:** `.hermes/plans/2026-07-27_071437-best-effort-plugin.md`
- **Canonical SHA-256:** `da6610578119ba9b8d0539cebd58372768e3ba63166aff778e3df6344ff7b0f9`
- **Plan state:** Active; Tasks 0–3 are complete. The Task 4 contract was amended after the independent Stage 0 gate found behavior-defining gaps in diagnostics, operator-runtime ownership, mission response predicates, ambiguous writes, released command discovery, and CLI exits. No Task 4 production source may be edited until this exact amendment is independently approved.
- **Code checkpoint:** `ef200c948b738a34f9a74a6ee3f2a964445c5126` (`ef200c9`) is the completed Task 3 sender-delivery and retry checkpoint.
- **Next action:** Independently approve this exact active-plan Task 4 contract from `ef200c9`, then write its deterministic RED tests. Add only bounded sanitized queue diagnostics and explicit mission check/apply commands—no retry/drain command, IPC, model-facing memory tools, generic control plane, installation, deployment, isolated live-write proof, or production rollout.

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
- no authoritative human/synthetic-origin claim;
- no pre-callback or pre-turn-return zero-loss claim;
- no Hermes-core prerequisite;
- no model-facing memory tools in the first prerelease;
- isolated Hindsight development/canary instances, preserving the existing deployment for rollback.
