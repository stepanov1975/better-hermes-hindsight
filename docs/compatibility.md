# Compatibility

Better Hermes Hindsight follows the Hermes checkout used by its maintainer rather than promising a permanent release matrix.

## Current policy

- The maintained development lane uses Python 3.13.
- The provider is tested against the current intended Hermes source checkout.
- Validation records the observed Hermes package version and Git commit when available.
- A different commit is not rejected solely because its identity changed.
- Compatibility fails only when required provider/CLI interfaces are missing or behavior tests fail.
- Historical Hermes versions are not blocking CI lanes.

The relevant public host contract is Hermes's `MemoryProvider`/`MemoryManager` lifecycle: provider discovery, `is_available()`, `initialize()`, current-query prefetch, `sync_turn()`, session switching, shutdown, and plugin CLI registration. Tests exercise these behaviors through the real installed host where practical.

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

## Update behavior

When Hermes changes:

1. update or select the intended Hermes checkout;
2. install it into the development interpreter;
3. verify Hermes's installed `aiohttp` satisfies Better's declared range;
4. run the deterministic suite and isolated live smoke test;
5. fix only demonstrated interface or behavior breakage.

CI may follow Hermes `main` and therefore occasionally report an upstream compatibility break. That is useful information, not evidence that every previous Better commit needs a new release declaration.

## Supported deployment

The practical target is Linux/POSIX, one configured principal, one static bank, one external Hindsight 0.8.5 or 0.9.1 service, and the normal Hermes memory-provider execution path. Other platforms and runtimes are best effort and do not block use in the intended environment.
