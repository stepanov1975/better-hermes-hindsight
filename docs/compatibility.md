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

Better intentionally targets external Hindsight server and `hindsight-client==0.8.5`. This remains an exact dependency because the SDK API is materially different from the `0.6.1` client used by bundled Hermes Hindsight.

A Hermes profile does not isolate interpreter packages. Run Better from a dedicated Hermes interpreter/profile when another active profile needs bundled Hindsight's incompatible SDK.

## Update behavior

When Hermes changes:

1. update or select the intended Hermes checkout;
2. install it into the development interpreter;
3. reinstall `hindsight-client==0.8.5` if Hermes dependency resolution replaced it;
4. run the deterministic suite and isolated live smoke test;
5. fix only demonstrated interface or behavior breakage.

CI may follow Hermes `main` and therefore occasionally report an upstream compatibility break. That is useful information, not evidence that every previous Better commit needs a new release declaration.

## Supported deployment

The practical target is Linux/POSIX, one configured principal, one static bank, one external Hindsight 0.8.5 service, and the normal Hermes memory-provider execution path. Other platforms and runtimes are best effort and do not block use in the intended environment.
