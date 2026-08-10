# Security policy

## Supported versions

There are no supported production releases yet. Version `0.1.0a1` is a GitHub development prerelease
and is not supported for production. Proof branches must not be installed in production.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or leaked secret. Use GitHub's
private security advisory mechanism for this repository. If that is unavailable while the
repository is private, contact the repository owner through the GitHub profile associated
with the project without including secret values in the initial message.

Include affected revision, reproduction conditions, expected impact, and a minimally
sensitive proof. Never attach real Hermes configuration, API keys, private memories,
session transcripts, or production database extracts.

## Secret response

If a credential is committed, revoke and rotate it first. Removing it from the latest tree
is not sufficient; inspect and clean Git history before publication, then verify the old
credential is unusable.
