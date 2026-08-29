# Security

## Supported versions

The `main` branch and the latest optional tagged snapshot are the supported development line.

Better Hermes Hindsight is an unofficial personal plugin, not a hardened multi-tenant service.

## Reporting

Do not open a public issue containing credentials, private endpoints, principal identifiers, bank names,
memories, transcripts, databases, or logs. Use GitHub's private
**[Report a vulnerability](https://github.com/stepanov1975/better-hermes-hindsight/security/advisories/new)**
flow with a minimal synthetic reproducer.

If a real credential is committed, rotate it first. Remove it from Git history afterward when needed;
history rewriting is not a substitute for rotation.

## Deployment boundary

The supported deployment is a trusted Linux Hermes installation with one configured principal and an external Hindsight 0.8.5, 0.9.1, or 0.9.2 service. The configured Hindsight credential may have broader server permissions than the plugin exposes; `single_principal` is plugin policy, not server-side authorization.

Install Better through the standard Hermes plugin lifecycle and use the least-privileged Hindsight credential available. Keep secrets in environment variables, never in repository configuration. Use HTTPS when the service crosses an untrusted network; plaintext HTTP is appropriate only on an explicitly trusted network.

## Preserved controls

- bounded, fail-open recall;
- untrusted-history framing and high-confidence secret redaction;
- opt-in automatic retention;
- destination-bound durable outbox rows;
- model-facing writes only through the same opt-in, redacted, destination-bound local admission path;
- compact passive model-facing queue status with operator-only diagnostics and replay;
- no model-facing reflection, mission/configuration changes, or bank/policy overrides;
- confirmation-gated mission writes with readback;
- no automatic replay to another destination or remote deletion.

These controls reduce realistic risk but do not make the plugin a complete secret-detection, tenant-isolation, or exactly-once system.
