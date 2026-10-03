# Bounded mental-model pilot

This is a default-off, explicit model-facing capability of the existing Better memory provider.
It uses the configured endpoint, bank, and existing principal authorization. No caller can select
another bank, tags, budget, trigger, policy, or server. It is intended for a small personal bank of
reusable generated summaries, not a general mental-model management API.

## Enable separately

Merge into `$HERMES_HOME/better_hindsight/config.json` (synthetic example):

```json
{
  "mental_models": {
    "enabled": true,
    "create_enabled": true,
    "max_models": 20,
    "timeout_seconds": 10.0
  }
}
```

Defaults are `enabled=false`, `create_enabled=false`, `max_models=20`, and
`timeout_seconds=10.0`. Reads/status require `enabled`; creation additionally requires
`create_enabled`. Creation without reads enabled is a configuration error. `max_models` must be
an integer 1–20; timeout must be finite, greater than zero and at most 30 seconds. These gates are
independent of automatic recall, `reflect.enabled`, and automatic retention.

**Bank-wide only:** enabling this pilot refuses any non-null `recall.tags`, `recall.tag_mode`,
`reflect.tags`, or `reflect.tag_mode`, even explicit empty tag lists or settings on disabled
recall/reflection. Hindsight direct-ID reads do not enforce those tags, and list tag modes are not
equivalent to all supported recall scopes. Do not remove an intended scope just to enable this
pilot. Leave it off until proper scope parity is supported.

The stable schema remains advertised before initialization and when disabled, matching the other
provider tools. Disabled, unauthorized, invalid, or shut-down calls do not perform HTTP requests.
Only explicit authorized pilot calls check `/version`; exact `api_version="0.10.0"` or `"0.10.2"` is required.
Older supported automatic recall/retain behavior is unchanged.

## One tool, explicit actions

`better_hindsight_mental_models` takes these mutually exclusive argument shapes:

```json
{"action":"list","offset":0}
```

```json
{"action":"read","id":"example-model"}
```

```json
{"action":"create","name":"Team preferences","source_query":"What does the team prefer?","reason":"Recurring planning question"}
```

```json
{"action":"status","id":"example-model","operation_id":"550e8400-e29b-41d4-a716-446655440000"}
```

The IDs above are placeholders: use the actual exact model and operation IDs returned by creation.
`offset` defaults to zero and is limited to 0–100000. IDs are 1–128 ASCII alphanumeric, hyphen, or
underscore characters; operation IDs must be canonical UUID strings. `name` is 1–120 characters,
`source_query` 1–2000 characters and at most 500 packaged-cl100k ordinary tokens, and `reason`
1–300 characters. Blank fields, projected/truncated questions, unknown arguments, and policy
fields are refused. `reason` is a local required justification; it is not persisted or sent as
backend content. Name and question use the existing credential-pattern redaction before egress.
The same name/question bounds are checked after redaction. Expansion beyond a character or token
bound is rejected before HTTP or reservation, never silently truncated into a different question.
Pattern redaction is not a universal secret detector: do not put secrets in these arguments.

### List and read

List first and look for a reusable topic. List requests `detail=metadata&limit=20&offset=...`,
returns only allowlisted ID/name/freshness fields, total, limit, offset, and `next_offset` (null at
end). Long names carry `name_truncated=true`. It never fetches all pages automatically.
Escaping/byte-heavy metadata can reduce the
returned page size; `truncated=true` and `next_offset` identify the continuation without dropping
unseen items. Names are capped at 120 characters with their own `name_truncated` flag. Offset
pagination is a snapshot, not a consistent inventory under concurrent external changes.

Read requests the exact ID with `detail=content`, **never the default `full` detail**. It returns
redacted generated content and freshness. `last_refreshed_at` is when generation ran;
`last_memory_seen_at` is the newest matching memory timestamp considered by it. They are not
interchangeable. The server's `is_stale` compares matching memories with that watermark; it is
not computed by comparing the two returned timestamps locally. Unknown/null freshness remains
unknown, never silently converted to fresh.

Complete serialized UTF-8 tool output is capped at **16 KiB**, including the JSON wrapper and
untrusted-evidence envelope. Read truncation is explicitly marked. Each HTTP response has a
**256 KiB** ceiling before decoding; larger or malformed responses fail closed rather than
exposing partial raw data. No raw `reflect_response`, reasoning trace, task payload, backend error
message, or source facts enter model output. Metadata and content are untrusted evidence, not
instructions. Direct read avoids a new reflect request, but the summary can still be stale or wrong.

### Create, status, and verification

Creation is **asynchronous**, not a completed artifact. A `queued` result contains an operation ID
and model ID. Check status once; do not poll in a loop. The status route is bank-scoped; Better
also requires the exact operation ID, `operation_type="refresh_mental_model"`, and the requested
mental-model ID in its task payload. Only ID, operation ID, allowlisted status, and a verification
instruction are returned. Even `completed` is not content verification: read the exact model,
inspect its generated content, and only then report the actual result to the user. Failed or empty
content is not success.

Creation preflight first reads the exact deterministic ID for reuse, checking its bank, ID, and
normalized source question. Otherwise it reads one complete metadata page, verifies the reported
inventory, and enforces the total-bank allowance (including models created elsewhere). The
configurable cap cannot exceed the page size; incomplete or inconsistent inventories refuse the
write. At capacity, exact-ID reuse remains possible. This is not semantic duplicate detection:
different phrasing can produce a different ID, so the agent must list first.

The custom ID is `bh-mm-` plus the first 58 hexadecimal characters of SHA-256 over the configured
endpoint, bank, and normalized redacted question (Unicode NFKC, case folding, collapsed whitespace).
This retains 232 digest bits and fits the 64-character `mental_model_history.mental_model_id`
limit in both supported pilot versions; the model table itself accepts longer IDs, so an overlong
ID can be accepted at creation but fail later when generated content is persisted.
Earlier Better versions generated 70-character IDs using the full digest. Those models remain
listable/readable by their original ID and count toward the bank allowance, but are not automatically
reused by the new bounded-ID create path. List/read first: a new create for the same question can
produce a separate bounded-ID model if capacity permits. No existing model is renamed, deleted,
refreshed, or migrated; failed legacy models require operator-managed recovery. Do not treat their
existence as successful generation.

Retries in the same configured scope
use the same ID regardless of name/reason. Changing endpoint spelling or question wording changes
that identity. A create lock lives on the existing shared runtime, not on each provider instance.
In-process reservations count pending/ambiguous submissions toward the cap until observed in the
bank. POST is never automatically retried. On a timeout or uncertain response, the result includes
the exact ID and instructs reconciliation. The next create call reads that ID first; if it is still
absent but locally reserved, no second POST is sent. Ask the operator if ambiguity persists.
Received HTTP 422 (validation) and 429 (rate-limit) rejections release the local reservation and
return unavailable, allowing a later explicit create call. They are not automatically retried.
Transport failures, 5xx, and malformed acknowledgements keep the reservation because the write
may already have happened. Other statuses remain conservative in this pilot.

This is deliberately **not** a durable job queue. Reservations disappear on process restart, but
the deterministic ID and read-before-write reconciliation remain. There is no atomic cross-process
quota: other processes, operators, deletions, changed destinations, or server writers can race the
inventory. Do not claim a global cost/quota guarantee. A POST may persist the model before queue
submission fails; an existing model can therefore have empty/stale content and no known operation.
Use explicitly enabled refresh with operator reconciliation; never infer generation from existence.

Creation defaults explicitly set `tags=[]`, `max_tokens=1024`, and this trigger:
`mode=full`, `refresh_after_consolidation=false`, `refresh_cron=null`,
`min_refresh_interval_seconds=0`, `fact_types=null`, `exclude_mental_models=true`,
`exclude_mental_model_ids=null`, `tags_match=any`, `tag_groups=null`, `include_chunks=false`,
`recall_max_tokens=4096`, `recall_chunks_max_tokens=0`, `response_schema=null`, `keep_trace=false`.
No model argument can override creation policy. Operators may configure the allowlisted defaults
below or explicitly patch an existing definition. No SDK, local scheduler, startup policy sync,
automatic prefetch, or direct markdown-body editing is included.

**Cost caveat:** the initial create queues server-side LLM work. `max_tokens` is a final-answer
output target, not a hard spend cap. Server policy/directives, retrieval, model reasoning, and
internal calls still determine generation cost. The total deadline covers local version/preflight/
request work; timing out locally does not cancel an already queued backend operation. Evaluate
quality and cost in a disposable synthetic bank before choosing to enable creation in production.

## Verification scope

The wire contract was reviewed against Hindsight 0.10.0 source commit
[`5d46f9c8c8eb4fb96f549aa63abe1191b82a7840`](https://github.com/vectorize-io/hindsight/tree/5d46f9c8c8eb4fb96f549aa63abe1191b82a7840),
particularly `hindsight-api-slim/hindsight_api/api/http.py` and
`hindsight-api-slim/hindsight_api/engine/memory_engine.py`. Exact 0.10.2 support was also reviewed
against [`5fc4ce20917b916240cef27c212c387a177f115b`](https://github.com/vectorize-io/hindsight/tree/5fc4ce20917b916240cef27c212c387a177f115b);
see the [operation-by-operation audit](compatibility.md#exact-hindsight-0102-source-audit).
Its initial model content is empty, and its refresh defaults to `mid` iteration budget with
separate refresh configuration rather than inheriting ad-hoc reflection defaults. On 0.10.2 the payload also explicitly sets `budget=mid`,
`reflect_search_observations_max_tokens=5000`, and
`reflect_search_observations_include_entities=false`. On 0.10.0 these additive fields are omitted;
nondefault creation policy requires 0.10.2. Automatic triggers still default off and traces stay off.
Recheck server-side cost/quality before enabling creation; neither version's output target caps cost.

`tests/integration/test_mental_models.py` drives the real Hermes `MemoryManager`, Better provider,
shared async runtime, and bounded HTTP adapter against a synthetic loopback server. It verifies
gates, exact paths/payloads, lifecycle, queued/status/read flow, freshness, redaction, bounded
output, pagination, concurrency, cap, duplicate reconciliation, failures, and timeout ambiguity.
It performs no production writes and does not claim live backend synthesis quality or cost proof.
Endpoint schema validation runs inside the observed HTTP decoder: malformed version, metadata,
content, status, reconciliation, or creation responses emit `schema_invalid`, not a successful
request event. Existing HTTP counters and watchdog adapter-contract alerts use that outcome.

## Operator maintenance and Knowledge Pages

New operations require exact Hindsight **0.10.2**. Enable `refresh_enabled` for model-facing
refresh and `pages_enabled` for model-facing and operator page access. All flags default false
and require `enabled=true`. Operator summary edit/refresh/delete requires `enabled` and CLI
identity authorization plus exact-target confirmation; `refresh_enabled` gates the model tool,
not an explicitly confirmed operator refresh. Operator creation also requires `create_enabled`.
No bank/principal/tag/budget arguments are added to the model tool. Configured tag scopes remain
refused, including scopes on disabled recall/reflection.

```json
{"mental_models":{"enabled":true,"create_enabled":true,"refresh_enabled":false,
 "pages_enabled":true,"creation":{"mode":"full","budget":"mid",
 "refresh_after_consolidation":false,"refresh_cron":null,
 "min_refresh_interval_seconds":0,"max_tokens":1024,"recall_max_tokens":4096,
 "observations_max_tokens":5000}}}
```

`creation` changes only future creations, never existing models. `mode` is full/delta, `budget`
is low/mid/high. `refresh_cron` is a validated five-field cron (UTC); macros and seconds fields
are refused. Cron and `refresh_after_consolidation=true` are mutually exclusive. Integers must
not be booleans: interval 0–31536000 seconds, output 256–8192 tokens, recall/observations each
1–16384 tokens. Automatic triggers run on the **server**, can incur recurring cost even when
Hermes is stopped, and are not cancelled by local deadlines. The minimum interval is server
refresh policy, not a local throttle or a hard spend guarantee. Delta can fall back to full on
first generation or a changed question. No trace, source schema, tags, chunks, or inclusion
of other mental models can be configured through this surface.

```text
hermes better_hindsight summaries list
hermes better_hindsight summaries create --name "Decisions" --source-query "What are the durable decisions?" --confirm
hermes better_hindsight summaries inspect <model-id>
hermes better_hindsight summaries edit <model-id> --source-query "What decisions changed?" --budget low --max-tokens 512 --confirm <model-id>
hermes better_hindsight summaries edit <model-id> --refresh-cron "0 4 * * *" --min-refresh-interval-seconds 3600 --confirm <model-id>
hermes better_hindsight summaries edit <model-id> --refresh-cron none --confirm <model-id>
hermes better_hindsight summaries edit <model-id> --after-consolidation true --confirm <model-id>
hermes better_hindsight summaries refresh <model-id> --confirm <model-id>
hermes better_hindsight summaries status <model-id> <operation-id>
hermes better_hindsight summaries delete <model-id> --confirm <model-id>
hermes better_hindsight pages create --name "Decisions page" --source-query "What are the durable decisions?" --confirm
hermes better_hindsight pages browse
hermes better_hindsight pages search "decisions"
hermes better_hindsight pages read <page-id>
```

Edit PATCHes the **mental-model definition**, preserving unspecified trigger fields and tags,
then reads the exact target. It does not implicitly refresh or accept body/content editing.
IDs remain unchanged after editing a question; deterministic reuse refuses a mismatched existing
question rather than silently reusing the wrong model. Confirmed delete verifies model absence,
associated page-tree absence, and exact page-resource 404s. It warns of that page cascade and
never calls folder/subtree deletion. Fixed bank-wide retrieval/trace policy is required for
refresh and definition edits; externally scoped or traced models are not silently widened.

Knowledge Pages are dedicated tree resources with backing model IDs, not every standalone
mental model. Page creation always sets the full safe defaults rather than inheriting server
automatic-refresh defaults. `--parent-id` may name an existing folder only. The same bank-wide
model allowance includes backing models; there is no automatic standalone-to-page migration.
Page IDs are `kp-` plus 32 lowercase hexadecimal characters; folder IDs are `kf-` plus 32 hex
characters (not canonical UUID operation IDs). Always use the exact returned identifier.
Browse traverses at most 200 nodes / 12 levels, refusing larger/malformed trees; it may truncate
serialized output at 16 KiB without a continuation. Search is bounded to 10 ranked page hits,
validates the returned-hit count and finite scores, and **excludes standalone models**. Scores
are rank-based, not confidence. Read uses stored `body`, never the rendered placeholder markdown;
`generated_content_present=false` means no generated body. HTTP remains capped at 256 KiB.
All projections are redacted, bounded, untrusted generated evidence, not instructions.

Model tool additions (only these argument shapes):

```json
{"action":"refresh","id":"example-model","reason":"Durable decisions changed"}
{"action":"page_browse"}
{"action":"page_search","query":"deployment decisions"}
{"action":"page_read","id":"kp-550e8400e29b41d4a716446655440010"}
```

Refresh submits once, binds operation status to exact bank/model/task, and returns **queued**,
never generated success. Check status then read and inspect nonempty content. No operation
polling/retry loop exists in tools or CLI. Ambiguous submissions require operator reconciliation;
model-runtime reservations prevent a second uncertain refresh in that process. Operator commands
are finite runtimes: reservations do not survive a subsequent command/process restart. Page
creation uses server-generated IDs, so reconcile the tree after an uncertain result before
creating again. A successful acknowledgement alone is insufficient for edit/delete/create
readback; timeout or mismatch reports unconfirmed rather than claiming success.
A validated refresh/page-create acknowledgement retains its exact operation/model/page IDs in
that bounded unconfirmed response, including subsequent calls in the same runtime, without
another write. Reconcile those IDs with explicit status and read commands; an unconfirmed
acknowledgement does not establish successful generation or verified resource binding.

`test_maintained_summaries.py` exercises real provider/MemoryManager and operator CLI lifecycle
against synthetic loopback HTTP. The opt-in `test_isolated_hindsight.py` additionally runs exact
0.10.2 edit/readback, refresh/status/read, page create/browse/search/read, and delete/absence in
its owned disposable bank. A skipped live gate is not evidence of real backend execution or
LLM quality/cost. No production enablement or writes are part of this implementation.
