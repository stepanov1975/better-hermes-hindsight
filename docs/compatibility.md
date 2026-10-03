# Compatibility

Better Hermes Hindsight follows the Hermes checkout used by its maintainer rather than promising a permanent release matrix.

## Current policy

- Better declares Python `>=3.11,<3.15`. Current Hermes requires Python 3.14 for its core
  runtime dependencies; its broader bootstrap packaging bound does not make 3.13 a working runtime.
- Required Linux CI tests Python 3.11, 3.12, and 3.13 against one reviewed Hermes source commit.
- A weekly/manual Python 3.14 canary follows Hermes `main`; a successful canary supports an intentional
  update of the required commit rather than making pull-request results depend on a moving upstream.
- Validation records the observed Hermes package version and Git commit when available.
- A different commit is not rejected solely because its identity changed.
- Compatibility fails only when required provider/CLI interfaces are missing or behavior tests fail.
- Historical Hermes versions are not blocking CI lanes.

### 0.7.1 current-host validation

The local Linux suite on Python **3.14.6**, using an unmodified editable Hermes checkout at
`234badf4012af380d23c91eae55d045a69c69ffb` (distribution version `0.0.0`), passed **1422 tests**
with **1 explicitly gated live test skipped**. This exercises the real host's plugin install,
setup, discovery, operator CLI, and first-turn recall against fake services. Current Hermes needs
interactive dependency consent for install/enable; non-TTY `--enable` alone can leave a plugin disabled.
No Hermes-core or Better runtime implementation patch was needed. Integration fixtures follow the
new tool/client lookup sites and separately verify that host-owned loader workers terminate, while
retaining plugin network, SQLite, sender, and operation-specific thread assertions.

The required pinned-host Python 3.11–3.13 matrix remains at
`080907e3b7ad4985cf4a7c73024a283a49381ba8`; the updated fixtures also passed the full local
Python 3.13.12 suite (**1422 passed, 1 live test skipped**) against that host. Python 3.14 Ruff,
formatting, mypy, dependency consistency, build, Twine, sdist-content, and locked application-runtime
audit checks passed. No runtime package version changed in the expanded lock.
Scheduled/manual current-host and isolated live lanes
now select Python 3.14 explicitly. The older live evidence below is not a live proof for this host;
the new-host 0.9.2/0.10.0/0.10.2 isolated matrix remains a separate gate before release.

The relevant public host contract is Hermes's `MemoryProvider`/`MemoryManager` lifecycle: provider
discovery, `is_available()`, `initialize()`, current-query prefetch, `recall_status()`, `sync_turn()`,
model-tool schema/dispatch, session switching, shutdown, and plugin CLI registration. Tests exercise
these behaviors through the real installed host where practical.

## Hindsight compatibility

Better intentionally targets the exact external Hindsight 0.8.5, 0.9.1, 0.9.2, 0.10.0, and 0.10.2 HTTP
contracts. **0.10.0 and 0.10.2 require server-side `HINDSIGHT_API_TOKENIZER_ENCODING=cl100k_base`**; their
default `o200k_base` mode is unsupported. The isolated proof below remains a release/deployment gate;
a version allowlist or short-query canary alone is not proof of tokenizer compatibility. Better
implements recall, read-only reflection, synchronous retain, bank-config read/patch, and the
separately opt-in [mental-model pilot](mental-models.md) over `aiohttp`; it does not import or depend
on the Hindsight Python SDK. Other Hindsight versions are unsupported until their used operations
are reviewed and the isolated live proof passes.

Mental-model list/read/create/status is restricted to exact Hindsight **0.10.0 or 0.10.2**, checked via
`GET /version` only on explicit authorized pilot calls. It does not change the older supported
recall/retain paths or add version checks to automatic recall. Loopback tests verify the reviewed
0.10.0/0.10.2 wire contract, not live backend LLM generation quality or cost.

These versions expose `POST /v1/default/banks/{bank_id}/reflect` with the narrow fields
Better uses: `query`, `budget`, `max_tokens`, and optional `tags`/`tags_match`. Their response requires
one `text` field. Better omits tag groups, optional source facts, tool traces, response schemas,
contextual policy, mental-model exclusions, and other caller controls. Hindsight 0.9.1 adds optional
`apply_all_directives`; Better deliberately omits it so the request remains compatible with 0.8.5 and
does not expand model authority.

Deterministic fake-service tests pin this shared wire subset. The explicitly enabled isolated
live test proves recall and retention; on 0.10.0/0.10.2 it also requires reflection decoding and
mental-model list/create/status/read/reuse through the provider. Synthetic mock-LLM success is not
real-model quality or cost evidence. A deployment enabling either synthesis capability must prove
its actual isolated LLM configuration; skipped live tests are never backend evidence.

Hindsight 0.9.1 adds optional `source_facts_truncated` to recall responses and optional
`operation_id` to retain requests. Better ignores the additive response field and continues to omit
the optional request field. Version 0.2.2 was validated against the official Hindsight 0.9.1 image
with real retain, outbox restart recovery, recall, stable replay, and disposable-bank cleanup.

Hindsight 0.9.2 adds optional `temporal_window` to recall requests and optional `resolve_entities` to
retain requests. Better does not need either option and continues to omit both. The response fields
Better consumes and the 500-token default query ceiling remain unchanged.

### Hindsight 0.10.0 tokenizer compatibility mode

The reviewed 0.10.0 wire subset remains compatible: retain still accepts strings, synchronous
`async:false`, and `update_mode:"replace"`; recall attachments and reflection's optional
`structured_output_error` are additive fields that Better ignores. Bank-config readback must still
be proven on an existing isolated bank. Removed profile/background operations are not part of
Better's runtime adapter; test infrastructure must not rely on them.

Older supported servers use `cl100k_base`. Hindsight 0.10.0 changes the default vocabulary to
`o200k_base` through `toktok-rs`. Better deliberately retains its packaged, hash-verified
`cl100k_base` table and ordinary treatment of special-token literals; no new runtime dependency,
encoding download, tokenizer selection, or per-query preflight is introduced.

Set this on the **Hindsight server**, before process startup, not just in the Hermes environment:

```text
HINDSIGHT_API_TOKENIZER_ENCODING=cl100k_base
```

The server default `HINDSIGHT_API_RECALL_MAX_QUERY_TOKENS` remains 500. Keep Better's
`recall.input_max_tokens` at or below that independently configured ceiling. A larger ceiling or a
character/token approximation does not restore the matching-tokenizer contract.

The exact synthetic counterexample is `query = " tiktoken" * 250`: Better preserves it at its
500-token limit. The reviewed 0.10.0 tokenizer counts **750** with default `o200k_base`, but **500**
with compatibility `cl100k_base`, so the default REST recall handler rejects it with HTTP 400 at
a 500-token ceiling. `<|endoftext|>` counts as ordinary text (seven tokens) in compatibility mode.
The offline regression pins the counterexample's local count, unchanged projection at the boundary,
and bounded projection above it; it does not pretend to execute a live server.

Source review used upstream release commit
[`5d46f9c8c8eb4fb96f549aa63abe1191b82a7840`](https://github.com/vectorize-io/hindsight/tree/5d46f9c8c8eb4fb96f549aa63abe1191b82a7840),
particularly `hindsight-api-slim/hindsight_api/engine/token_encoding.py`, the recall handler in
`api/http.py`, and `config_resolver.py`. Executing that tokenizer with its locked `toktok-rs==0.1.3`
confirms the two counts; this is tokenizer execution and handler-source evidence, not live HTTP proof.

### Verifying the server policy

The reviewed API cannot attest the tokenizer: `/version` exposes version/features, `/health` reports
health, and bank-config excludes the server-level tokenizer setting. The version allowlist and a
passing short-query canary therefore do **not** certify tokenizer compatibility. Normal recall keeps
its existing fail-open behavior and does not preflight server versions or settings.

At deployment validation, inspect the running server's configuration for the encoding and query
ceiling, then use an isolated bank and the exact boundary query above with a verified **500-token**
server ceiling. Expect a successful recall in compatibility mode and a query-length HTTP 400 in
default mode; an empty result set is acceptable, a swallowed fail-open error is not. An above-limit
raw query (`" tiktoken" * 251`) must still fail with HTTP 400. Success with an unknown or enlarged
ceiling alone cannot distinguish tokenizers. This one-time operator/integration check avoids adding
load and latency to every recall or scheduled canary.

Before declaring a compatible release/deployment, record exact Better and Hermes commits, candidate
image digest and `/version`, server tokenizer/ceiling, boundary-query HTTP outcomes, and isolated
retain → outbox restart/replay → recall, bank-config/mission readback, ownership, and cleanup results.
Prove synthetic reflection separately if enabled. A default-mode negative control and the required
migration/restore rehearsal belong in that evidence; offline or skipped live tests are not substitutes.

The bundled provider can therefore keep Hermes's `hindsight-client==0.6.1` unchanged. Better is
loaded directly from its standard Git-plugin checkout and needs no separate runtime or configuration
isolation.

### Exact Hindsight 0.10.2 source audit

The additional exact target is upstream release commit
[`5fc4ce20917b916240cef27c212c387a177f115b`](https://github.com/vectorize-io/hindsight/tree/5fc4ce20917b916240cef27c212c387a177f115b).
The audit compared the 0.10.0 and 0.10.2 `hindsight-docs/static/openapi.json` used schemas,
`hindsight-api-slim/hindsight_api/api/http.py` handlers, and `engine/memory_engine.py` implementations.
It does not authorize arbitrary 0.10 patch releases, bank aliases, or new model-facing controls.

| Used operation | 0.10.2 finding |
| --- | --- |
| `POST .../memories/recall` | Used request/response fields unchanged. Nonpositive server query ceilings now disable the cap; compatibility proof still requires exactly 500 and a real oversized HTTP 400. |
| `POST .../memories` | Synchronous `async:false`, string content, explicit timestamp, stable document ID and `update_mode:"replace"` remain valid. `MemoryItem.content` now references the equivalent shared `Content` schema. Attachment changes do not require Better to send attachments. |
| `POST .../reflect` | Used fields and required text response preserved. Blank-query validation is stricter (Better already rejects blanks); observation-budget options and evidence/attachment fields are additive and omitted/ignored. |
| `GET/PATCH .../config` | Used handlers and mission read/patch/readback contract unchanged. |
| `GET .../mental-models` and exact-ID read | Explicit metadata/content detail, pagination, bank identity and source question remain valid. Optional `last_refresh_failed_at` is ignored, not treated as proof of freshness or success. |
| `POST .../mental-models` | Same custom-ID/request/queued acknowledgement. Initial content is now empty rather than a generating placeholder. Better still requires status then a content read, never treating existence as generated success. |
| `GET .../operations/{id}?include_payload=true` | Bank-scoped SQL predicate and canonical operation ID/type/task payload remain. Additive `id`, `task_type`, and top-level `mental_model_id` do not replace Better's strict canonical identity checks. |
| Harness bank/document lifecycle | Authenticated bank listing keeps pagination and substring `q`; exact-ID/name ownership, create readback, document text/identity inspection and deletion readback remain required. |

Tokenizer code is unchanged between these releases: `o200k_base` remains the incompatible default,
so **0.10.2 also requires server `HINDSIGHT_API_TOKENIZER_ENCODING=cl100k_base`**. Better's packaged
encoding and 500-token projection remain unchanged; no SDK or dependency bump is needed.

Server-side mental-model generation is **not** behaviorally identical: 0.10.2 defaults refresh to
`mid` iteration budget, separates refresh configuration from ad-hoc reflection defaults, and adds
observation-retrieval options and failed-refresh tracking. Better continues to send its existing
explicit no-auto-refresh/no-trace trigger and omits those new optional controls. Its 1024-token
answer target does not cap backend work. Revalidate synthesis quality/cost with the intended server
configuration before enabling creation. The deterministic suite runs both exact pilot versions,
including empty content, additive fields, ambiguity and refusal of unreviewed versions for every action.

The compatibility candidate passed the isolated gate on both exact images with Hermes
`080907e3b7ad4985cf4a7c73024a283a49381ba8` and Python 3.13: each invocation reported **17 passed**,
including the live smoke test (the other cases check harness safety). The 0.10.2 image index was
`sha256:d1840062a5b79940ab7a9f4809ceb90fc776d4ad737cd9329e9b5836cc64ab70`, resolving to amd64 manifest
`sha256:9f2a0bfc1af6835a8f09f2168689dd50ecacdde2a0b15369a315d46a542a196e`; baseline 0.10.0 used
`sha256:e34028bf84b5bc800029e5d3b13db2c469484dfe5b557b84c577d97fd6af1c74`.

Both used fresh PostgreSQL/pgvector datastores, real local embeddings/reranking, `cl100k_base`,
a 500-token ceiling, and Hindsight's official mock LLM. The proof covered tokenizer HTTP boundaries,
retention/restart/replay, useful automatic and explicit recall, missions, reflection, completed
mental-model generation/read/reuse, and authenticated bank-absence readback. Task containers,
volumes and networks were removed afterward. It exposed and regression-tested the 64-character
mental-model history ID limit shared by both servers; newly generated IDs now fit that limit.
The deterministic suite separately reported **1420 passed, 1 opt-in live skip**.

This is synthetic lifecycle compatibility evidence, not hosted-LLM quality/cost evidence, a
production migration/restore rehearsal, or authorization to deploy. Re-run the isolated gate with
`BETTER_HINDSIGHT_DEV_EXPECTED_VERSION=0.10.2` for subsequent candidates and retain exact-image
results separately; deployment still needs the relevant operational gates above.

## Hermes profile compatibility

Hermes profiles are separate Hermes homes. Better uses the exact `hermes_home` supplied by the host
for `better_hindsight/config.json`, the SQLite outbox, and recall diagnostics. Multiple profiles are
therefore supported when each Better-enabled profile runs in its own CLI or gateway process, which is
Hermes's ordinary per-profile gateway model. Install and select the plugin separately in each profile,
and use a distinct Hindsight bank whenever those profiles require remote memory isolation.

One process owns one exact Better Hindsight configuration and one client/sender runtime. A second
provider handle for the same Hermes home shares that runtime. A handle initialized with another
Hermes home or any other configuration fails open without constructing a second client. Consequently,
a gateway using `gateway.multiplex_profiles: true` may select Better for at most one routed profile.
Selecting Better in several multiplexed profiles is unsupported: the first initialized profile owns
the runtime and later profiles have Better recall, reflection, and retention disabled with a sanitized
warning.

This is a deliberate isolation boundary rather than dynamic bank routing. The provider also reads
`HINDSIGHT_API_KEY` from the process environment, so it cannot select different Hindsight credentials
for profiles multiplexed inside one process.

| Arrangement | Compatibility |
| --- | --- |
| Separate profile CLI/gateway processes | Supported |
| Shared Hindsight service with a distinct bank per profile | Supported |
| Shared bank across profile processes | Operational, but remote memory is combined by design |
| Multiplexed gateway, one Better-enabled profile | Supported |
| Multiplexed gateway, multiple Better-enabled profiles | Unsupported; later profiles fail open |

## Update behavior

When Hermes changes:

1. update or select the intended Hermes checkout and prove it in the compatibility canary;
2. install it into the development interpreter;
3. verify Hermes's installed `aiohttp` satisfies Better's declared range;
4. run the deterministic suite and isolated live smoke test;
5. fix only demonstrated interface or behavior breakage.

CI may follow Hermes `main` and therefore occasionally report an upstream compatibility break. That is useful information, not evidence that every previous Better commit needs a new release declaration.

## Multimodal feature boundary

Text-only support still spans all listed API versions. The separately opted-in multimodal capability
requires **exact 0.10.2** (reviewed source `5fc4ce20917b916240cef27c212c387a177f115b`). Version probing
occurs only on binary sends or attachment-enabled reads; no startup probe or default text-request
change is introduced. An older/unknown server cannot receive a caption-only binary fallback.
`include_attachments` on reflection requests facts and never tool-call traces; recall projects
attachments already present on fact results/source facts, not retrieved chunk unions.

Deterministic coverage includes actual provider/runtime fake-service integration and loopback HTTP
response-loss/process-kill replay of original admitted bytes, document ID and timestamp. The existing
explicit isolated live harness conditionally adds a 0.10.2 synthetic image/plain-file lifecycle:
chunk extraction policy with exact readback, provider restart after source mutation/deletion, stored
attachment byte/hash readback through authenticated routes, document/chunk provenance and provider
recall/reflection. It makes no flattened document-text equality assumption for block inputs. Other
version lanes keep their text-only proof. The mock LLM proves lifecycle/plumbing, not vision semantic
quality or arbitrary document/backend compatibility. A skipped local live gate is not a live pass;
run the current-branch 0.10.2 isolated CI lane before claiming live verification. No production service
or data is authorized by these tests. See [multimodal policy and usage](multimodal.md).

## Supported deployment

The practical target is Linux/POSIX, one configured principal, one static bank, one Better-enabled
profile per process, one external Hindsight 0.8.5, 0.9.1, 0.9.2, 0.10.0, or 0.10.2 service under the tokenizer
policy above, and the normal Hermes memory-provider execution path. Other platforms and runtimes are best effort and do not block use in
the intended environment.
