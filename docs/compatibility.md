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

The relevant public host contract is Hermes's `MemoryProvider`/`MemoryManager` lifecycle: provider discovery, `is_available()`, `initialize()`, current-query prefetch, `recall_status()`, `sync_turn()`, session switching, shutdown, and plugin CLI registration. Tests exercise these behaviors through the real installed host where practical.

## Hindsight compatibility

Better intentionally targets the exact external Hindsight 0.8.5 and 0.9.1 HTTP contracts. It implements only recall, synchronous retain, bank-config read, and bank-config patch over `aiohttp`; it does not import or depend on the Hindsight Python SDK. Other Hindsight versions are unsupported until their used operations are reviewed and the isolated live proof passes.

Hindsight 0.9.1 adds optional `source_facts_truncated` to recall responses and optional
`operation_id` to retain requests. Better ignores the additive response field and continues to omit
the optional request field. Version 0.2.2 was validated against the official Hindsight 0.9.1 image
with real retain, outbox restart recovery, recall, stable replay, and disposable-bank cleanup.

Both supported Hindsight versions validate recall query length with `tiktoken`'s `cl100k_base`
encoding and treat special-token literals as ordinary text. Their default
`HINDSIGHT_API_RECALL_MAX_QUERY_TOKENS` is 500. Better applies the same count locally through the
explicit `recall.input_max_tokens` setting before sending a request.

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
the runtime and later profiles have Better recall and retention disabled with a sanitized warning.

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
profile per process, one external Hindsight 0.8.5 or 0.9.1 service, and the normal Hermes
memory-provider execution path. Other platforms and runtimes are best effort and do not block use in
the intended environment.
