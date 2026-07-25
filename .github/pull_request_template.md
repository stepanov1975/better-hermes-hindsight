## Summary

<!-- What changed, why, and which proof gate or issue does it address? -->

## Root cause

<!-- Describe the underlying cause, not only the visible symptom. -->

## Verification

- [ ] `uv lock --check`
- [ ] `uv run --extra dev --extra proof python -m pytest`
- [ ] `uv run --extra dev --extra proof python -m ruff check .`
- [ ] `uv run --extra dev --extra proof python -m ruff format --check .`
- [ ] `uv run --extra dev --extra proof python -m mypy`
- [ ] Package build and `twine check`
- [ ] Fresh temporary-profile smoke when plugin behavior changed
- [ ] `git diff --check`

## Safety and compatibility

- [ ] No production endpoint, credential, bank ID, memory, transcript, or database added
- [ ] No active Hermes/Hindsight installation was modified during tests
- [ ] Upstream-derived source attribution is preserved
- [ ] Compatibility and rollback impact are documented
