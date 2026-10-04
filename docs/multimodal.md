# Multimodal durable memory (Hindsight 0.10.2 only)

This is explicit agent-selected memory, not automatic upload or transcript attachment retention.
All new capabilities default off. Text-only retention and recall keep their existing contracts on
older supported Hindsight versions. Binary delivery and opt-in attachment-aware reads require an
exact `/version` response of `0.10.2`; unreviewed patch versions are refused, with no caption-only
fallback. Configure the server with `HINDSIGHT_API_TOKENIZER_ENCODING=cl100k_base` before startup.

## Server requirements (exact 0.10.2)

Before enabling binary admission, configure the external server's attachment-reading model.
It must be recognized as vision-capable: by default this is the retain model, or set
`HINDSIGHT_API_VLM_MODEL` for the separate vision slot used only by attachment-bearing chunks.
A text-only retain model can remain in use for other chunks. When a genuinely vision-capable model
is behind a gateway whose catalogue Hindsight cannot recognize, set
`HINDSIGHT_API_LLM_VISION=true` on the server. This tri-state override bypasses recognition; it
does not add vision capability to an incapable model. With no override, false or unknown vision
support causes inline attachment retention to fail with HTTP 422 rather than silently dropping bytes.

Keep `HINDSIGHT_API_RETAIN_BATCH_ENABLED=false` (the server default). Batch retain cannot carry
inline attachments and also fails with HTTP 422, even though Better requests synchronous retention.
These are server settings, not Better configuration keys. Verify model/data/cost exposure and these
gates before admission: `queued_locally` only proves local durability and the sender cannot repair a
permanent server configuration rejection by retrying.

Verified against the peeled `v0.10.2` commit
[`5fc4ce20917b916240cef27c212c387a177f115b`](https://github.com/vectorize-io/hindsight/tree/5fc4ce20917b916240cef27c212c387a177f115b):
[retain requirements](https://github.com/vectorize-io/hindsight/blob/5fc4ce20917b916240cef27c212c387a177f115b/skills/hindsight-docs/references/developer/retain.md#requirements),
[server configuration](https://github.com/vectorize-io/hindsight/blob/5fc4ce20917b916240cef27c212c387a177f115b/hindsight-api-slim/hindsight_api/config.py), and
[attachment model/batch validation](https://github.com/vectorize-io/hindsight/blob/5fc4ce20917b916240cef27c212c387a177f115b/hindsight-api-slim/hindsight_api/engine/memory_engine.py).
No current-`main` behavior is assumed.

## Enable deliberately

Merge into the existing profile-local `$HERMES_HOME/better_hindsight/config.json`, replacing the
synthetic root with a narrow absolute directory you intend to expose. Restart that profile's process.
Never use `/` or a directory containing credentials as an attachment root.

```json
{
  "retain": {"enabled": true},
  "multimodal": {
    "enabled": true,
    "allowed_roots": ["/srv/synthetic-memory-inputs"],
    "max_attachments": 4,
    "max_decoded_bytes": 8388608,
    "max_encoded_bytes": 12582912
  },
  "recall": {"include_attachments": true},
  "reflect": {"enabled": true, "include_attachments": true}
}
```

`retain.enabled` and `multimodal.enabled` are separate admission gates. Recall and reflection
provenance opt-ins do not require local binary retention. Reflection still requires its own enable
flag and invokes the configured Hindsight model. The existing principal/bank/tag policy applies;
callers cannot choose another destination or bypass operator limits.

| Policy | Default | Accepted range |
| --- | --- | --- |
| `multimodal.enabled` | `false` | Boolean |
| `multimodal.allowed_roots` | `[]` | Up to 16 absolute narrow roots; at least one when enabled |
| `multimodal.max_attachments` | 4 | 1–16 per admission |
| `multimodal.max_decoded_bytes` | 8388608 | 1–16777216, total binary bytes per admission |
| `multimodal.max_encoded_bytes` | 12582912 | 1–25165824, complete persisted UTF-8 envelope |
| `recall.include_attachments` | `false` | Boolean |
| `reflect.include_attachments` | `false` | Boolean |

The encoded limit includes base64 expansion, caption, context, filenames, hashes and metadata.
The existing outbox aggregate row/byte limits additionally apply to all queued text and binary rows;
a locally valid snapshot can still be rejected for queue capacity. There is no sidecar blob store.

## Explicit model tool

```json
{
  "content": "Synthetic recovery instructions, with an illustrative screenshot and guide.",
  "context": "synthetic operational convention",
  "attachments": [
    {"path": "/srv/synthetic-memory-inputs/screenshot.png", "kind": "image", "media_type": "image/png"},
    {"path": "/srv/synthetic-memory-inputs/guide.pdf", "kind": "file", "media_type": "application/pdf"}
  ]
}
```

Pass this to `better_hindsight_retain`. `content` remains required (1–8192 characters), and optional
`context` is 1–256 characters. Each attachment accepts exactly `path`, `kind`, `media_type`:
images support PNG, JPEG, WebP and GIF; files support PDF and plain text. The operator/model supplies
MIME; Better does not prove file format or guarantee the backend model can interpret it.

Only absolute local regular nonempty files within an allowed root are admitted. Traversal, symlink
components, directories, FIFOs, URLs, unsupported MIME/kind combinations and host-blocked reads are
refused. Better invokes Hermes's file-read safety policy and uses descriptor-based `NOFOLLOW` reads
with count/size/change checks. This is a trusted local single-operator deployment, not a sandbox
against an adversarial local writer. No URL download is added.

Caption/context and filename credential patterns are redacted, but **binary bytes cannot be
text-redacted**. They are stored in the private SQLite outbox and sent to Hindsight's configured
model/provider. Do not submit secrets, private documents without authorization, or unsupported data.
The payload carries only a safe basename for file blocks, not the original absolute source path;
user-written caption/context may themselves contain paths. Status and telemetry carry no attachment
bytes or admission paths. Protect the outbox and its WAL/backups as sensitive data.

## Durability and replay

Admission reads files once and commits one immutable `better-hindsight-multimodal-v1` envelope or
nothing. The persisted envelope contains ordered text/image/file blocks, canonical base64, original
byte hashes, one fixed UTC timestamp and a credential-free destination/policy fingerprint. It uses
a distinct stable document ID and the existing outbox/sender, without changing text v1/v2 schemas.

`queued_locally` means **local durable admission**, not delivery or successful interpretation.
Sender retries and process restarts use the committed bytes, ID, context and timestamp even if source
files change or disappear. Delivery uses synchronous Hindsight retention and `update_mode="replace"`.
A commit followed by response loss can cause the same remote document to be replaced: there is no
exactly-once transport guarantee. A fresh repeated tool call builds a new timestamped snapshot; it
is not deduplicated as an equivalent semantic request. Duplicate admission of the *same immutable
row* is idempotent while queued.

Changing endpoint, bank, retain tags/scopes, binary roots/limits or disabling retention/multimodal
blocks those queued binary rows. Passive status reports destination mismatch, and the sender does
not silently replay them under a different policy. Existing text fingerprints remain unchanged.
Restore the exact authorized configuration or perform separately reviewed operator recovery; this
feature does not migrate, delete or rewrite mismatched rows. Unsupported servers likewise never
receive a caption-only replacement.

## Recall and reflection evidence

When opted in, recalled facts (including observations and requested source facts) can expose at most
eight validated attachment descriptors each: `id`, `hash`, `kind`, `media_type`, `byte_size`, safe
optional `filename`, and exact current-bank relative `url`. These are fact provenance, not the union
of every attachment found in a retrieved chunk. Invalid optional descriptors are dropped independently
without discarding otherwise valid memory text. Missing metadata remains normal text-only evidence.
Distinct attachment sets are not deduplicated merely because captions match.

Reflection requests `include.facts` only when provenance is enabled; tool-call traces are never
requested. It projects at most eight handles from returned `based_on.memories`, not full source facts,
directives, usage or traces. Both surfaces retain redaction, complete JSON/evidence framing and their
configured serialized output limits. Descriptors are not automatically downloaded or rendered.

The bank-relative download path needs the same authenticated bank access as Hindsight memory; it is
**not a public download URL**. Better adds no arbitrary attachment-fetch tool, credential-bearing URL
or unauthenticated download. Returned history and generated reflection remain untrusted evidence.

## Verification scope

Deterministic tests exercise the real provider/runtime with fake HTTP responses, local admission
without network work, atomic validation, encoded queue capacity, provenance versus chunk metadata,
and a loopback HTTP process-kill/response-loss replay with byte-identical original image/file blocks.
The isolated live harness conditionally extends its exact 0.10.2 disposable synthetic bank: deterministic
chunk extraction, provider admission/restart after source mutation/deletion, authenticated byte
readback, document/chunk provenance, and provider recall/reflection. Older live lanes stay text-only.
This path supports the CI mock LLM and does **not** establish vision semantic quality. Run it only with
the existing explicit live-write/endpoint gates in [compatibility](compatibility.md), never production.
Local deterministic passes are not proof that the real-service live gate has run.
