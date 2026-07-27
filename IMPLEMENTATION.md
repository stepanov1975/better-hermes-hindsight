# Implementation source of truth

## Current status

- **Canonical plan:** `.hermes/plans/2026-07-27_071437-best-effort-plugin.md`
- **Canonical SHA-256:** `e51752970fc91f5d604e5c5bb74e6ca938172d275d0d70ea559e704fc31e34c7`
- **Plan state:** Active and independently approved. The amendment-only recheck approved the exact canonical hash after confirming both prior Important findings were resolved; no Blocking or Important plan finding remains open.
- **Code checkpoint:** `spike/local-external-provider` at `4e437fc` is the completed recall-only baseline. No task from the active best-effort plan has been implemented yet.
- **Next action:** Begin active-plan Task 0. Do not resume work from a retired plan.

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
6. Until active-plan Task 0 rewrites all product documents, any conflict in `README.md`, `DESIGN.md`, or `docs/*.md` is resolved in favor of this router and the canonical active plan.
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
