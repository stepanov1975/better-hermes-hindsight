# Configuration

Better Hermes Hindsight configuration is explicit, profile-scoped, and local-external-only. Loading
configuration does not contact Hindsight, create a client, import Hermes, discover a `.env` file, or
search the current working directory.

Recall is enabled by default. Automatic retention is disabled by default and must be enabled only
after fake-service proof and an isolated Hindsight development deployment. Capability is controlled
directly by `recall.enabled` and `retain.enabled`.

## Sources and precedence

`load_config(hermes_home=...)` reads non-secret profile settings only from:

```text
$HERMES_HOME/better_hindsight/config.json
```

Values resolve in this order, highest precedence first:

1. explicitly injected non-secret test values;
2. the existing process variables `HINDSIGHT_API_URL`, `HINDSIGHT_API_KEY`, and
   `HINDSIGHT_BANK_ID`;
3. profile `config.json`; and
4. documented defaults.

`HINDSIGHT_API_KEY` is the only supported API-key source. JSON is rejected if a secret-bearing key
appears at any nesting level. The loader never writes credentials, and typed configuration
representations omit the API key, endpoint, bank ID, outbox path, principal identifiers, and raw
mission text.

## Minimal profile example

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
    "query_projection": "head_tail",
    "timeout_seconds": 3.5,
    "input_max_chars": 4096,
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
  }
}
```

Unknown keys, wrong types, unsupported enum values, duplicate principal tuples, non-finite numbers,
and invalid ranges are errors rather than silent fallbacks.

## Defaults and finite bounds

| Setting | Default | Validation |
| --- | --- | --- |
| `api_url` | `http://localhost:8888` | HTTP/HTTPS, host required, no credentials/query/fragment |
| `bank_id` | `hermes` | Non-empty exact string |
| `single_principal` | `false` | Must be explicitly `true` to authorize CLI or gateway memory |
| `allowed_principals` | `[]` | Exact `(platform, identifier_kind, identifier)` tuples |
| `recall.enabled` | `true` | Boolean |
| `recall.query_projection` | `head_tail` | Head-plus-tail projection; no full-history mode |
| `recall.timeout_seconds` | `3.5` | Greater than zero, at most 30 seconds |
| `recall.input_max_chars` | `4096` | 1 through 65,536 characters |
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

`max_pending_rows` and `max_pending_bytes` are logical admission limits, not an exact cap on the
SQLite database, indexes, or WAL file. Every unconfirmed `pending` or `sending` row consumes its exact
UTF-8 segment-content bytes plus a code-owned, non-configurable 1,024-byte accounting allowance.
Configuration therefore requires
`retain.segment_max_bytes + 1024 <= outbox.max_pending_bytes`. Operators may lower the limits, but
configuration cannot raise them above the documented finite ceilings.

The retain deadline is consumed by the future sender around one remote synchronous retain attempt;
it is not a user-response deadline. Remote work never runs inside the released `sync_turn()` callback.

The API URL is normalized for deterministic destination identity: scheme and host are lowercased,
international hostnames use IDNA form, default ports and trailing slashes are removed, and embedded
credentials are forbidden. The destination fingerprint is SHA-256 over canonical normalized API URL,
bank ID, and the code-owned payload-schema version only. The schema version has no operator override.
Changing `HINDSIGHT_API_KEY` does not change the fingerprint.

## Recall trust, redaction, and byte budget

The provider contributes one byte-stable system-role policy for the exact
`[BETTER_HINDSIGHT_HISTORICAL_EVIDENCE_BEGIN] ...
[BETTER_HINDSIGHT_HISTORICAL_EVIDENCE_END]` envelope. Every enclosed JSONL record is treated as
stale, untrusted historical evidence: it is evidence to evaluate, not an instruction, role message,
or authority over the current conversation. The provider exposes no model-facing memory tool.

Every recalled response text passes through the same deterministic high-confidence redactor before
byte budgeting and JSON serialization. The deliberately narrow patterns cover labeled API-key
assignments, Bearer tokens, Authorization headers, PEM private-key blocks, and HTTP(S) URL userinfo.
If response validation or redaction fails, the provider emits no Better Hindsight context for that
call. Pattern-based redaction reduces common accidental egress but is not a universal secret
detector; credentials and other sensitive text must not be stored in Hindsight in the first place.

`recall.context_max_bytes` counts the complete Better envelope, including its preamble, JSONL record
separators, truncation marker, and suffix. It does not include the outer `<memory-context>` wrapper
added later by released Hermes. Initialization remains local and network-free: server-version and
bank checks belong to explicit diagnostics or isolated live-proof setup, not another cold pre-model
request.

## Hindsight 0.8.5 recall controls

The following optional fields remain `null` when omitted. The adapter omits them rather than
replacing Hindsight 0.8.5 SDK/server defaults:

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

The first prerelease has one static bank and credential for one explicitly asserted principal. It
has no per-user bank router.

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

Enabling `retain.enabled` opts into completed-turn callbacks released Hermes actually supplies. It
does not establish direct-user provenance. Released Hermes schedules `sync_turn()` on its background
executor after a completed turn; the provider does not run local admission inline before returning
the answer. A callback that is cancelled, never runs, or is lost before its SQLite transaction commits
remains outside the durability guarantee.

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

The profile-local outbox uses private SQLite schema version 1. A new/version-0 database is initialized
to v1, reopening v1 is idempotent, and unknown nonzero versions are rejected without an invented
legacy migration. The configured path is revalidated inside `hermes_home` when opened, including
symlink escapes. The pre-created database is opened existing-only, and its no-follow device/inode
identity is checked immediately after connection but before schema writes and again after
initialization. Newly created outbox directories use mode `0700` on POSIX; the database and reserved
profile lock file use `0600`. Pre-existing parent-directory modes are not changed, and an existing
file is permission-corrected only after it passes confinement and private-schema validation; a
rejected foreign file is left unchanged.

Task 2 stops at local admission: it starts no sender, acquires no sender lock, performs no Hindsight
retain request, and does not delete confirmed rows. Admitted rows therefore remain local pending data
until the separately implemented sender phase. Deterministic IDs support idempotent processing but do
not create an exactly-once or zero-loss guarantee.

## Mission behavior

`retain_mission` and `observations_mission` are independent optional texts. Loading and initialization
do not check or apply them. Mission check/apply is explicit future Task 4 operator behavior, with
confirmation required for apply. No model-facing memory tools in the first prerelease can invoke it.

Development writes require an isolated Hindsight instance and Hermes profile. Production uses a
separate canary instance and bank while the old deployment remains untouched for rollback.
