# Compatibility

Better Hermes Hindsight follows the Hermes checkout used by its maintainer rather than promising a permanent release matrix.

## Current policy

- Required Linux CI tests Python 3.11, 3.12, and 3.13 against one reviewed Hermes source commit.
- A weekly/manual Python 3.13 canary follows Hermes `main`; a successful canary supports an intentional
  update of the required commit rather than making pull-request results depend on a moving upstream.
- Validation records the observed Hermes package version and Git commit when available.
- A different commit is not rejected solely because its identity changed.
- Compatibility fails only when required provider/CLI interfaces are missing or behavior tests fail.
- Historical Hermes versions are not blocking CI lanes.

The relevant public host contract is Hermes's `MemoryProvider`/`MemoryManager` lifecycle: provider
discovery, `is_available()`, `initialize()`, current-query prefetch, `recall_status()`, `sync_turn()`,
model-tool schema/dispatch, session switching, shutdown, and plugin CLI registration. Tests exercise
these behaviors through the real installed host where practical.

## Hindsight compatibility

Better intentionally targets the exact external Hindsight 0.8.5, 0.9.1, 0.9.2, and 0.10.0 HTTP
contracts. **0.10.0 requires server-side `HINDSIGHT_API_TOKENIZER_ENCODING=cl100k_base`**; its
default `o200k_base` mode is unsupported. The isolated proof below remains a release/deployment gate;
a version allowlist or short-query canary alone is not proof of tokenizer compatibility. Better
implements only recall, read-only reflection, synchronous retain, bank-config read, and bank-config
patch over `aiohttp`; it does not import or depend on the Hindsight Python SDK. Other Hindsight
versions are unsupported until their used operations are reviewed and the isolated live proof passes.

These versions expose `POST /v1/default/banks/{bank_id}/reflect` with the narrow fields
Better uses: `query`, `budget`, `max_tokens`, and optional `tags`/`tags_match`. Their response requires
one `text` field. Better omits tag groups, optional source facts, tool traces, response schemas,
contextual policy, mental-model exclusions, and other caller controls. Hindsight 0.9.1 adds optional
`apply_all_directives`; Better deliberately omits it so the request remains compatible with 0.8.5 and
does not expand model authority.

Deterministic fake-service tests pin this shared wire subset. The existing explicitly enabled isolated
live test proves recall and retention behavior, not reflection. A deployment that will enable
reflection must separately prove one synthetic query against its isolated Hindsight LLM configuration;
a skipped or recall-only live test is not evidence that reflection works for that deployment.

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

## Supported deployment

The practical target is Linux/POSIX, one configured principal, one static bank, one Better-enabled
profile per process, one external Hindsight 0.8.5, 0.9.1, 0.9.2, or 0.10.0 service under the tokenizer
policy above, and the normal Hermes memory-provider execution path. Other platforms and runtimes are best effort and do not block use in
the intended environment.
