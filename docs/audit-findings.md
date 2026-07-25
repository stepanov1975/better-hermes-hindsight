# Sanitized audit findings

A read-only audit on 2026-07-25 found that Hermes's bundled Hindsight integration was
functional but exposed only part of the current Hindsight capability surface. The complete
instance-specific audit remains outside this public-intended repository.

The project requirements derived from that audit are:

1. Preserve current-query first-turn recall and a bounded fail-open path.
2. Accept and forward Hindsight's documented `shared` observation scope.
3. Separate retain, reflect, and observation missions.
4. Filter synthetic/internal turns through structured lifecycle context rather than text
   heuristics where Hermes exposes that context.
5. Support calibrated recall score floors and observation preference.
6. Present recalled material as potentially stale historical evidence.
7. Preserve provenance and operation status for explicit memory tools.
8. Keep FIFO writes, session epochs, final flushing, and rollback observable.
9. Avoid automatic per-turn reflection until its latency and token cost are bounded.
10. Repair ingress and retrieval before considering legacy-bank migration.

These findings are requirements, not proof that the future provider has implemented them.
Each item needs a contract test and an integration-shaped verification before release.
