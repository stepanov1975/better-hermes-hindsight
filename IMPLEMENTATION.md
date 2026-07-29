# Implementation source of truth

## Current status

- **Canonical plan:** `.hermes/plans/2026-07-27_071437-best-effort-plugin.md`
- **Canonical SHA-256:** `f4d71a33f327510f70e64a1e3d0533281fd8a22862c42e5fbe54d54c08fb6562`
- **Plan state:** Active; Tasks 0–3 and the original active-plan Task 4 contract gate are complete. Deterministic GREEN work proved two SQLite WAL facts omitted by that contract: a coherent active-WAL `mode=ro` read may update the existing derived `-shm` WAL index, while an ordinary read of a fully checkpointed closed WAL-mode file may create empty sidecars. The exact amendment freezes SQLite-owned same-inode SHM coordination/recovery changes, a guarded sidecar-absent `mode=ro&immutable=1` branch, a byte-preserving transient `flock` probe, and the trusted same-principal pathname-reopen limitation. It avoids stale active-WAL reads and a bespoke VFS/WAL parser without claiming hostile same-UID safety. No Task 4 implementation candidate may be finalized until this amendment is independently approved.
- **Code checkpoint:** `ef200c948b738a34f9a74a6ee3f2a964445c5126` (`ef200c9`) is the completed Task 3 sender-delivery and retry checkpoint.
- **Next action:** Independently approve this exact SQLite WAL/SHM amendment from Task 4 contract checkpoint `9579d8a`, then resume the preserved deterministic implementation. Add only bounded sanitized queue diagnostics and explicit mission check/apply commands—no retry/drain command, IPC, model-facing memory tools, generic control plane, installation, deployment, isolated live-write proof, or production rollout.

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
