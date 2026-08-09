# Sanitized audit findings

This document preserves public-safe aggregate evidence from the read-only audit. It does not make a
Hermes-core patch or ideal host lifecycle a release prerequisite. The active product contract is the
best-effort plugin described by tracked [IMPLEMENTATION.md](../IMPLEMENTATION.md) and its canonical
plan; the two older plans are retired historical records.

A read-only audit completed on 2026-07-25 found that Hermes's bundled Hindsight integration was
functional but exposed only part of the current Hindsight capability surface. The complete
instance-specific audit remains outside this public-intended repository. The figures below are
sanitized aggregates; no endpoint, bank identity, principal identifier, memory text, transcript,
or row-level export is included.

## Compatibility and latency evidence

- The active Hermes package was 0.19.0 and its installed Hindsight client was 0.6.1, while the
  audited server and current client release were 0.8.5.
- Automatic recall without a useful score floor returned dozens of memories and roughly
  8.5–11 kB per query.
- The best small live-instance candidate tested—`budget=low`, observation-only, and 1,024 recall
  tokens—measured about 2.41 seconds median and 2.65 seconds p95.
- Hindsight reflection was materially more expensive than recall in both latency and tokens, so
  automatic per-turn reflection is not justified.

## Aggregate quality evidence

The follow-up live snapshot contained:

- 874 documents;
- 13,197 raw memory units;
- 8,451 active observations;
- 82.65% of observations carrying volatile session lineage;
- 73.67% of observations supported by one proof; and
- 0.54% active exact duplicates after consolidation.

The durable quality problems are heavy session scoping and weak support, not a need for
client-side semantic deduplication. Exact duplicate observation text and an unusually large
session document were observed, but neither their content nor identifiers are retained here.

A small relevance trial found `min_scores.final = 0.10` removed every tested negative-control
result set while preserving every tested initial positive operational case. This is a promising
project-specific candidate to evaluate, not a justified universal default.

## Current supported-host security gate

A fresh 2026-08-09 `pip-audit` of Better Hindsight's runtime/build manifest found no known
vulnerabilities. The separate combined environment for current Hermes release `v2026.8.3`
(package 0.20.0) did not pass: Hermes pins `cryptography==48.0.1`, and the audit reports
`PYSEC-2026-3552`, `PYSEC-2026-3553`, and `PYSEC-2026-3554`. The reported fixes require
cryptography 49.0.0 or 50.0.0, outside that released host's exact dependency pin.

This is an upstream supported-host release blocker, not a Better Hindsight wheel finding. There is
no allowlist, dependency override, or checkpoint exception: public release remains blocked until a
supported Hermes release resolves the findings and the combined-environment audit passes. The
failing audit does not invalidate the passing provider-lifecycle compatibility tests.

## Active product disposition

1. Preserve current-query first-turn recall with a bounded fail-open deadline. Recall is enabled by
   default and remains the only remote or potentially long-running pre-model memory operation.
2. Treat recalled material as potentially stale, untrusted historical evidence. Support bounded
   score-floor and observation-preference controls without turning the project-specific trial into a
   universal default.
3. Keep automatic retention disabled by default. When explicitly enabled, accept the non-empty
   completed-turn callbacks released Hermes actually supplies through released `sync_turn()` and do
   not infer human/synthetic origin from text.
4. Local durability starts only after provider admission commits the complete redacted callback to
   the profile SQLite outbox. There is no direct-user provenance claim and no pre-return or no-loss
   guarantee.
5. Preserve destination matching, one profile sender, bounded cross-process polling, stable
   replace-mode replay, and retry recovery. Do not claim exactly-once transport or global FIFO.
6. Keep retain and observation mission text distinct. Checking is explicit and read-only; applying
   requires `better_hindsight missions apply --confirm`, changes only configured drift, verifies exact
   readback, and never becomes initialization policy.
7. Accept Hindsight's documented `shared` observation scope only after exact principal and exclusive
   bank proof. Hindsight's shared write-capable key is not a server-enforced provider policy boundary.
8. Ship no model-facing memory tools in the first prerelease.
9. Require an isolated Hindsight instance and Hermes profile for development writes. Use a separate
   canary instance and bank for production evaluation, preserving the old deployment and bank for
   rollback.
10. Preserve source documents and improve ingress/retrieval before considering any separately
    authorized legacy-bank migration.

## Superseded ideal requirements

Two former requirements are retained here only as design history:

- **Authoritative structured origin.** A typed direct-human versus synthetic signal across every
  Hermes ingress would be useful upstream, but released callbacks do not provide that authority and
  Better Hindsight does not guess it.
- **Inline admission before turn return.** A host-owned pre-return durable callback could close more
  loss windows, but the released provider lifecycle does not expose it. Better Hindsight starts its
  guarantee at its own successful admission after released Hermes executes the callback.

These are superseded ideal requirements, not active release gates. The plugin has no Hermes-core
prerequisite, `codex_app_server` is unsupported on the characterized host lifecycle, and the product makes no
pre-callback losslessness claim.

The active dispositions are requirements, not evidence that every planned retention component is
already implemented. Each needs focused contract and integration-shaped proof before release.
[Compatibility](compatibility.md) remains the version/source evidence baseline.
