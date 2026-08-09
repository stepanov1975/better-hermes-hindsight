# Isolated Hindsight development proof

This page defines the operator-owned environment for the opt-in Task 6 proof. It does not provision
Hindsight, Docker, a datastore, an interpreter, or a credential. The repository test creates only a
temporary Hermes home/profile and, after deterministic guards pass, the generated disposable bank it
will use. It never reads or copies an existing Hermes profile or production Hindsight configuration.

The proof is not a production rollout gate by itself. It is one bounded compatibility and usefulness
check for released Hermes 0.19.0 and Hindsight server/client 0.8.5. Repeated-run aggregation, ranking,
release thresholds, migration, reconstruction of an existing bank, pruning, and deletion of existing
data are outside its scope.

## Operator-supplied isolation contract

Before setting any write gate, supply all of the following:

1. A dedicated Hermes installation and interpreter containing released Hermes 0.19.0 at the pinned
   source commit, `hindsight-client==0.8.5`, the reviewed Better wheel, and the proof dependencies.
   No gateway, TUI, CLI, or worker may share this interpreter during the proof. Profile isolation by
   itself is insufficient because the Hindsight SDK version is interpreter-global.
2. A separate Hindsight 0.8.5 deployment with its own datastore. Do not point the proof at the
   existing or production deployment.
3. A separate development API key authorized only for that deployment and held exclusively by this
   proof for the complete run. No other client or writer may use the key or datastore concurrently. Do
   not copy a production key.
4. A generated bank identifier matching `better-hindsight-dev-` followed by 32 lowercase hexadecimal
   characters. The bank must not exist. The harness proves absence through the public bank API before
   its guarded create; an existing bank is a hard no-write failure.
5. An independently prepared destination fingerprint for the exact normalized development endpoint,
   generated bank, fixed proof tags, and the code-owned payload schema.
6. An explicit endpoint decision. Literal loopback addresses and `localhost` are accepted. Every
   other endpoint must be present as an exact normalized URL in the JSON development allowlist.

Hindsight 0.8.5 exposes bank creation as create-or-update and has no conditional create-only primitive.
The exclusive writer/key rule is therefore a safety boundary, not an optimization. The harness takes a
nonblocking host-local writer lock before its first remote operation and keeps the random bank ID and
ownership token out of output. This prevents accidental concurrent local proof runs; it cannot make a
second process that already possesses the same key and target ID safe. Do not run the proof unless that
concurrent writer is excluded operationally.

The harness receives only explicit `BETTER_HINDSIGHT_DEV_*` values. Its child process is built from a
small environment allowlist; inherited `HINDSIGHT_*`, profile, proxy, credential, and unrelated secret
variables do not cross the subprocess boundary. Inside that sanitized disposable process, only the
explicit development API key is mapped to the provider's supported `HINDSIGHT_API_KEY` input.

## Run the bounded proof

First run deterministic tests before loading any development variable. They cover the guards, fake
HTTP mutation ledger, parent-timeout cleanup fallback, and sanitized subprocess contract without
contacting a real service:

```bash
<dedicated-hermes-python> -m pytest \
  tests/integration/test_isolated_hindsight.py -m 'not isolated_hindsight_live'
```

Run the live node only inside a disposable subshell. The reviewed wheel digest must be computed from
the exact wheel before it is installed into the dedicated interpreter; the harness compares that
independently supplied digest with the installed distribution's PEP 610 archive provenance. If the
installer omits an archive hash, the reviewed local wheel must remain at its PEP 610 `file:` URL for
the bounded run so the harness can hash it directly. The harness also verifies that imported Better
modules resolve inside the installed distribution. Values in angle brackets are operator inputs and
must not be committed or pasted into reports:

```bash
(
  set -eu
  cleanup_development_gate() {
    unset BETTER_HINDSIGHT_ALLOW_DEV_WRITES BETTER_HINDSIGHT_REQUIRE_LIVE_PROOF
    unset BETTER_HINDSIGHT_DEV_API_KEY BETTER_HINDSIGHT_DEV_API_URL
    unset BETTER_HINDSIGHT_DEV_BANK_ID BETTER_HINDSIGHT_DEV_DESTINATION_FINGERPRINT
    unset BETTER_HINDSIGHT_DEV_ENDPOINT_ALLOWLIST BETTER_HINDSIGHT_DEV_HERMES_PYTHON
    unset BETTER_HINDSIGHT_DEV_ISOLATION_ACK BETTER_HINDSIGHT_DEV_WHEEL_SHA256
  }
  trap cleanup_development_gate EXIT

  export BETTER_HINDSIGHT_DEV_HERMES_PYTHON='<dedicated-python-path>'
  export BETTER_HINDSIGHT_DEV_API_URL='<isolated-development-api-url>'
  export BETTER_HINDSIGHT_DEV_API_KEY='<isolated-development-api-key>'
  export BETTER_HINDSIGHT_DEV_WHEEL_SHA256='<reviewed-wheel-sha256>'
  bank_suffix="$(
    "$BETTER_HINDSIGHT_DEV_HERMES_PYTHON" -c 'import secrets; print(secrets.token_hex(16))'
  )"
  test -n "$bank_suffix"
  export BETTER_HINDSIGHT_DEV_BANK_ID="better-hindsight-dev-$bank_suffix"
  export BETTER_HINDSIGHT_DEV_ENDPOINT_ALLOWLIST='[]'
  export BETTER_HINDSIGHT_DEV_ISOLATION_ACK='dedicated-interpreter-and-datastore'

  destination_fingerprint="$(
    "$BETTER_HINDSIGHT_DEV_HERMES_PYTHON" -c '
import os
from better_hermes_hindsight.config import derive_destination_fingerprint
print(derive_destination_fingerprint(
    api_url=os.environ["BETTER_HINDSIGHT_DEV_API_URL"],
    bank_id=os.environ["BETTER_HINDSIGHT_DEV_BANK_ID"],
    retain_tags=("kind:isolated-proof", "source:task6"),
))'
  )"
  test -n "$destination_fingerprint"
  export BETTER_HINDSIGHT_DEV_DESTINATION_FINGERPRINT="$destination_fingerprint"

  # Set only after the operator independently confirms this generated bank is absent.
  export BETTER_HINDSIGHT_ALLOW_DEV_WRITES=1
  export BETTER_HINDSIGHT_REQUIRE_LIVE_PROOF=1
  "$BETTER_HINDSIGHT_DEV_HERMES_PYTHON" -m pytest \
    -m isolated_hindsight_live \
    tests/integration/test_isolated_hindsight.py::test_isolated_hindsight_released_host_proof
)
```

For a non-loopback development endpoint, replace `[]` with a JSON array containing only that exact
normalized endpoint. Do not add the production endpoint as a convenience fallback. If any required
variable or the exact write gate is absent, an ordinary suite run skips the live test; the documented
one-shot command sets `BETTER_HINDSIGHT_REQUIRE_LIVE_PROOF=1`, so the same condition fails instead of
producing a false-green skip. A malformed value, non-loopback
endpoint outside the exact allowlist, non-generated bank, fingerprint or wheel mismatch, failed absence
read, or existing bank fails closed before create/upsert. Deterministic fake HTTP tests prove zero
mutations—no mutation requests—on static-guard rejection, failed authenticated absence probes, and
existing-bank rejection. They also cover timeout after the local marker but before create, matching and
mismatched remote ownership witnesses, and the one allowed absent-bank create.

The parent test creates a disposable temporary Hermes profile containing only the reviewed plugin
bridge and synthetic configuration. The sanitized child verifies exact releases, discovers and
selects `better_hindsight` through released Hermes, then performs one fixed proof:

- current-query recall on the first recall call, plus a separate fixed-bound loopback failure that
  returns no context before any model call;
- asynchronous released-Hermes callbacks for fixed synthetic turns, including one above
  `retain.segment_max_bytes`, with local admission held observable before sender drain;
- public document listing, metadata-based long-source reconstruction, and SHA-256 verification;
- one byte-identical callback replay with a positive remote-revision witness, followed by one fresh
  interpreter restart that drains durably pending local work and proves replace-safe convergence;
- one fixed synthetic usefulness/provenance recall check, without aggregation or thresholds;
- mission check/apply/readback with an exact assertion that only the two intended mission fields
  changed; and
- retention disablement followed by a later callback that changes neither the local queue nor public
  remote documents.

After the authenticated absence guard succeeds, the child completely writes, syncs, and rereads a
random cleanup token in the parent's temporary directory before its one create attempt and puts the
matching token in the public bank display name. A stalled, partial, or unverifiable marker fails before
create and is removed when possible. The child never deletes the bank; both success and failure leave
the durable marker for the parent. Public mission-command results `runtime_cleanup_failed` and
`write_attempted_outcome_unknown` fail the proof because the command boundary cannot prove its operator
runtime quiescent. The parent owns the child's POSIX process group and makes the complete interval from
before launch through the descendant-absence proof exception-total. After timeout or normal leader exit,
it detects and terminates surviving descendants. Any interruption or communication failure before the
absence proof either settles the known process tree before propagation or becomes an unsettled-tree
result; an unknown launch outcome with no returned process handle and a failed liveness proof are always
unsettled. Only after proving the full descendant tree absent does the parent perform bounded cleanup.
Cleanup first reads the public profile: HTTP 404 is already a confirmed absence and sends no DELETE,
while a present bank is deleted only when both its ID and remote ownership name match. The parent then
confirms absence and clears the local marker. If tree absence cannot be proven, it performs no cleanup.
After absence is proven, a propagated Python `KeyboardInterrupt` or `SystemExit` still runs
ownership-gated parent cleanup before the interruption is re-raised. Existing-bank
rejection and timeout before create therefore send no mutation, even if the local marker was already
written. The child protocol is one exact JSON line with
an exact success or failure schema; stderr, preceding output, extra fields, invalid types, and sensitive
development values are rejected. Neither process emits a raw endpoint, key, bank, source text,
transcript, profile content, ownership token, or generated identifier. If cleanup fails, the parent
reports only a one-way sanitized `dev-` identifier so the operator can correlate the disposable
resource without disclosing its raw ID. Do not run manual deletion against any other bank.

## Proposed production canary—do not activate here

A future production canary uses another isolated Better Hindsight instance and bank, a dedicated
Hermes interpreter with the exact SDK, and an explicitly selected canary profile. The existing
Hindsight deployment, bundled-provider profile, and bank remain running and untouched as the rollback
source. Canary promotion evaluates only recall usefulness and retained-source quality.

This Task 6 proof does not activate that canary. It performs no initial migration, deduplication,
reconstruction, reconsolidation, pruning, or deletion of existing data. Provisioning, activation,
promotion, publication, and any later data lifecycle action each require separate authorization.
