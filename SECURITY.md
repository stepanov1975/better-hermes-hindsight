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
- default-off reflection with fixed bank/principal/policy, bounded query/network/text/tool output, and
  the same untrusted-history framing and redaction;
- opt-in automatic retention;
- destination-bound durable outbox rows;
- model-facing writes only through the same opt-in, redacted, destination-bound local admission path;
- compact passive model-facing queue status with operator-only diagnostics and replay;
- no caller-selected reflection controls, mission/configuration changes, or bank/policy overrides;
- confirmation-gated mission writes with readback;
- no automatic replay to another destination or remote deletion.

These controls reduce realistic risk but do not make the plugin a complete secret-detection, tenant-isolation, or exactly-once system.

Reflection is a separate authority and data boundary from recall. It sends the bounded query and
selected bank evidence through Hindsight's configured reflection LLM, which may be a third-party
provider, and returns generated prose shaped by stored content, disposition, and directives. Treat the
result only as untrusted evidence; pattern redaction cannot guarantee that generated prose will not
paraphrase sensitive material. The operation is read-only for bank memory, not side-effect-free: it
incurs model work and may produce Hindsight audit/usage records.

The plugin's timeout, raw-response, decoded-text, and serialized-output caps protect the Hermes call.
They are not a complete server-cost ceiling, and a local timeout does not guarantee backend model
cancellation or refund work/cost already incurred. Configure Hindsight's available reflect iteration,
context, wall-time, and completion-token limits for the deployed version before enabling the tool.
