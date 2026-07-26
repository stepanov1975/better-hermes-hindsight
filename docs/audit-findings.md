# Sanitized audit findings

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

## Requirements derived from the audit

1. Preserve current-query first-turn recall and a bounded fail-open path.
2. Make recall the only remote or potentially long-running pre-LLM memory operation.
3. Accept and forward Hindsight's documented `shared` observation scope only after principal and
   bank-isolation proof.
4. Separate retain, reflect, and observation missions.
5. Filter synthetic/internal turns through structured lifecycle origin rather than text heuristics;
   unknown origin remains ineligible.
6. Support calibrated recall score floors and observation preference.
7. Present recalled material as potentially stale, untrusted historical evidence.
8. Preserve provenance and truthful operation status for explicit memory tools.
9. Commit eligible redacted turns to a local durable outbox before turn completion, then preserve
   FIFO replay, destination matching, profile-wide sender ownership, and final recovery.
10. Preserve source documents and repair ingress/retrieval before considering any separately
    authorized legacy-bank migration.

These findings are requirements, not proof that the future provider has implemented them. Each
item needs a contract test and integration-shaped verification before release. The exact version,
caller, lifecycle, preservation, and go/no-go baseline is frozen in
[compatibility.md](compatibility.md).
