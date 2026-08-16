# Configuration

Better Hermes Hindsight configuration is explicit, Hermes-home-scoped, and local-external-only. Loading
configuration does not contact Hindsight, create a client, import Hermes, discover a `.env` file, or
search the current working directory.

Recall is enabled by default. Automatic retention is disabled by default and must be enabled only
after fake-service proof and an isolated Hindsight development deployment. Capability is controlled
directly by `recall.enabled` and `retain.enabled`.

## Sources and precedence

`load_config(hermes_home=...)` reads non-secret local settings only from:

```text
$HERMES_HOME/better_hindsight/config.json
```

Values resolve in this order, highest precedence first:

1. explicitly injected non-secret test values;
2. the existing process variables `HINDSIGHT_API_URL`, `HINDSIGHT_API_KEY`, and
   `HINDSIGHT_BANK_ID`;
3. the plugin's local `config.json`; and
4. documented defaults.

`HINDSIGHT_API_KEY` is the only supported API-key source. JSON is rejected if a secret-bearing key
appears at any nesting level. The loader never writes credentials, and typed configuration
representations omit the API key, endpoint, bank ID, outbox path, principal identifiers, and raw
mission text.

## Minimal configuration example

This example uses only synthetic/local values and contains no API key. Retention remains off.

```json
{
  "api_url": "http://localhost:8888",
  "bank_id": "example-test-bank",
  "single_principal": true,
  "allowed_principals": [
    {
      "platform": "example-gateway",
      "identifier_kind": "user_id",
      "identifier": "example-user"
    }
  ],
  "recall": {
    "enabled": true,
    "timeout_seconds": 3.5,
    "input_max_chars": 4096,
    "input_max_tokens": 500,
    "context_max_bytes": 8192
  },
  "retain": {
    "enabled": false,
    "timeout_seconds": 60.0,
    "segment_max_bytes": 65536,
    "observation_scopes": "combined",
    "tags": []
  },
  "missions": {
    "retain_mission": null,
    "observations_mission": null
  },
  "outbox": {
    "path": "better_hindsight/outbox.sqlite3",
    "max_pending_rows": 2000,
    "max_pending_bytes": 134217728,
    "busy_timeout_seconds": 1.0,
    "poll_interval_seconds": 2.0,
    "retry_initial_seconds": 2.0,
    "retry_max_seconds": 300.0
  },
  "diagnostics": {
    "enabled": false,
    "path": "better_hindsight/recall_diagnostics",
    "slow_threshold_seconds": 5.0,
    "max_records": 50,
    "replay_timeout_seconds": 30.0
  }
}
```

Unknown keys, wrong types, unsupported enum values, duplicate principal tuples, non-finite numbers,
and invalid ranges are errors rather than silent fallbacks.

`recall.input_max_chars` is the local pre-tokenization safety bound. `recall.input_max_tokens` is a
separate input-query limit and defaults to the 500-token default used by Hindsight 0.8.5 and 0.9.1.
Better counts with the same `cl100k_base` encoding and treats special-token-looking literals as
ordinary text. Keep this value at or below the server's
`HINDSIGHT_API_RECALL_MAX_QUERY_TOKENS`. The existing `recall.max_tokens` setting controls the
response budget; it does not limit query input.

## Defaults and finite bounds

| Setting | Default | Validation |
| --- | --- | --- |
| `api_url` | `http://localhost:8888` | HTTP/HTTPS, host required, no credentials/query/fragment |
| `bank_id` | `hermes` | Non-empty exact string |
| `single_principal` | `false` | Must be explicitly `true` to authorize CLI or gateway memory |
| `allowed_principals` | `[]` | Exact `(platform, identifier_kind, identifier)` tuples |
| `recall.enabled` | `true` | Boolean |
| `recall.timeout_seconds` | `3.5` | Greater than zero, at most 30 seconds |
| `recall.input_max_chars` | `4096` | 1 through 65,536 characters |
| `recall.input_max_tokens` | `500` | 1 through 1,048,576 `cl100k_base` tokens; must not exceed the Hindsight server's `HINDSIGHT_API_RECALL_MAX_QUERY_TOKENS` |
| `recall.context_max_bytes` | `8192` | 1 through 1,048,576 bytes |
| `retain.enabled` | `false` | Boolean; explicit opt-in only |
| `retain.timeout_seconds` | `60.0` | Greater than zero, at most 300 seconds |
| `retain.segment_max_bytes` | `65536` | 1 through 16,777,216 bytes; plus the code-owned 1,024-byte row allowance it must not exceed `outbox.max_pending_bytes` |
| `retain.observation_scopes` | `null` | `null`, `combined`, `shared`, or `[[]]` |
| `retain.tags` | `[]` | At most 64 unique non-empty tags, each at most 256 characters |
| mission texts | `null` | Distinct optional non-empty `retain_mission` and `observations_mission` fields |
| `outbox.path` | `better_hindsight/outbox.sqlite3` | Must resolve inside `hermes_home` |
| `outbox.max_pending_rows` | `2000` | Integer from 1 through 100,000 |
| `outbox.max_pending_bytes` | `134217728` | Integer from 1 through 1,073,741,824 logical payload bytes (1 GiB) |
| `outbox.busy_timeout_seconds` | `1.0` | Greater than zero, at most 5 seconds |
| `outbox.poll_interval_seconds` | `2.0` | Inclusive 0.1 through 60.0 seconds |
| `outbox.retry_initial_seconds` | `2.0` | Greater than zero, at most 3,600 seconds; must not exceed `outbox.retry_max_seconds` |
| `outbox.retry_max_seconds` | `300.0` | Greater than zero, at most 3,600 seconds |
| `diagnostics.enabled` | `false` | Explicit opt-in; captured files contain the exact projected query |
| `diagnostics.path` | `better_hindsight/recall_diagnostics` | Directory must resolve inside `hermes_home` |
| `diagnostics.slow_threshold_seconds` | `5.0` | Inclusive 0.1 through 30 seconds |
| `diagnostics.max_records` | `50` | Integer from 1 through 500; oldest records are removed after capture |
| `diagnostics.replay_timeout_seconds` | `30.0` | Inclusive 0.1 through 300 seconds |

`diagnostics.enabled` is intentionally off by default because exact replay requires storing the full
projected query and credential-free request parameters. Files are mode 0600 in a mode-0700 profile-local
directory; ordinary logs and list output remain query-free. Capture keeps only slow successful recalls
and failed recalls, and prunes the oldest files to `diagnostics.max_records`.

`max_pending_rows` and `max_pending_bytes` are logical admission limits, not an exact cap on the
SQLite database, indexes, or WAL file. Every unconfirmed `pending` or `sending` row consumes its exact
UTF-8 segment-content bytes plus a code-owned, non-configurable 1,024-byte accounting allowance.
Configuration therefore requires
`retain.segment_max_bytes + 1024 <= outbox.max_pending_bytes`. Operators may lower the limits, but
configuration cannot raise them above the documented finite ceilings.

## Mission operator configuration

`missions.retain_mission` and `missions.observations_mission` are independent optional desired text.
They do nothing during provider initialization, recall, admission, or sender delivery. The explicit
`better_hindsight missions check` command compares each configured value byte-for-byte with the typed
remote bank configuration. `better_hindsight missions apply --confirm` updates only configured fields
that are drifted or remotely missing, sends at most one PATCH, preserves an unconfigured allowlisted
field exactly, and requires fresh exact readback.

Mission commands require `single_principal=true` regardless of recall or retention enablement and use
`retain.timeout_seconds` as one total remote-operation deadline. Mission values, endpoint, bank,
credential, Hermes path, and raw SDK errors never appear in command JSON. Omitting both mission
values makes apply a fixed no-write error; it does not infer or install default policy.

For automatic retention, this deadline is consumed by the sender around one remote synchronous retain
attempt; it is not a user-response deadline. Remote work never runs inside the released `sync_turn()`
callback.

## Destination identity

The API URL is normalized for deterministic destination identity: scheme and host are lowercased,
international hostnames use IDNA form, default ports and trailing slashes are removed, and embedded
credentials are forbidden. The destination fingerprint is SHA-256 over canonical normalized API URL,
bank ID, code-owned payload-schema version, canonical redacted/sorted retain tags, and normalized
observation scopes. The schema version has no operator override. Credentials and retry/timing settings
do not participate, so changing `HINDSIGHT_API_KEY` does not change the fingerprint.

## Recall trust, redaction, and byte budget

The provider contributes one byte-stable system-role policy for the exact
`[BETTER_HINDSIGHT_HISTORICAL_EVIDENCE_BEGIN] ...
[BETTER_HINDSIGHT_HISTORICAL_EVIDENCE_END]` envelope. Every enclosed JSONL record is treated as
stale, untrusted historical evidence: it is evidence to evaluate, not an instruction, role message,
or authority over the current conversation. The provider exposes one model-facing read-only tool,
`better_hindsight_recall`, for focused retrieval when automatic context is insufficient. The tool
uses the same authorized provider handle, query projection, configured Hindsight recall controls,
deadline, redaction, allowlist, complete-record byte budget, and untrusted evidence envelope. Its
sole argument is `query`; callers cannot override bank, tags, types, budget, scores, result size, or
timeout.

Every recalled response text passes through the same deterministic high-confidence redactor before
byte budgeting and JSON serialization. The deliberately narrow patterns cover labeled API-key
assignments, Bearer tokens, Authorization headers, PEM private-key blocks, and HTTP(S) URL userinfo.
If response validation or redaction fails, the provider emits no Better Hindsight context for that
call. Pattern-based redaction reduces common accidental egress but is not a universal secret
detector; credentials and other sensitive text must not be stored in Hindsight in the first place.

`recall.context_max_bytes` counts the complete Better envelope, including its preamble, JSONL record
separators, truncation marker, and suffix. It does not include the outer `<memory-context>` wrapper
added later by Hermes. Initialization remains local and network-free: server-version and
bank checks belong to explicit diagnostics or isolated live-proof setup, not another cold pre-model
request.

## Supported Hindsight recall controls

The following optional fields remain `null` when omitted. The adapter omits them rather than
replacing the supported Hindsight 0.8.5/0.9.1 server defaults:

- `recall.budget`: `low`, `mid`, or `high`;
- `recall.max_tokens`: integer from 1 through 1,048,576;
- `recall.types`: a non-empty unique list of `world`, `experience`, and/or `observation`;
- `recall.tags`: at most 64 unique tags of at most 256 characters each, including an explicit empty
  list where Hindsight semantics need it;
- `recall.tag_mode`: `any`, `all`, `any_strict`, `all_strict`, or `exact`;
- `recall.prefer_observations`: boolean;
- `recall.min_scores`: optional finite non-negative `semantic`, `keyword`, `reranker`, and `final`
  floors; and
- `recall.include_source_facts` plus `recall.max_source_facts_tokens` from 1 through 1,048,576.

Omitted `retain.observation_scopes` likewise remains `null`, preserving the SDK/server default.
`shared` and `[[]]` normalize to the same typed global scope. Bare `[]` is rejected because Hindsight
silently interprets it as `combined`; write `combined` explicitly. `shared` additionally requires
`single_principal=true`.

## Principal authorization

The current provider uses one static bank and credential for one explicitly asserted principal. It has no per-user bank router.

- CLI is authorized only when `single_principal=true`.
- Gateway authorization requires an exact configured platform plus either a `user_id` tuple or a
  separate `user_id_alt` tuple. The two identifier kinds are never interchangeable.
- A missing or unlisted identifier disables recall and retain before any client call.
- `agent_context` is a separate execution-context string. It never establishes identity. An
  authorized non-`primary` context may recall but cannot retain.
- `single_principal=false` disables memory even if a tuple happens to match.

Multiple exact tuples may represent the same asserted principal, but configuration cannot route
different users to different banks.

## Retention construction and local admission

Enabling `retain.enabled` opts into completed-turn callbacks Hermes actually supplies. It
does not establish direct-user provenance. Hermes normally schedules `sync_turn()` on its
background executor after a completed turn. If executor creation fails or submission raises
`RuntimeError` outside shutdown, the host invokes the callback inline; shutdown rejects late work.
That rare fallback can make the caller spend Better's bounded local-admission time before returning,
but it does not provide guaranteed pre-return admission. A callback that is cancelled, never runs, or
is lost before its SQLite transaction commits remains outside the durability guarantee.

The callback ignores the raw `messages` transcript and uses only its direct non-empty user and
assistant text arguments. Before hashing, segmentation, or SQLite admission, both role texts and the
configured low-cardinality tags pass through the same deliberately narrow deterministic redactor
described above. The canonical source is compact deterministic UTF-8 JSON with payload schema
`better-hindsight-turn-v1`, SHA-256 of the raw session identifier, explicit `user` and `assistant`
role records, and sorted configured tags. The raw session identifier is not stored. Segments preserve
Unicode code-point boundaries and concatenate exactly to that canonical source.

Each segment ID starts with `better-hindsight-turn-v1:` followed by lowercase SHA-256 of its final
canonical redacted segment record. That record includes the payload schema, digest of the complete
canonical source, zero-based segment index and total count, and exact segment content. Identical rows
are admission no-ops; an ID collision or immutable-row mismatch rejects the complete turn.

Admission uses one `BEGIN IMMEDIATE` transaction. Capacity is checked against all existing
unconfirmed rows plus every new nonduplicate segment before any insertion, so a completed turn is
inserted in full or not at all. Local durability begins only after that transaction commits. Queue
saturation, bounded SQLite contention, construction failure, runtime finalization, or another local
failure fails open for the conversation and emits only the fixed warning
`Better Hindsight local retention admission was rejected.` The warning contains no turn payload,
session identifier, endpoint, bank, credential, or path.

The Hermes-home-local outbox uses private SQLite schema version 1. A new/version-0 database is initialized
to v1, reopening v1 is idempotent, and unknown nonzero versions are rejected without an invented
legacy migration. On the sender/write path, the configured path is revalidated inside `hermes_home`
when opened, including symlink escapes. The pre-created database is opened existing-only, and its
no-follow device/inode identity is checked immediately after connection but before schema writes and
again after initialization. Newly created outbox directories use mode `0700` on POSIX; the database
and reserved ownership lock file use `0600`. Pre-existing parent-directory modes are not changed, and
an existing file is permission-corrected only after it passes confinement and private-schema
validation; a rejected foreign file is left unchanged.

Operator status is a narrower passive path. It checks that the existing database and any SQLite
sidecars are regular files inside `hermes_home`, rejects incomplete WAL topology, and opens SQLite
with URI `mode=ro`. It uses an immutable read when no sidecars exist and a WAL-aware read when an
active WAL/SHM pair exists. Status never creates or migrates a schema, performs application-owned
queue recovery, or writes outbox rows. SQLite may update existing SHM coordination state while
reading active WAL content. Status assumes the intended personal Linux/POSIX deployment has one
trusted local operator and a stable outbox pathname and sidecar topology during the short inspection;
concurrent pathname/topology replacement is outside that operational model.

Sender delivery is implemented after local admission. A retain-enabled process starts one daemon
sender, and a Hermes-home-wide POSIX advisory lock elects the sole process allowed to recover, claim,
complete, or reschedule rows. Bounded cross-process polling lets that owner discover rows admitted by
another process. Only rows matching the current destination fingerprint and payload schema are
claimed; mismatched rows remain durable and unconfirmed.

The sender deletes a row only after strict typed confirmation of synchronous replace-mode retention.
Timeouts, fixed remote failures, and well-formed non-confirming responses remain retryable as
`retain_timeout`, `retain_failed`, and `retain_unconfirmed`. Retry uses the stable document ID and
exact content with deterministic capped exponential backoff. Commit-then-timeout may repeat a remote
request, so this is replace-safe best effort, not exactly-once transport or a zero-loss guarantee.

## Mission behavior

`retain_mission` and `observations_mission` are independent optional texts. Loading and initialization
do not check or apply them. Operators can compare configured and remote values with
`hermes better_hindsight missions check`; applying drift requires the explicit
`hermes better_hindsight missions apply --confirm` command. The model-facing recall tool cannot
invoke either operation, and no retain, reflect, mission, or configuration tool is exposed.

Development writes require an isolated Hindsight instance and synthetic bank. Production canary
checks likewise use synthetic content and an explicitly designated bank.
