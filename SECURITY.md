# Security

Better Hermes Hindsight is an unofficial personal plugin, not a hardened multi-tenant service.

## Reporting

Do not open a public issue containing credentials, private endpoints, principal identifiers, bank names, memories, transcripts, databases, or logs. Use a private GitHub security advisory or contact the maintainer privately with a minimal synthetic reproducer.

## Deployment boundary

The supported deployment is a trusted Linux Hermes profile with one configured principal and an external Hindsight 0.8.5 service. The configured Hindsight credential may have broader server permissions than the plugin exposes; `single_principal` is plugin policy, not server-side authorization.

Use a dedicated Hermes profile where operational separation is useful and the least-privileged Hindsight credential available. Keep secrets in environment variables, never in repository configuration. Use HTTPS when the service crosses an untrusted network; plaintext HTTP is appropriate only on an explicitly trusted network.

## Preserved controls

- bounded, fail-open recall;
- untrusted-history framing and high-confidence secret redaction;
- opt-in automatic retention;
- destination-bound durable outbox rows;
- no model-facing retain or reflect tools;
- confirmation-gated mission writes with readback;
- no automatic replay to another destination or remote deletion.

These controls reduce realistic risk but do not make the plugin a complete secret-detection, tenant-isolation, or exactly-once system.
