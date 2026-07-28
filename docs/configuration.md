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
| `retain.segment_max_bytes` | `65536` | 1 through 16,777,216 bytes and must not exceed `outbox.max_pending_bytes` |
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
SQLite database, indexes, or WAL file. Operators may lower them, but configuration cannot raise them
above the documented finite ceilings.

The retain deadline is consumed by the future sender around one remote synchronous retain attempt;
it is not a user-response deadline. Remote work never runs inside the released `sync_turn()` callback.

The API URL is normalized for deterministic destination identity: scheme and host are lowercased,
international hostnames use IDNA form, default ports and trailing slashes are removed, and embedded
credentials are forbidden. The destination fingerprint is SHA-256 over canonical normalized API URL,
bank ID, and the code-owned payload-schema version only. The schema version has no operator override.
Changing `HINDSIGHT_API_KEY` does not change the fingerprint.

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

## Retention and mission behavior

Enabling `retain.enabled` opts into completed-turn callbacks released Hermes actually supplies. It
does not establish direct-user provenance. Local durability starts only after the future provider
admission commits; there is no pre-return or no-loss guarantee.

`retain_mission` and `observations_mission` are independent optional texts. Loading and initialization
do not check or apply them. Mission check/apply is explicit future Task 4 operator behavior, with
confirmation required for apply. No model-facing memory tools in the first prerelease can invoke it.

Development writes require an isolated Hindsight instance and Hermes profile. Production uses a
separate canary instance and bank while the old deployment remains untouched for rollback.
