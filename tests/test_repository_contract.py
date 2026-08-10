"""Tests for the active best-effort product and compatibility contracts."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token

ROOT = Path(__file__).resolve().parents[1]
LOCAL_PLAN_PATH = ROOT / ".hermes/plans/2026-07-27_071437-best-effort-plugin.md"
LOCAL_PLAN_INDEX_PATH = ROOT / ".hermes/plans/README.md"
LOCAL_PLAN_INDEX_SHA256 = "609b3d35cd7eeb578687d75fde9a4541d1e17a6c49fe6391d3382b0f832621a7"

_STATUS_COMPATIBILITY_START = b"<!-- better-hindsight-status-compatibility:start -->"
_STATUS_COMPATIBILITY_END = b"<!-- better-hindsight-status-compatibility:end -->"
_STATUS_STORAGE_START = b"<!-- better-hindsight-status-storage:start -->"
_STATUS_STORAGE_END = b"<!-- better-hindsight-status-storage:end -->"

_STATUS_COMPATIBILITY_CONTRACT = b"""\
## Status inspection compatibility

Inspection of an existing outbox requires `os.name == "posix"`, linked SQLite `>=3.22.0`,
Python URI connections, and SQLite's built-in POSIX `unix` VFS selected with `vfs=unix`.
A non-POSIX or older runtime returns fixed `status_unavailable` before `sqlite3.connect()`;
an unavailable `unix` VFS fails selection before the target database is opened. The command
does not support a process-default or custom VFS.
"""

_STATUS_STORAGE_CONTRACT = b"""\
## Status storage contract

- **Active WAL.** When WAL exists, status requires a pre-existing regular SHM file and uses
  SQLite `mode=ro&vfs=unix` with `PRAGMA query_only=ON` and one read transaction. SQLite may
  initialize, recover, resize, or otherwise change contents, size, atime, mtime, and ctime only
  on the same pre-existing regular SHM inode. Its inode, type, link count, mode, UID, GID, and
  xattrs/ACL xattrs remain unchanged.
- **Byte and lock effects.** Status issues no database, WAL, profile-lock, or row-byte writes.
  The point-in-time sender probe may acquire and release a transient kernel `flock` without
  changing lock-file bytes. An authorized writer may change database or WAL bytes and timestamps
  during the read; those external changes are not attributed to status.
- **Sidecar-free snapshot.** When WAL, SHM, and rollback journal are all absent, status uses
  `mode=ro&immutable=1&vfs=unix`, requires the main-file identity/size/mtime/ctime to remain
  unchanged, and requires all three sidecars to remain absent. Missing SHM is not an error in the
  all-sidecars-absent branch.
- **Malformed topology.** If WAL exists but SHM is missing, status fails before SQLite opens and
  creates nothing. A pre-existing rollback journal or SHM without WAL is unavailable. Active WAL
  never uses `immutable=1`.
- **Trusted topology.** Supported concurrency assumes stable file identities and journal mode.
  Observable same-principal races return `status_unavailable` when detected, but raced-path effects
  and undetectable ABA are not prevented; status is not safe against hostile same-UID replacement.
  This is not a zero-mutation claim because SQLite may change the derived SHM as described above.
"""

ACTIVE_CONTRACT_PATHS = (
    "README.md",
    "DESIGN.md",
    "docs/audit-findings.md",
    "docs/compatibility.md",
    "docs/configuration.md",
    "docs/development-instance.md",
    "docs/operations.md",
    "docs/public-release-checklist.md",
)

TASK4_FROZEN_AUTHORITY_PATHS = (
    "IMPLEMENTATION.md",
    "README.md",
    "DESIGN.md",
    "docs/audit-findings.md",
    "docs/compatibility.md",
    "docs/configuration.md",
    "docs/development-instance.md",
    "docs/operations.md",
    "docs/public-release-checklist.md",
    "src/better_hermes_hindsight/config.py",
    "src/better_hermes_hindsight/hermes_plugin/cli.py",
)

_TASK4_FROZEN_AUTHORITY_SHA256 = {
    "IMPLEMENTATION.md": "fc231ed887932256255486ba8d4041b5273460af902a0811de9193e77d5823d5",
    "README.md": "898749a47393c467cf4503c578ed26a200527bbb9f97b555904abe6964cb372c",
    "DESIGN.md": "332295589aacb6fd0e1e61a26fdde8179e30f2c0cf587a45014042b81cf5d27f",
    "docs/audit-findings.md": "6968809d0860ee5418414f74e2cecff74745c0b9972a1a0aec0a67b085859b92",
    "docs/compatibility.md": "5b125f4d546d930664e82aad92a5d71fc475d72ebb6d2c975eb3cc716e676a59",
    "docs/configuration.md": "b60acfb20c468a9bddb5e489a382c6dd676f5fc1fd081af4225d1dc2e9a64380",
    "docs/development-instance.md": (
        "6ee5b3cd960fab6fee52c569c54d7578683c9ba0b6a242578a367f408be87d10"
    ),
    "docs/operations.md": "4635ea77853448e4a32f4b34c94d0fe5bbd82c8bcb4691818f88a9da80269f18",
    "docs/public-release-checklist.md": (
        "e241b9eedaa3a0cb428c341020d8a921cf9442051aeff0763e3218db066d0c2c"
    ),
    "src/better_hermes_hindsight/config.py": (
        "ce310b60359d34c6e2c30fcc46592d43ecc0b2ad36a6731ae87743b21a733621"
    ),
    "src/better_hermes_hindsight/hermes_plugin/cli.py": (
        "dbfbf37a26771d993ea2d66e558a05940922b4d1640150341b98c6d1a96c53d5"
    ),
}


def _read(relative_path: str) -> str:
    path = ROOT / relative_path
    assert path.is_file(), f"required repository contract is missing: {relative_path}"
    content = path.read_bytes()
    assert b"\r" not in content, f"repository contract must use LF only: {relative_path}"
    return content.decode("utf-8", errors="strict")


def _iter_public_source_text_paths(root: Path) -> Iterator[Path]:
    """Yield repository source text while excluding generated and external roots."""
    text_suffixes = {".in", ".md", ".py", ".toml", ".yaml", ".yml"}
    ignored_roots = {".compat", ".git", ".hermes", ".venv", "build", "dist"}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not path.is_file() or not relative.parts:
            continue
        if relative.parts[0] in ignored_roots or path.suffix not in text_suffixes:
            continue
        yield path


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).casefold()


def _assert_terms(text: str, *terms: str) -> None:
    normalized = _normalized(text)
    missing = [term for term in terms if term.casefold() not in normalized]
    assert not missing, f"missing repository contract terms: {missing}"


def _workflow_job(
    name: str, *, workflow: str = ".github/workflows/security.yml"
) -> dict[str, object]:
    document = cast(object, yaml.safe_load(_read(workflow)))
    assert isinstance(document, dict)
    jobs = document.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs.get(name)
    assert isinstance(job, dict)
    return cast(dict[str, object], job)


def _workflow_step(job: dict[str, object], name: str) -> dict[str, object]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    for raw_step in steps:
        assert isinstance(raw_step, dict)
        step = cast(dict[str, object], raw_step)
        if step.get("name") == name:
            return step
    raise AssertionError(f"missing workflow step: {name}")


def _shell_tokens(step: dict[str, object]) -> list[str]:
    command = step.get("run")
    assert isinstance(command, str)
    return shlex.split(command.replace("\\\n", " "))


def _host_audit_validator_script() -> str:
    host_job = _workflow_job("supported-hermes-observation")
    host_audit = _workflow_step(host_job, "Record supported-host dependency observations")
    script = host_audit.get("run")
    assert isinstance(script, str)
    marker = "python - <<'PY'\n"
    assert marker in script
    validator = script.split(marker, maxsplit=1)[1]
    assert validator.endswith("\nPY\n")
    return validator.removesuffix("\nPY\n")


def _assert_terms_in_order(text: str, *terms: str) -> None:
    normalized = _normalized(text)
    cursor = 0
    for term in terms:
        normalized_term = _normalized(term)
        assert normalized.count(normalized_term) == 1, (
            f"repository contract term is absent or duplicated inside its structural scope: {term}"
        )
        position = normalized.find(normalized_term, cursor)
        assert position >= 0, f"missing or out-of-order repository contract term: {term}"
        cursor = position + len(normalized_term)


def _assert_exact_normalized(text: str, expected: str) -> None:
    assert _normalized_scope(text) == _normalized_scope(expected)


_MARKDOWN = MarkdownIt("commonmark").enable("table")
_COMPATIBILITY_RAW_HTML_BLOCKS = frozenset(
    {
        _STATUS_COMPATIBILITY_START.decode("utf-8"),
        _STATUS_COMPATIBILITY_END.decode("utf-8"),
    }
)
_PLAN_ROOT_ANCESTRY = ("# Better Hermes Hindsight Best-Effort Plugin Implementation Plan",)
_PLAN_TASK_ANCESTRY = (*_PLAN_ROOT_ANCESTRY, "## 4. Honest best-effort contracts")
_DESIGN_ROOT_ANCESTRY = ("# Design and proof contract",)
_COMPATIBILITY_ROOT_ANCESTRY = ("# Compatibility and preservation contract",)
_CHECKLIST_ROOT_ANCESTRY = ("# Public release checklist",)
_README_ROOT_ANCESTRY = ("# Better Hermes Hindsight",)
_IMPLEMENTATION_ROOT_ANCESTRY = ("# Implementation source of truth",)
_PLAN_INDEX_ROOT_ANCESTRY = ("# Better Hermes Hindsight plan index",)


def _normalized_scope(text: str) -> str:
    assert "\r" not in text
    return re.sub(r"[ \t\n]+", " ", text).strip()


def _normalized_anchor(text: str) -> str:
    return _normalized_scope(text).casefold()


def _walk_tokens(tokens: list[Token]) -> Iterator[Token]:
    for token in tokens:
        yield token
        if token.children:
            yield from _walk_tokens(token.children)


def _markdown_source(
    text: str,
    *,
    allowed_raw_html_blocks: frozenset[str] = frozenset(),
) -> tuple[list[str], list[Token]]:
    assert "\r" not in text
    lines = text.split("\n")
    tokens = _MARKDOWN.parse(text)
    assert all(token.type != "html_inline" for token in _walk_tokens(tokens)), (
        "inline HTML is forbidden in ordered Markdown contract sources"
    )

    raw_html_sources: list[str] = []
    for token in tokens:
        if token.type != "html_block":
            continue
        assert token.map is not None
        start, end = token.map
        assert end == start + 1
        source = "\n".join(lines[start:end])
        assert token.content == f"{source}\n"
        raw_html_sources.append(source)
    assert len(raw_html_sources) == len(allowed_raw_html_blocks)
    assert frozenset(raw_html_sources) == allowed_raw_html_blocks, (
        "raw HTML is forbidden outside its exact ordered-contract owner"
    )
    return lines, tokens


def _source_slice(lines: list[str], source_map: list[int] | None) -> str:
    assert source_map is not None
    start, end = source_map
    return "\n".join(lines[start:end])


def _heading_source_line(lines: list[str], token: Token) -> str:
    assert token.type == "heading_open" and token.level == 0
    assert token.map is not None
    start, end = token.map
    assert end == start + 1
    return lines[start]


def _heading_ancestry(
    lines: list[str],
    tokens: list[Token],
    before_index: int,
    *,
    entering_level: int | None = None,
) -> tuple[str, ...]:
    stack: list[tuple[int, int]] = []
    for index, token in enumerate(tokens[:before_index]):
        if token.type != "heading_open" or token.level != 0:
            continue
        heading_level = int(token.tag[1:])
        while stack and stack[-1][0] >= heading_level:
            stack.pop()
        stack.append((heading_level, index))
    if entering_level is not None:
        while stack and stack[-1][0] >= entering_level:
            stack.pop()
    return tuple(_heading_source_line(lines, tokens[index]) for _, index in stack)


def _closing_token_index(tokens: list[Token], opening_index: int) -> int:
    opening = tokens[opening_index]
    assert opening.type.endswith("_open")
    closing_type = f"{opening.type.removesuffix('_open')}_close"
    for index in range(opening_index + 1, len(tokens)):
        token = tokens[index]
        if token.type == closing_type and token.level == opening.level:
            return index
    raise AssertionError(f"unclosed Markdown token: {opening.type}")


def _plain_text(tokens: list[Token], start: int, end: int, *, level: int | None = None) -> str:
    parts: list[str] = []
    for token in tokens[start:end]:
        if token.type != "inline" or (level is not None and token.level != level):
            continue
        for child in token.children or []:
            if child.type == "text":
                parts.append(child.content)
            elif child.type in {"softbreak", "hardbreak"}:
                parts.append("\n")
    return "".join(parts)


def _mapped_heading_at(
    text: str,
    *,
    ordinal: int,
    tag: str,
    title: str,
    ancestry: tuple[str, ...],
    allowed_raw_html_blocks: frozenset[str] = frozenset(),
) -> tuple[list[str], list[Token], int]:
    lines, tokens = _markdown_source(
        text,
        allowed_raw_html_blocks=allowed_raw_html_blocks,
    )
    headings = [
        index
        for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.level == 0
    ]
    assert 0 <= ordinal < len(headings)
    selected_index = headings[ordinal]
    selected = tokens[selected_index]
    assert selected.tag == tag
    assert _heading_source_line(lines, selected) == f"{'#' * int(tag[1:])} {title}"
    assert (
        _heading_ancestry(
            lines,
            tokens,
            selected_index,
            entering_level=int(tag[1:]),
        )
        == ancestry
    )
    return lines, tokens, selected_index


def _mapped_root_block_at(
    text: str,
    *,
    ordinal: int,
    opening_type: str,
    expected_text: str,
) -> tuple[list[str], list[Token], int]:
    lines, tokens = _markdown_source(text)
    roots = [
        index
        for index, token in enumerate(tokens)
        if token.level == 0 and token.block and token.nesting >= 0 and token.type != "inline"
    ]
    assert 0 <= ordinal < len(roots)
    selected_index = roots[ordinal]
    selected = tokens[selected_index]
    assert selected.type == opening_type
    assert selected.nesting == 1
    assert _heading_ancestry(lines, tokens, selected_index) == (), (
        "ordered contract owner must be a direct child of its selected section"
    )
    closing_index = _closing_token_index(tokens, selected_index)
    visible_text = _plain_text(tokens, selected_index + 1, closing_index)
    assert _normalized_anchor(expected_text) in _normalized_anchor(visible_text)
    return lines, tokens, selected_index


def _extract_heading_section_at(
    text: str,
    *,
    tag: str,
    ordinal: int,
    title: str,
    ancestry: tuple[str, ...],
    allowed_raw_html_blocks: frozenset[str] = frozenset(),
) -> str:
    lines, tokens, index = _mapped_heading_at(
        text,
        ordinal=ordinal,
        tag=tag,
        title=title,
        ancestry=ancestry,
        allowed_raw_html_blocks=allowed_raw_html_blocks,
    )
    heading = tokens[index]
    assert heading.map is not None
    heading_level = int(tag[1:])
    end_line = len(lines)
    for token in tokens[index + 1 :]:
        if token.type != "heading_open" or token.level != 0:
            continue
        if int(token.tag[1:]) <= heading_level:
            assert token.map is not None
            end_line = token.map[0]
            break
    return "\n".join(lines[heading.map[1] : end_line])


def _extract_between_paragraphs_at(
    text: str,
    *,
    start_ordinal: int,
    start_text: str,
    start_source: str,
    end_ordinal: int,
    end_text: str,
    end_source: str,
) -> str:
    start_lines, start_tokens, start_index = _mapped_root_block_at(
        text,
        ordinal=start_ordinal,
        opening_type="paragraph_open",
        expected_text=start_text,
    )
    end_lines, end_tokens, end_index = _mapped_root_block_at(
        text,
        ordinal=end_ordinal,
        opening_type="paragraph_open",
        expected_text=end_text,
    )
    assert start_lines == end_lines
    start = start_tokens[start_index]
    end = end_tokens[end_index]
    assert start.map is not None and end.map is not None
    _assert_exact_normalized(_source_slice(start_lines, start.map), start_source)
    _assert_exact_normalized(_source_slice(end_lines, end.map), end_source)
    assert start.map[1] <= end.map[0]
    return "\n".join(start_lines[start.map[1] : end.map[0]])


def _extract_paragraph_at(text: str, *, ordinal: int, expected_text: str) -> str:
    lines, tokens, index = _mapped_root_block_at(
        text,
        ordinal=ordinal,
        opening_type="paragraph_open",
        expected_text=expected_text,
    )
    return _source_slice(lines, tokens[index].map)


def _extract_list_item_at(
    text: str,
    *,
    parent_ordinal: int,
    parent_type: str,
    item_ordinal: int,
    expected_text: str,
) -> str:
    lines, tokens, parent_index = _mapped_root_block_at(
        text,
        ordinal=parent_ordinal,
        opening_type=parent_type,
        expected_text=expected_text,
    )
    parent_end = _closing_token_index(tokens, parent_index)
    items = [
        index
        for index in range(parent_index + 1, parent_end)
        if tokens[index].type == "list_item_open" and tokens[index].level == 1
    ]
    assert 0 <= item_ordinal < len(items)
    item_index = items[item_ordinal]
    item_end = _closing_token_index(tokens, item_index)
    item_text = _plain_text(
        tokens,
        item_index + 1,
        item_end,
        level=tokens[item_index].level + 2,
    )
    assert _normalized_anchor(expected_text) in _normalized_anchor(item_text)
    return _source_slice(lines, tokens[item_index].map)


def _extract_table_row_at(
    text: str,
    *,
    parent_ordinal: int,
    table_text: str,
    row_ordinal: int,
    row_text: str,
) -> str:
    lines, tokens, table_index = _mapped_root_block_at(
        text,
        ordinal=parent_ordinal,
        opening_type="table_open",
        expected_text=table_text,
    )
    table_end = _closing_token_index(tokens, table_index)
    rows = [
        index
        for index in range(table_index + 1, table_end)
        if tokens[index].type == "tr_open" and tokens[index].level == 2
    ]
    assert 0 <= row_ordinal < len(rows)
    row_index = rows[row_ordinal]
    row_end = _closing_token_index(tokens, row_index)
    actual_row_text = _plain_text(tokens, row_index + 1, row_end)
    assert _normalized_anchor(row_text) in _normalized_anchor(actual_row_text)
    return _source_slice(lines, tokens[row_index].map)


def test_ordered_contract_helpers_reject_reordering_duplicates_and_decoys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(AssertionError, match="out-of-order"):
        _assert_terms_in_order("second then first", "first", "second")

    with pytest.raises(AssertionError, match="duplicated"):
        _assert_terms_in_order(
            "second then first; decoy says first then second",
            "first",
            "second",
        )

    with pytest.raises(AssertionError):
        _extract_paragraph_at(
            "target transition is wrong\n\ntarget transition is the decoy",
            ordinal=0,
            expected_text="target transition is the decoy",
        )

    with pytest.raises(AssertionError):
        _assert_exact_normalized(
            "stop, mutate, verify, preserve",
            "stop, mutate, preserve, verify",
        )
    with pytest.raises(AssertionError):
        _assert_exact_normalized("stop\u2028verify", "stop verify")
    with pytest.raises(AssertionError):
        _assert_exact_normalized("HERMES_HOME", "hermes_home")

    hidden_item_decoys = (
        "- visible transition is wrong\n\n```markdown\n- target transition\n```",
        "- visible transition is wrong\n\n````markdown\n- target transition\n```",
        "- visible transition is wrong\n\n~~~markdown\n- target transition\n~~~",
        "- visible transition is wrong\n\n<!--\n- target transition\n-->\n",
        "- visible transition is wrong\n\n<script>\n- target transition\n</script>\n",
        "- visible transition is wrong\n\n<script\n>\n- target transition\n</script>\n",
        "- visible transition is wrong\n\n<div>\n- target transition\n</div>\n\n",
        "- visible transition is wrong\n\n    - target transition\n",
        "- visible transition is wrong\n\n \t- target transition\n",
        (
            "- visible transition is wrong\n\n"
            "  - nested\n\n"
            "    ```markdown\n"
            "    - target transition\n"
            "    ```\n"
        ),
    )
    for document in hidden_item_decoys:
        with pytest.raises(AssertionError):
            _extract_list_item_at(
                document,
                parent_ordinal=0,
                parent_type="bullet_list_open",
                item_ordinal=0,
                expected_text="target transition",
            )

    with pytest.raises(AssertionError):
        _extract_heading_section_at(
            "### Wrong\n\n```markdown\n### Start\nhidden\n### End\n```",
            tag="h3",
            ordinal=0,
            title="Start",
            ancestry=(),
        )

    with pytest.raises(AssertionError):
        _extract_paragraph_at(
            "visible transition is wrong\n\n`\ntarget transition\n`",
            ordinal=0,
            expected_text="target transition",
        )
    for tag in ("script", "pre", "style", "textarea", "div", "details"):
        with pytest.raises(AssertionError):
            _extract_paragraph_at(
                f"visible transition is wrong\n\n<{tag}\n>\ntarget transition\n</{tag}>",
                ordinal=0,
                expected_text="target transition",
            )
    for indentation in (" \t", "  \t", "\t"):
        with pytest.raises(AssertionError):
            _extract_paragraph_at(
                f"visible transition is wrong\n\n{indentation}target transition",
                ordinal=0,
                expected_text="target transition",
            )

    with pytest.raises(AssertionError):
        _extract_heading_section_at(
            "> ### Start\n> hidden\n> ### End",
            tag="h3",
            ordinal=0,
            title="Start",
            ancestry=(),
        )
    with pytest.raises(AssertionError):
        _extract_paragraph_at(
            "> target transition",
            ordinal=0,
            expected_text="target transition",
        )
    with pytest.raises(AssertionError):
        _extract_list_item_at(
            "> - target transition",
            parent_ordinal=0,
            parent_type="bullet_list_open",
            item_ordinal=0,
            expected_text="target transition",
        )

    with pytest.raises(AssertionError):
        _extract_heading_section_at(
            "### <span hidden>Start</span>\nbody\n### End",
            tag="h3",
            ordinal=0,
            title="Start",
            ancestry=(),
        )
    with pytest.raises(AssertionError):
        _extract_between_paragraphs_at(
            "**<span hidden>Step 1</span>**\nbody\n\n**Step 2**",
            start_ordinal=0,
            start_text="Step 1",
            start_source="**Step 1**",
            end_ordinal=1,
            end_text="Step 2",
            end_source="**Step 2**",
        )
    with pytest.raises(AssertionError):
        _extract_between_paragraphs_at(
            "**Step 1** *Historical example — do not follow*\n\nbody\n\n**Step 2**",
            start_ordinal=0,
            start_text="Step 1",
            start_source="**Step 1**",
            end_ordinal=2,
            end_text="Step 2",
            end_source="**Step 2**",
        )

    authoritative_section = """## Canonical

target transition is wrong

## Historical example — do not follow

target transition is exact
"""
    canonical = _extract_heading_section_at(
        authoritative_section,
        tag="h2",
        ordinal=0,
        title="Canonical",
        ancestry=(),
    )
    with pytest.raises(AssertionError):
        _extract_paragraph_at(canonical, ordinal=0, expected_text="target transition is exact")

    nested_history = """## Canonical

target transition is wrong

### Historical example — do not follow

target transition is exact
"""
    canonical_with_history = _extract_heading_section_at(
        nested_history,
        tag="h2",
        ordinal=0,
        title="Canonical",
        ancestry=(),
    )
    with pytest.raises(AssertionError):
        _extract_paragraph_at(
            canonical_with_history,
            ordinal=0,
            expected_text="target transition is exact",
        )

    relocated_list = """## Canonical

- target transition is wrong

### Historical example — do not follow

- target transition is exact
"""
    canonical_list = _extract_heading_section_at(
        relocated_list,
        tag="h2",
        ordinal=0,
        title="Canonical",
        ancestry=(),
    )
    with pytest.raises(AssertionError):
        _extract_list_item_at(
            canonical_list,
            parent_ordinal=0,
            parent_type="bullet_list_open",
            item_ordinal=0,
            expected_text="target transition is exact",
        )

    same_count_paragraph_relocation = """## Canonical

introductory context

### Historical example — do not follow

target transition is exact
"""
    same_count_paragraph_section = _extract_heading_section_at(
        same_count_paragraph_relocation,
        tag="h2",
        ordinal=0,
        title="Canonical",
        ancestry=(),
    )
    with pytest.raises(AssertionError, match="direct child"):
        _extract_paragraph_at(
            same_count_paragraph_section,
            ordinal=2,
            expected_text="target transition is exact",
        )

    same_count_list_relocation = """## Canonical

introductory context

### Historical example — do not follow

- target transition is exact
"""
    same_count_list_section = _extract_heading_section_at(
        same_count_list_relocation,
        tag="h2",
        ordinal=0,
        title="Canonical",
        ancestry=(),
    )
    with pytest.raises(AssertionError, match="direct child"):
        _extract_list_item_at(
            same_count_list_section,
            parent_ordinal=2,
            parent_type="bullet_list_open",
            item_ordinal=0,
            expected_text="target transition is exact",
        )

    same_count_table_relocation = """## Canonical

introductory context

### Historical example — do not follow

| Mode | Contract |
| --- | --- |
| Rollback | target transition is exact |
"""
    same_count_table_section = _extract_heading_section_at(
        same_count_table_relocation,
        tag="h2",
        ordinal=0,
        title="Canonical",
        ancestry=(),
    )
    with pytest.raises(AssertionError, match="direct child"):
        _extract_table_row_at(
            same_count_table_section,
            parent_ordinal=2,
            table_text="ModeContract",
            row_ordinal=1,
            row_text="target transition is exact",
        )

    with pytest.raises(AssertionError):
        _extract_heading_section_at(
            "# Root\n\n## Previous\n\n# Historical example — do not follow\n\n## Target",
            tag="h2",
            ordinal=3,
            title="Target",
            ancestry=("# Root",),
        )
    with pytest.raises(AssertionError):
        _extract_heading_section_at(
            "## Canonical `Historical example — do not follow`\n\nbody",
            tag="h2",
            ordinal=0,
            title="Canonical",
            ancestry=(),
        )
    with pytest.raises(AssertionError, match="inline HTML"):
        _extract_paragraph_at(
            "![<span hidden>target transition</span>](image.png)",
            ordinal=0,
            expected_text="target transition",
        )

    hidden_table = """| Mode | Contract |
| --- | --- |
| Recovery | visible transition is wrong |

<script
>
| Mode | Contract |
| --- | --- |
| Rollback | target transition |
</script>
"""
    with pytest.raises(AssertionError):
        _extract_table_row_at(
            hidden_table,
            parent_ordinal=0,
            table_text="ModeContract",
            row_ordinal=2,
            row_text="Rollback",
        )
    with pytest.raises(AssertionError):
        _extract_table_row_at(
            "`prefix\u2028| Rollback | target transition |\u2028suffix`",
            parent_ordinal=0,
            table_text="ModeContract",
            row_ordinal=1,
            row_text="Rollback",
        )
    with pytest.raises(AssertionError):
        _extract_table_row_at(
            "> | Mode | Contract |\n> | --- | --- |\n> | Rollback | target transition |",
            parent_ordinal=0,
            table_text="ModeContract",
            row_ordinal=1,
            row_text="Rollback",
        )

    checklist_item = _extract_list_item_at(
        "- [ ] target transition\n      visible continuation",
        parent_ordinal=0,
        parent_type="bullet_list_open",
        item_ordinal=0,
        expected_text="target transition",
    )
    _assert_exact_normalized(
        checklist_item,
        "- [ ] target transition visible continuation",
    )

    loose_checklist_item = _extract_list_item_at(
        "- [ ] target transition\n\n    visible contradiction",
        parent_ordinal=0,
        parent_type="bullet_list_open",
        item_ordinal=0,
        expected_text="target transition",
    )
    with pytest.raises(AssertionError):
        _assert_exact_normalized(loose_checklist_item, "- [ ] target transition")

    autolink_neighbor = _extract_paragraph_at(
        "<https://example.com>\n\nvisible target paragraph",
        ordinal=1,
        expected_text="visible target paragraph",
    )
    _assert_exact_normalized(autolink_neighbor, "visible target paragraph")

    crlf_contract = tmp_path / "contract.md"
    crlf_contract.write_bytes(b"## Contract\r\n\r\ntarget transition\r\n")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="LF only"):
        _read("contract.md")


def _extract_marked_contract(
    document: bytes,
    *,
    start: bytes,
    end: bytes,
) -> bytes:
    document.decode("utf-8", errors="strict")
    assert b"\r" not in document
    assert document.count(start) == 1
    assert document.count(end) == 1

    # CommonMark physical lines are LF-delimited; Unicode separators are ordinary content bytes.
    lines = document.split(b"\n")
    assert lines.count(start) == 1
    assert lines.count(end) == 1
    start_index = lines.index(start)
    end_index = lines.index(end)
    assert start_index < end_index
    assert end_index < len(lines) - 1

    before_lines = lines[:start_index]
    before = b"\n".join(before_lines)
    # The owner section must precede every raw-HTML opener, rather than partially parsing HTML.
    assert b"<" not in before

    fence_character: str | None = None
    fence_length = 0
    for raw_line in before_lines:
        line = raw_line.decode("utf-8")
        if fence_character is not None:
            closing = re.fullmatch(
                rf" {{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*",
                line,
            )
            if closing is not None:
                fence_character = None
                fence_length = 0
            continue

        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opening is None:
            continue
        delimiter, info = opening.groups()
        if delimiter[0] == "`" and "`" in info:
            continue
        fence_character = delimiter[0]
        fence_length = len(delimiter)
    assert fence_character is None
    contract_lines = lines[start_index + 1 : end_index]
    return b"\n".join(contract_lines) + b"\n"


def _assert_task4_status_document_contract(contents: dict[str, bytes]) -> None:
    assert contents.keys() == dict.fromkeys(TASK4_FROZEN_AUTHORITY_PATHS).keys()
    for document in contents.values():
        document.decode("utf-8", errors="strict")
    marker_owners = {
        _STATUS_COMPATIBILITY_START: "docs/compatibility.md",
        _STATUS_COMPATIBILITY_END: "docs/compatibility.md",
        _STATUS_STORAGE_START: "docs/operations.md",
        _STATUS_STORAGE_END: "docs/operations.md",
    }
    for marker, owner in marker_owners.items():
        assert sum(document.count(marker) for document in contents.values()) == 1
        assert contents[owner].count(marker) == 1

    compatibility_contract = _extract_marked_contract(
        contents["docs/compatibility.md"],
        start=_STATUS_COMPATIBILITY_START,
        end=_STATUS_COMPATIBILITY_END,
    )
    storage_contract = _extract_marked_contract(
        contents["docs/operations.md"],
        start=_STATUS_STORAGE_START,
        end=_STATUS_STORAGE_END,
    )
    assert compatibility_contract == _STATUS_COMPATIBILITY_CONTRACT
    assert storage_contract == _STATUS_STORAGE_CONTRACT


def _assert_task4_frozen_authority_hashes(
    contents: dict[str, bytes],
    expected_hashes: dict[str, str] | None = None,
) -> None:
    if expected_hashes is None:
        expected_hashes = _TASK4_FROZEN_AUTHORITY_SHA256
    assert contents.keys() == expected_hashes.keys()
    for relative_path, content in contents.items():
        assert b"\r" not in content, f"frozen authority must use LF only: {relative_path}"
        expected = expected_hashes[relative_path]
        assert re.fullmatch(r"[0-9a-f]{64}", expected) is not None
        assert hashlib.sha256(content).hexdigest() == expected


def _task4_literal_hash_map_from_source(source: str) -> dict[str, str]:
    tree = ast.parse(source)
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_TASK4_FROZEN_AUTHORITY_SHA256"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    value = assignments[0].value
    assert isinstance(value, ast.Dict)
    assert all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in value.keys)
    assert all(
        isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.values
    )
    return {
        key.value: item.value
        for key, item in zip(value.keys, value.values, strict=True)
        if isinstance(key, ast.Constant)
        and isinstance(key.value, str)
        and isinstance(item, ast.Constant)
        and isinstance(item.value, str)
    }


def _assert_task4_production_hash_map_is_source_literal() -> None:
    source = (
        (ROOT / "tests/test_repository_contract.py").read_bytes().decode("utf-8", errors="strict")
    )
    assert "\r" not in source
    literal = _task4_literal_hash_map_from_source(source)
    assert literal == _TASK4_FROZEN_AUTHORITY_SHA256
    assert tuple(literal) == TASK4_FROZEN_AUTHORITY_PATHS


def _assert_local_plan_pair_bytes(
    plan_bytes: bytes,
    index_bytes: bytes,
    *,
    index_sha256: str = LOCAL_PLAN_INDEX_SHA256,
) -> None:
    assert b"\r" not in plan_bytes, "local canonical plan must use LF only"
    assert b"\r" not in index_bytes, "local plan index must use LF only"
    assert hashlib.sha256(index_bytes).hexdigest() == index_sha256


def _read_local_plan_pair() -> tuple[bytes, bytes] | None:
    """Read ignored planning aids; tracked clean-clone authority is tested separately below."""

    plan_exists = LOCAL_PLAN_PATH.is_file()
    index_exists = LOCAL_PLAN_INDEX_PATH.is_file()
    if not plan_exists and not index_exists:
        return None
    assert plan_exists and index_exists, "local canonical plan and index must exist together"
    plan_bytes = LOCAL_PLAN_PATH.read_bytes()
    index_bytes = LOCAL_PLAN_INDEX_PATH.read_bytes()
    _assert_local_plan_pair_bytes(plan_bytes, index_bytes)
    return plan_bytes, index_bytes


def test_local_plan_pair_rejects_refreshed_hash_cr_mutations() -> None:
    if not LOCAL_PLAN_PATH.is_file() and not LOCAL_PLAN_INDEX_PATH.is_file():
        pytest.skip("ignored local planning aids are absent from this clean checkout")
    assert LOCAL_PLAN_PATH.is_file() and LOCAL_PLAN_INDEX_PATH.is_file()
    plan_bytes = LOCAL_PLAN_PATH.read_bytes()
    index_bytes = LOCAL_PLAN_INDEX_PATH.read_bytes()
    for separator in (b"\r\n", b"\r"):
        changed_plan = plan_bytes.replace(b"\n", separator)
        with pytest.raises(AssertionError, match="LF only"):
            _assert_local_plan_pair_bytes(plan_bytes=changed_plan, index_bytes=index_bytes)

        changed_index = index_bytes.replace(b"\n", separator)
        refreshed_index_sha = hashlib.sha256(changed_index).hexdigest()
        with pytest.raises(AssertionError, match="LF only"):
            _assert_local_plan_pair_bytes(
                plan_bytes=plan_bytes,
                index_bytes=changed_index,
                index_sha256=refreshed_index_sha,
            )


def test_owned_active_contract_inventory_is_complete() -> None:
    assert ACTIVE_CONTRACT_PATHS == (
        "README.md",
        "DESIGN.md",
        "docs/audit-findings.md",
        "docs/compatibility.md",
        "docs/configuration.md",
        "docs/development-instance.md",
        "docs/operations.md",
        "docs/public-release-checklist.md",
    )
    for relative_path in ACTIVE_CONTRACT_PATHS:
        assert (ROOT / relative_path).is_file()
    assert (
        "IMPLEMENTATION.md",
        *ACTIVE_CONTRACT_PATHS,
        "src/better_hermes_hindsight/config.py",
        "src/better_hermes_hindsight/hermes_plugin/cli.py",
    ) == TASK4_FROZEN_AUTHORITY_PATHS
    assert tuple(_TASK4_FROZEN_AUTHORITY_SHA256) == TASK4_FROZEN_AUTHORITY_PATHS


def test_best_effort_provider_scope_and_lifecycle_are_explicit() -> None:
    public_contract = "\n".join(_read(path) for path in ACTIVE_CONTRACT_PATHS)

    _assert_terms(
        public_contract,
        "better_hindsight",
        "external/self-hosted only",
        "hindsight-client==0.8.5",
        "Hindsight server 0.8.5",
        "current-query recall",
        "recall is enabled by default",
        "automatic retention is disabled by default",
        "released `sync_turn()`",
        "best-effort",
        "executor creation fails",
        "submission raises `RuntimeError` outside shutdown",
        "callback inline",
        "shutdown rejects late work",
        "local durability starts only after provider admission",
        "no direct-user provenance claim",
        "no pre-return or no-loss guarantee",
        "no Hermes-core prerequisite",
        "`codex_app_server` is unsupported on the current supported release",
        "isolated Hindsight instance and Hermes profile",
        "separate canary instance and bank",
        "preserves the old deployment",
        "no model-facing memory tools in the first prerelease",
        "profile-wide POSIX advisory lock",
        "bounded SQLite polling",
        "destination fingerprint",
        'update_mode="replace"',
        "stable document ID",
        "source documents are the preserved record",
    )


def test_task3_delivery_and_task4_operator_contract_are_documented_without_rollout_claims() -> None:
    readme = _read("README.md")
    _assert_terms(
        readme,
        "Tasks 0–6 are complete at checkpoint `3f542d4`",
        "Task 6 proof ran once",
        "Automatic retention is disabled by default",
        "Hermes-managed plugin installation",
        "must not be rerun without a changed candidate plus renewed",
    )

    delivery_contract = _read("docs/configuration.md") + _read("docs/operations.md")
    _assert_terms(
        delivery_contract,
        "profile-wide POSIX advisory lock",
        "bounded cross-process polling",
        "typed confirmation",
        "retain_timeout",
        "retain_failed",
        "retain_unconfirmed",
        "stable document ID",
        "replace mode",
        "not exactly-once transport",
        "hermes better_hindsight status",
        "hermes better_hindsight missions apply --confirm",
        "review findings were closed at checkpoint `3f542d4`",
    )
    assert "future task 4 operator behavior" not in _normalized(delivery_contract)


def test_active_contracts_do_not_reinstate_retired_requirements() -> None:
    for relative_path in ACTIVE_CONTRACT_PATHS:
        normalized = _normalized(_read(relative_path))
        assert "separate generic core prerequisite" not in normalized, relative_path
        assert "unknown origin remains ineligible" not in normalized, relative_path
        assert "before hermes reports the turn complete" not in normalized, relative_path
        assert "guarantees exactly-once transport" not in normalized, relative_path
        assert "provides exactly-once transport" not in normalized, relative_path
        assert "integration_mode" not in normalized, relative_path


def test_compatibility_baseline_and_released_callback_boundary_are_frozen() -> None:
    compatibility = _read("docs/compatibility.md")

    _assert_terms(
        compatibility,
        "v2026.7.20",
        "package 0.19.0",
        "3ef6bbd201263d354fd83ec55b3c306ded2eb72a",
        "41e0b6cf60942b7a4962aa7c7e2f1173527dfe2f",
        "4dae897265f09ed5b26f5e02b0f0fcb1325e0b6d",
        "ahead by 9",
        "behind by 1,416",
        "eb52760564dbba2e5971fa54bd67384e281cd3b8",
        "705757f362552918dfb0242906cb8466de320378",
        "installed hindsight-client 0.6.1",
        "hindsight-client==0.8.5",
        "Hindsight server 0.8.5",
        "plugins/memory/__init__.py",
        "plugins/memory/<name>/",
        "$HERMES_HOME/plugins/<name>/",
        "ctx.register_memory_provider",
        "MemoryProvider",
        "is_available()",
        "prefetch()",
        "queue_prefetch()",
        "sync_turn()",
        "serialized background executor",
        "documented as non-blocking",
        "On the supported Hermes 0.20.0 normal path",
        "executor creation fails",
        "submission raises `RuntimeError` outside shutdown",
        "invokes the callback inline",
        "shutdown rejects late work",
        "does not establish guaranteed pre-return admission",
        "may fail before Better Hindsight receives the callback",
        "arecall()",
        "aretain_batch()",
        "get_bank_profile()",
        "get_bank_config()",
        "update_bank_config()",
        "acreate_bank()",
        "delete_bank()",
        "aclose()",
    )

    for commit in (
        "41e0b6cf60942b7a4962aa7c7e2f1173527dfe2f",
        "821b11e631e5f663f2e9f915f77a353d1c528cbc",
        "d48bcb0d400bf852499758080f50ae49ed857626",
        "151a1b4f6045c86577806feb55909ed33e608752",
        "0e4f1ba7e017aac4d3c0995be941c5ce6364a17b",
        "fb8cc28775bc247eacc964d2c5d5d88830152adf",
        "517e0bcf0511564eec0290037e2a8d0ff3f1c895",
        "fcb1f9da2bb5f38f6270137fce13d051449cbcc5",
        "2b1d2bafc3e4f13ed8b961b6c81effccd7c066bd",
    ):
        assert commit in compatibility


def test_callback_boundary_and_retired_plan_precedence_are_explicit() -> None:
    router = _read("IMPLEMENTATION.md")
    public_contract = router + _read("README.md") + _read("docs/audit-findings.md")

    _assert_terms(
        public_contract,
        "completed-turn callbacks released Hermes actually supplies",
        "do not infer human/synthetic origin from text",
        "callbacks lost before Hermes executes the provider hook are outside that guarantee",
        "The previous Task 5 specification and uncommitted RED oracle were abandoned",
        "do not recover or continue it",
        "stale proof wording",
        "ci: rebaseline Hermes compatibility gates",
    )


def test_sanitized_audit_evidence_and_superseded_ideals_are_preserved() -> None:
    audit = _read("docs/audit-findings.md")
    public_contract = audit + _read("README.md") + _read("DESIGN.md")

    _assert_terms(
        audit,
        "superseded ideal requirements",
        "authoritative structured origin",
        "inline admission before turn return",
        "8.5–11 kB per query",
        "2.41 seconds median",
        "2.65 seconds p95",
        "874 documents",
        "13,197 raw memory units",
        "8,451 active observations",
        "82.65%",
        "73.67%",
        "0.54%",
        "min_scores.final = 0.10",
        "project-specific",
    )
    _assert_terms(public_contract, "narrower", "not universally better")


def test_configuration_contract_has_only_direct_capability_switches_and_finite_bounds() -> None:
    configuration = _read("docs/configuration.md")
    config_source = _read("src/better_hermes_hindsight/config.py")

    assert "integration_mode" not in _normalized(configuration + config_source)
    assert "IntegrationMode" not in config_source
    assert "MissionPolicy" not in config_source
    assert "missions.policy" not in _normalized(configuration + config_source)
    assert "outbox.retry_multiplier" not in _normalized(configuration + config_source)
    assert "outbox.shutdown_join_seconds" not in _normalized(configuration + config_source)
    _assert_terms(
        configuration,
        "recall.enabled` | `true`",
        "retain.enabled` | `false`",
        "retain.timeout_seconds` | `60.0`",
        "at most 300 seconds",
        "outbox.max_pending_rows` | `2000`",
        "Integer from 1 through 100,000",
        "outbox.max_pending_bytes` | `134217728`",
        "Integer from 1 through 1,073,741,824",
        "outbox.busy_timeout_seconds` | `1.0`",
        "outbox.poll_interval_seconds` | `2.0`",
        "0.1 through 60.0 seconds",
        "outbox.retry_initial_seconds` | `2.0`",
        "outbox.retry_max_seconds` | `300.0`",
        "at most 3,600 seconds",
        "segment_max_bytes",
        "must not exceed `outbox.max_pending_bytes`",
        "retry_initial_seconds",
        "must not exceed `outbox.retry_max_seconds`",
    )


def test_release_gate_requires_isolated_development_and_reversible_canary() -> None:
    checklist = _read("docs/public-release-checklist.md")

    _assert_terms(
        checklist,
        "no Hermes core patch or Better-owned host fork",
        "released `sync_turn()` callback",
        "durability begins only after the provider admission commit",
        "no direct-user provenance",
        "no pre-return or no-loss guarantee",
        "`codex_app_server` remains explicitly unsupported",
        "retention is disabled by default",
        "no model-facing memory tools",
        "dedicated Hermes interpreter/profile",
        "separate canary instance",
        "preserving the old deployment",
    )


def test_task6_live_proof_and_non_activation_contract_are_explicit() -> None:
    development = _read("docs/development-instance.md")
    operations = _read("docs/operations.md")
    rollback = _read("docs/rollback.md")
    checklist = _read("docs/public-release-checklist.md")
    task6_contract = "\n".join((development, operations, rollback, checklist))

    _assert_terms(
        development,
        "does not provision Hindsight, Docker, a datastore, an interpreter, or a credential",
        "Profile isolation by itself is insufficient",
        "hindsight-client==0.8.5",
        "BETTER_HINDSIGHT_ALLOW_DEV_WRITES=1",
        "inherited `HINDSIGHT_*`",
        "independently prepared destination fingerprint",
        "bank must not exist",
        "fails closed before create/upsert",
        "zero mutations",
        "public document listing",
        "metadata-based long-source reconstruction",
        "one byte-identical callback replay",
        "one fresh interpreter restart that drains durably pending local work",
        "only the two intended mission fields changed",
        "retention disablement",
        "completely writes, syncs, and rereads a random cleanup token",
        "existing-bank rejection",
        "POSIX process group",
        "BETTER_HINDSIGHT_REQUIRE_LIVE_PROOF=1",
        "remote ownership name match",
        "write_attempted_outcome_unknown",
        "exclusive writer/key rule",
        "child never deletes the bank",
        "stalled, partial, or unverifiable marker",
        "exception-total",
        "unknown launch outcome",
        "KeyboardInterrupt",
        "normal leader exit",
        "one exact JSON line",
    )
    _assert_terms(
        checklist,
        "Rolling compatibility and dependency audits",
        "current stable Hermes release",
        "historical characterization lane",
        "complete runtime dependency closure",
        "actively supported Hermes compatibility environment",
        "required lifecycle suite",
        "Host findings remain upstream evidence",
    )
    _assert_terms(
        task6_contract,
        "does not activate",
        "existing Hindsight deployment",
        "remain running and untouched",
        (
            "no initial migration, deduplication, reconstruction, reconsolidation, pruning, "
            "or deletion"
        ),
        "separate authorization",
    )
    assert "BETTER_HINDSIGHT_DEV_API_KEY='<isolated-development-api-key>'" in development
    assert not re.search(r"better-hindsight-dev-[0-9a-f]{32}", development)


def test_task3_sender_contract_is_frozen_before_red_tests() -> None:
    local_plan_pair = _read_local_plan_pair()
    if local_plan_pair is None:
        pytest.skip("ignored local planning aids are absent from this clean checkout")
    plan_bytes, _plan_index_bytes = local_plan_pair
    plan = plan_bytes.decode("utf-8", errors="strict")
    task3 = plan.split("### Task 3:", maxsplit=1)[1].split("### Task 4:", maxsplit=1)[0]

    _assert_terms(
        task3,
        "schema version 1 remains unchanged",
        "canonical redacted and sorted retain tags",
        "normalized observation scopes",
        "literal golden document IDs remain byte-for-byte unchanged",
        "observation scopes bind the destination fingerprint and wire request only",
        "reset every stale `sending` row",
        "cap-first saturating retry loop",
        "`attempt_count=10_000`",
        "network I/O occurs outside SQLite transactions",
        "`retain_timeout`, `retain_failed`, or `retain_unconfirmed`",
        "typed `RetainConfirmation`",
        "retain-enabled runtime starts exactly one daemon sender before the runtime is published",
        "passive contender",
        "non-owner admission remains allowed",
        "must not close the outbox or client beneath a live sender",
        "explicit draining/unsettled state",
        "per-call unsettled token set",
        "one runner lock/condition",
        "atomically rechecks completion and publishes",
        "already-admitted prepublication calls",
        "two cancellation-resistant calls",
        "completion/registration race",
        "`AsyncRunnerUnsettledError`",
        "no second SDK call",
        "crossed retain deadline remains `retain_timeout`",
        "cancellation-resistant retain",
        "strictly valid late success",
        "competing process proves the profile lock remains held",
        "sender has already joined",
        "second `finalize_process_runtime()`",
        "outbox/client/runner close counts remain zero",
        "real pinned `MemoryManager` and real pinned SDK adapter",
        "secret-shaped retain tags",
        "boolean/integer lookalikes",
        "`tests/integration/test_released_hermes_admission.py`",
        "`tests/unit/test_provider_retention.py`",
        "`README.md`",
        "`tests/test_repository_contract.py`",
        "`docs/operations.md` enters `ACTIVE_CONTRACT_PATHS`",
        "route to active-plan Task 4",
        "operator-visible status counts remain Task 4",
    )


def test_local_plan_files_match_the_tracked_router_when_present() -> None:
    router = _read("IMPLEMENTATION.md")
    _assert_terms(
        router,
        ".hermes/plans/2026-07-27_071437-best-effort-plugin.md",
        "ef41f48a3844048a8ff534a3b5132be5d23e962112c10e741bd3fe403b28bc31",
        "Tasks 0–7 and the rolling Hermes compatibility/release rebaseline are complete",
        "Last completed remote candidate checkpoint: `030aeff`",
        "dedicated Hermes interpreter/profile",
    )
    normalized_router = _normalized(router)
    assert "Task 5 active".casefold() not in normalized_router

    local_plan_pair = _read_local_plan_pair()
    if local_plan_pair is None:
        pytest.skip("ignored local planning aids are absent from this clean checkout")
    plan_bytes, plan_index_bytes = local_plan_pair
    plan = plan_bytes.decode("utf-8", errors="strict")
    plan_index = plan_index_bytes.decode("utf-8", errors="strict")
    plan_hash = hashlib.sha256(plan_bytes).hexdigest()

    assert plan_hash == "ef41f48a3844048a8ff534a3b5132be5d23e962112c10e741bd3fe403b28bc31"
    assert plan_hash in router
    assert plan_hash in plan_index
    _assert_terms(
        plan_index,
        "2a05a10",
        "Tasks 0–7 and the rolling Hermes compatibility/release rebaseline are complete",
        "rolling Hermes compatibility/release rebaseline",
        "Superseded Task 5 direction",
    )
    assert "ACTIVE — CANONICAL IMPLEMENTATION PLAN" in plan[:1000]

    crlf_plan_bytes = plan_bytes.replace(b"\n", b"\r\n")
    assert crlf_plan_bytes != plan_bytes
    assert hashlib.sha256(crlf_plan_bytes).hexdigest() != plan_hash

    for retired_name in (
        "2026-07-25_194157-better-hermes-hindsight-implementation.md",
        "2026-07-27_055353-plugin-only-rescope.md",
    ):
        retired_path = ROOT / ".hermes" / "plans" / retired_name
        if retired_path.is_file():
            retired_text = retired_path.read_bytes().decode("utf-8", errors="strict")
            assert "\r" not in retired_text
            retired_header = "\n".join(retired_text.split("\n")[:10])
            _assert_terms(retired_header, "RETIRED — DO NOT IMPLEMENT", "HISTORICAL RECORD ONLY")


def test_task4_production_hash_oracle_is_source_literal() -> None:
    _assert_task4_production_hash_map_is_source_literal()
    computed_forms = (
        "_TASK4_FROZEN_AUTHORITY_SHA256 = {p: p for p in ()}",
        '_TASK4_FROZEN_AUTHORITY_SHA256 = {"x": make_hash()}',
        '_TASK4_FROZEN_AUTHORITY_SHA256 = {"x": "a" + "b"}',
        '_TASK4_FROZEN_AUTHORITY_SHA256 = {**{"x": "y"}}',
    )
    for source in computed_forms:
        with pytest.raises(AssertionError):
            _task4_literal_hash_map_from_source(source)


def test_task4_status_public_documentation_is_frozen_in_clean_clones() -> None:
    if not (ROOT / "src/better_hermes_hindsight/management.py").is_file():
        return

    _assert_task4_production_hash_map_is_source_literal()
    contents: dict[str, bytes] = {}
    for relative_path in TASK4_FROZEN_AUTHORITY_PATHS:
        path = ROOT / relative_path
        assert path.is_file(), relative_path
        contents[relative_path] = path.read_bytes()
    _assert_task4_status_document_contract(contents)
    _assert_task4_frozen_authority_hashes(contents)


def test_task4_status_document_oracle_rejects_structural_and_authority_drift() -> None:
    compatibility = (
        _STATUS_COMPATIBILITY_START
        + b"\n"
        + _STATUS_COMPATIBILITY_CONTRACT
        + _STATUS_COMPATIBILITY_END
        + b"\n"
    )
    operations = (
        _STATUS_STORAGE_START + b"\n" + _STATUS_STORAGE_CONTRACT + _STATUS_STORAGE_END + b"\n"
    )
    contents = {
        relative_path: f"frozen authority: {relative_path}\n".encode()
        for relative_path in TASK4_FROZEN_AUTHORITY_PATHS
    }
    contents["docs/compatibility.md"] = compatibility
    contents["docs/operations.md"] = operations

    # The raw byte bodies include every valid branch qualification and explicit negation.
    _assert_task4_status_document_contract(contents)
    expected_hashes = {
        relative_path: hashlib.sha256(content).hexdigest()
        for relative_path, content in contents.items()
    }
    _assert_task4_frozen_authority_hashes(contents, expected_hashes)

    structural_counterfactuals: list[dict[str, bytes]] = []
    for changed_compatibility in (
        compatibility.replace(b"`vfs=unix`", b"`VFS=UNIX`", 1),
        compatibility.replace(
            _STATUS_COMPATIBILITY_START + b"\n",
            b"prefix " + _STATUS_COMPATIBILITY_START + b"\n",
            1,
        ),
        compatibility.replace(
            _STATUS_COMPATIBILITY_START + b"\n",
            b"  " + _STATUS_COMPATIBILITY_START + b"\n",
            1,
        ),
        b"````markdown\n```\n" + compatibility + b"````\n",
        b"~~~~markdown\n~~~\n" + compatibility + b"~~~~\n",
        b"<!-- open comment\n" + compatibility + b"-->\n",
        b'<script type="text/plain">\n' + compatibility + b"</script>\n",
    ):
        changed = dict(contents)
        changed["docs/compatibility.md"] = changed_compatibility
        structural_counterfactuals.append(changed)

    for delimiter in (b"````", b"~~~~"):
        for non_commonmark_separator in (
            "\u2028".encode(),
            "\u2029".encode(),
            "\u0085".encode(),
            b"\x0b",
        ):
            changed = dict(contents)
            changed["docs/compatibility.md"] = (
                delimiter
                + b"\ntext"
                + non_commonmark_separator
                + delimiter
                + b"\n"
                + compatibility
                + delimiter
                + b"\n"
            )
            structural_counterfactuals.append(changed)

    changed_indentation = dict(contents)
    changed_indentation["docs/operations.md"] = operations.replace(
        b"  SQLite `mode=ro", b"    SQLite `mode=ro", 1
    )
    structural_counterfactuals.append(changed_indentation)

    duplicate_elsewhere = dict(contents)
    duplicate_elsewhere["README.md"] += b"\n" + compatibility
    structural_counterfactuals.append(duplicate_elsewhere)

    for changed in structural_counterfactuals:
        recomputed_hashes = {
            relative_path: hashlib.sha256(content).hexdigest()
            for relative_path, content in changed.items()
        }
        _assert_task4_frozen_authority_hashes(changed, recomputed_hashes)
        with pytest.raises(AssertionError):
            _assert_task4_status_document_contract(changed)

    for relative_path, content in contents.items():
        assert b"\n" in content
        for changed_content in (
            content.replace(b"\n", b"\r\n"),
            content.replace(b"\n", b"\r"),
        ):
            changed = dict(contents)
            changed[relative_path] = changed_content
            recomputed_hashes = {
                path: hashlib.sha256(candidate).hexdigest() for path, candidate in changed.items()
            }
            with pytest.raises(AssertionError, match="LF only"):
                _assert_task4_frozen_authority_hashes(changed, recomputed_hashes)

    contradictions = (
        b"Status fails whenever S&#72;M is absent.",
        b"The shared-memory coordination file and all timestamps remain unchanged.",
        b"SQLite's default V&#70;S is supported too.",
        b"Every concurrency race is prevented.",
    )
    for relative_path in TASK4_FROZEN_AUTHORITY_PATHS:
        for contradiction in contradictions:
            changed = dict(contents)
            changed[relative_path] += b"\n" + contradiction + b"\n"
            with pytest.raises(AssertionError):
                _assert_task4_frozen_authority_hashes(changed, expected_hashes)


def test_task5_completed_implementation_and_task6_route_are_frozen() -> None:
    router = _read("IMPLEMENTATION.md")
    _assert_terms(
        router,
        "ef41f48a3844048a8ff534a3b5132be5d23e962112c10e741bd3fe403b28bc31",
        "ci: rebaseline Hermes compatibility gates",
        "2a05a10",
        "The previous Task 5 specification and uncommitted RED oracle were abandoned",
        "10,144 lines and 1,928 test nodes",
        "Hermes 0.19.0 already owns Git plugin install",
        "do not recover or continue it",
        "remote segment reconstruction metadata",
        "thin root plugin layout",
        "no custom installer",
        "retry accounting",
        "operator `status` and `missions` controls",
        "no retry/drain/dead-letter command or dead-letter state",
        "there is no `BETTER_HINDSIGHT_PROFILE` setting",
    )
    assert "retry/dead-letter accounting" not in router
    assert "operator `status`, `missions`, retry, and dead-letter controls" not in router

    if LOCAL_PLAN_INDEX_PATH.is_file():
        plan_index = _read(".hermes/plans/README.md")
        _assert_terms(
            plan_index,
            "Tasks 0–7 and the rolling Hermes compatibility/release rebaseline are complete",
            "rolling Hermes compatibility/release rebaseline",
            "Superseded Task 5 direction",
            "The uncommitted 10,144-line/1,928-case RED file was removed",
            "Do not reconstruct or continue that oracle",
            "Multi-segment remote documents must carry",
            "released `hermes plugins install|update|remove`",
        )

    readme = _read("README.md")
    _assert_terms(
        readme,
        "Tasks 0–6 are complete at checkpoint `3f542d4`",
        "Task 6 proof ran once",
        "Hermes-managed plugin installation",
        "No custom installer",
        "multi-segment reconstruction metadata",
    )


def test_local_task4_plan_contract_matches_completed_implementation_when_present() -> None:
    local_plan_pair = _read_local_plan_pair()
    if local_plan_pair is None:
        pytest.skip("ignored local planning aids are absent from this clean checkout")
    plan_bytes, plan_index_bytes = local_plan_pair
    plan = plan_bytes.decode("utf-8", errors="strict")
    plan_index = plan_index_bytes.decode("utf-8", errors="strict")
    task4 = plan.split("### Task 4:", maxsplit=1)[1].split("### Task 5:", maxsplit=1)[0]
    _assert_terms(
        task4,
        "src/better_hermes_hindsight/hermes_plugin/cli.py",
        "synchronous `better_hindsight_command(args)`",
        "mutually exclusive ordered partition",
        "`1m_to_lt_1h`",
        "SQLite `mode=ro&vfs=unix`",
        "`PRAGMA query_only=ON`",
        "existing regular `-shm` is also required at preflight",
        "`-shm` is derived SQLite coordination state",
        "initialize, recover, resize, or otherwise update WAL-index contents",
        "link count, mode, UID, GID, and xattrs/ACL xattrs must remain unchanged",
        "may only acquire/release a transient kernel `flock`",
        "Quiescent mutation-oracle tests require database/WAL/profile-lock bytes",
        "authorized same-principal writer may legitimately append, checkpoint, or update",
        "misattributing external byte or timestamp changes to status",
        "exact `single_principal=true` assertion is also the filesystem threat boundary",
        (
            "supported concurrency case is ordinary row work against stable "
            "database/sidecar identities"
        ),
        "journal-mode transitions, sidecar teardown, and ABA substitution",
        "cannot prevent SQLite from touching/creating a raced pathname",
        "same-principal TOCTOU side effects and undetectable ABA limit",
        "neither `-wal`, `-shm`, nor rollback `-journal` exists at preflight",
        "`mode=ro&immutable=1&vfs=unix` for that sidecar-absent main-file snapshot",
        "regardless of the persisted journal mode",
        "requires all three sidecars to remain absent",
        "pre-existing rollback journal or SHM without WAL is unavailable",
        "`immutable=1` is forbidden whenever a WAL exists",
        "avoid a bespoke WAL parser",
        "commits a row present only in uncheckpointed WAL frames",
        "active `immutable=1` would return the stale main-file count",
        '`os.name == "posix"`',
        "SQLite `>=3.22.0`",
        "before `sqlite3.connect()`",
        "built-in POSIX `unix` VFS selected explicitly with `vfs=unix`",
        ("unavailable `unix` VFS fails connection selection before the target database is opened"),
        "public operations documentation",
        "`single_principal=true`",
        "exact pinned SDK `BankConfigResponse`",
        "write_attempted` immediately before PATCH dispatch",
        "write_attempted_outcome_unknown",
        "Exit codes are fixed",
        "`outbox` is exactly `ready|uninitialized`",
        "`authorization_required`",
        "`mission_prewrite_unavailable`",
        "`runtime_cleanup_failed`",
        "src/better_hermes_hindsight/config.py",
        "host-owned stderr may echo arbitrarily long malformed argv",
        "all-unconfigured failed-GET case",
        "docs/audit-findings.md",
        "Define the Task 4 frozen authority corpus exactly as tracked `IMPLEMENTATION.md`",
        "public configuration docstring owner",
        "public CLI-help owner",
        "Once tracked `management.py` exists, a separate unconditional clean-clone repository test",
        "read that complete corpus as raw UTF-8 bytes",
        "`better-hindsight-status-compatibility`",
        "`better-hindsight-status-storage`",
        "none of the four marker tokens may occur in another authority file",
        "marker must precede every `<` byte in its file",
        "Physical lines are split only on the LF byte",
        "U+2028, U+2029, NEL, VT",
        "explicit source-literal dictionary",
        "AST discriminator rejects comprehensions",
        "Canonical ignored-plan SHA checks likewise hash `read_bytes()` directly",
        "CRLF mutation misses the tracked digest",
        "same-line/indented markers, shorter pseudo-closes",
        "import/help perform no database or lock access",
        "Do not add IPC, a retry/drain command",
    )
    _assert_terms(
        plan,
        (
            "Handler-controlled JSON from mission status/check/apply is bounded and sanitized; "
            "released host-owned argparse stderr is outside that guarantee"
        ),
        "never claims a custom VFS or hostile same-UID safety",
        "An unconditional clean-clone test activated by tracked `management.py`",
        "complete explicitly scoped authority corpus as raw bytes",
        "globally unique owner-only case-sensitive LF marker lines",
        "fences parsed on LF bytes only",
        "ignored canonical-plan SHA against raw bytes",
        "AST-proven source-literal map",
        "Unicode-separator pseudo-closes",
        "sentinels, computed hashes, CRLF/case/structure drift",
        "without trying to interpret arbitrary English",
    )
    assert "Mission status/check/apply commands are bounded and sanitized" not in plan

    _assert_terms(
        plan_index,
        "ef41f48a3844048a8ff534a3b5132be5d23e962112c10e741bd3fe403b28bc31",
        "Tasks 0–7 and the rolling Hermes compatibility/release rebaseline are complete",
        "rolling Hermes compatibility/release rebaseline",
        "Superseded Task 5 direction",
        "hermes plugins install|update|remove",
        "Do not reconstruct or continue that oracle",
    )


def test_local_task5_contract_is_product_aligned_when_present() -> None:
    local_plan_pair = _read_local_plan_pair()
    if local_plan_pair is None:
        pytest.skip("ignored local planning aids are absent from this clean checkout")
    plan_bytes, _plan_index_bytes = local_plan_pair
    plan = plan_bytes.decode("utf-8", errors="strict")
    task5 = plan.split("### Task 5:", maxsplit=1)[1].split("### Task 6:", maxsplit=1)[0]
    task6 = plan.split("### Task 6:", maxsplit=1)[1].split("### Task 7:", maxsplit=1)[0]

    _assert_terms(
        plan,
        "| Installation | Released Hermes Git plugin lifecycle",
        "| Rollback | Stop every Hermes process sharing the interpreter",
        "Dedicated Hermes interpreter/profile",
        "uv pip --python <interpreter>",
    )
    assert "Wheel plus marker-owned filesystem shim" not in plan
    assert "uninstall the verified shim" not in plan

    _assert_terms(
        task5,
        "Preserve segmented sources and add host-managed installation",
        "better_hindsight_payload_schema",
        "better_hindsight_source_sha256",
        "better_hindsight_segment_index",
        "better_hindsight_segment_count",
        "hermes plugins install|update|remove",
        "thin provider-registration bridge",
        "thin bridge to the wheel's existing operator CLI",
        "owns no custom installer entry point",
        "Use a small behavior suite rather than Cartesian fault matrices",
        "restore exact `hindsight-client==0.6.1`",
    )
    retired = _normalized(task5)
    for stale_requirement in (
        "must create a tombstone",
        "stage-<32 lowercase hex>",
        "marker-owned checked-hash cache files",
        ".better-hindsight-transactions",
        "exact host-module provenance",
    ):
        assert stale_requirement not in retired

    _assert_terms(
        task6,
        "one bounded end-to-end proof",
        "including one above `retain.segment_max_bytes`",
        "reconstruct the long source from returned segment metadata",
        "expected synthetic content and provenance are useful",
        "do not add repeated-run aggregation, a ranking framework, or release thresholds",
        "No initial migration, deduplication, reconstruction, pruning, or deletion is required",
    )


def test_task5_docs_use_host_lifecycle_and_explicit_sdk_transition() -> None:
    expected = {
        "README.md": (
            "Hermes-managed plugin installation",
            "No custom installer",
            "hindsight-client==0.8.5",
            "multi-segment reconstruction metadata",
            "every Hermes process sharing the interpreter is stopped",
            "A profile scopes config and data, not packages",
        ),
        "docs/installation.md": (
            'hermes --profile "$PROFILE" plugins install',
            "Stop every process sharing the interpreter",
            'uv pip install --python "$HERMES_PYTHON"',
            'uv pip check --python "$HERMES_PYTHON"',
            "hindsight-client==0.8.5",
            'hermes --profile "$PROFILE" config set memory.provider better_hindsight',
            "A Hermes profile scopes configuration, the outbox, and plugin checkout",
        ),
        "docs/rollback.md": (
            'hermes --profile "$PROFILE" plugins remove better_hindsight',
            'uv pip uninstall --python "$HERMES_PYTHON"',
            'uv pip check --python "$HERMES_PYTHON"',
            "restore exact `hindsight-client==0.6.1`",
            "Better's outbox and both banks",
            "Every Hermes gateway, TUI, CLI, and worker sharing the interpreter must be stopped",
        ),
        "docs/compatibility.md": (
            "same interpreter cannot run the two providers as a configuration-only switch",
            "released Hermes plugin lifecycle",
            "host-managed Git plugin directory",
            "uv pip --python",
        ),
        "docs/public-release-checklist.md": (
            "host-managed Git plugin installation",
            "fresh-process provider/CLI discovery",
            "explicit Better `0.8.5` and bundled `0.6.1` SDK states",
            "multi-segment reconstruction metadata",
            "every process sharing the interpreter is stopped",
        ),
    }
    for path, terms in expected.items():
        _assert_terms(_read(path), *terms)

    retired = "\n".join(_normalized(_read(path)) for path in expected)
    for stale in (
        ".better-hindsight-transactions",
        "marker-owned checked-hash",
        "stage-<32 lowercase hex>",
        "fixed `uninstalling`",
        "py_compile.pycinvalidationmode",
    ):
        assert stale not in retired
    assert "no custom installer" in retired
    assert '"$hermes_python" -m pip' not in retired
    assert "better_hindsight_profile" not in retired
    assert "--upgrade" not in _read("docs/installation.md")
    assert "--upgrade" not in _read("docs/rollback.md")
    assert _read("docs/installation.md").count('uv pip check --python "$HERMES_PYTHON"') == 2
    assert _read("docs/rollback.md").count('uv pip check --python "$HERMES_PYTHON"') == 2


def test_task4_owned_docs_describe_current_operator_commands_without_stale_negatives() -> None:
    expected = {
        "README.md": (
            "hermes better_hindsight status",
            "hermes better_hindsight missions apply --confirm",
        ),
        "DESIGN.md": (
            "better_hindsight missions check",
            "write_attempted_outcome_unknown",
        ),
        "docs/audit-findings.md": (
            "better_hindsight missions apply --confirm",
            "never becomes initialization policy",
        ),
        "docs/compatibility.md": (
            "Passive `better_hindsight status`",
            "Explicit mission check/apply",
        ),
        "docs/configuration.md": (
            "better_hindsight missions check",
            "better_hindsight missions apply --confirm",
        ),
        "docs/operations.md": (
            "hermes better_hindsight status",
            "hermes better_hindsight missions apply --confirm",
        ),
    }
    stale = {
        "README.md": ("mission changes are future", "Check/apply is explicit future"),
        "DESIGN.md": ("explicit future mission", "A future explicit operator command"),
        "docs/audit-findings.md": ("explicit future operator behavior",),
        "docs/compatibility.md": ("future confirmation-gated mission apply",),
        "docs/configuration.md": ("future operator",),
        "docs/operations.md": ("management commands remain Task 4", "adds no queue CLI"),
    }
    for relative_path, terms in expected.items():
        content = _read(relative_path)
        _assert_terms(content, *terms)
        for obsolete in stale[relative_path]:
            assert obsolete not in content


def test_changed_markdown_links_resolve_inside_repository() -> None:
    readme = _read("README.md")
    _assert_terms(
        readme,
        "IMPLEMENTATION.md",
        "docs/compatibility.md",
        "docs/operations.md",
        "docs/audit-findings.md",
    )

    for relative_target in (
        "IMPLEMENTATION.md",
        "docs/compatibility.md",
        "docs/operations.md",
        "docs/audit-findings.md",
        "DESIGN.md",
    ):
        assert (ROOT / relative_target).is_file(), f"broken repository link: {relative_target}"


def test_hermes_host_is_selected_by_a_rolling_compatibility_matrix() -> None:
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    optional = project.get("optional-dependencies", {})
    assert "proof" not in optional
    assert not any(
        dependency.startswith("hermes-agent")
        for dependencies in optional.values()
        for dependency in dependencies
    )

    workflow = _read(".github/workflows/ci.yml")
    _assert_terms(
        workflow,
        "compatibility:",
        "historical-0.19.0",
        "current-v2026.8.3",
        "3ef6bbd201263d354fd83ec55b3c306ded2eb72a",
        "3c27eb6234bf91b8ceee9e9071591b31e9b148cb",
        "BETTER_HINDSIGHT_EXPECT_HERMES_VERSION",
        "BETTER_HINDSIGHT_EXPECT_HERMES_COMMIT",
        "continue-on-error: ${{ matrix.historical }}",
    )
    compatibility = _workflow_job("compatibility", workflow=".github/workflows/ci.yml")
    for step_name in (
        "Install historical Hermes characterization host",
        "Install current supported Hermes host",
    ):
        install_step = _workflow_step(compatibility, step_name)
        script = install_step.get("run")
        assert isinstance(script, str)
        assert script.count("uv pip check --python .venv/bin/python") == 1
        assert "--upgrade" not in script


def test_release_contract_audits_plugin_dependency_closure_and_tests_hosts() -> None:
    security = _read(".github/workflows/security.yml")
    _assert_terms(
        security,
        "Better Hindsight runtime/build dependency roots",
        "Better Hindsight runtime/build dependency closure",
        "uv==0.11.4",
        "Export locked project-owned dependency closure",
        "uv export --quiet --frozen --all-extras --no-emit-project",
        "Audit locked project-owned dependency closure",
        "pip-audit --progress-spinner off --no-deps --disable-pip",
        'dependencies.extend(project.get("dependencies", []))',
        'pip-audit --progress-spinner off -r "${RUNNER_TEMP}/project-dependencies.txt"',
        "supported-hermes-observation:",
        "Supported Hermes environment observation (current-v2026.8.3)",
        "3c27eb6234bf91b8ceee9e9071591b31e9b148cb",
        "Record supported-host dependency observations",
    )
    static_job = _workflow_job("python-static-security")
    static_install = _workflow_step(static_job, "Install static scanners")
    assert _shell_tokens(static_install) == [
        "python",
        "-m",
        "pip",
        "install",
        "semgrep==1.168.0",
        "zizmor==1.26.1",
    ]

    plugin_job = _workflow_job("project-dependency-security")
    assert "if" not in plugin_job
    assert "continue-on-error" not in plugin_job
    strategy = plugin_job.get("strategy")
    assert isinstance(strategy, dict)
    assert strategy.get("fail-fast") is False
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict)
    assert matrix.get("python-version") == ["3.11", "3.12", "3.13"]
    dependency_roots = _workflow_step(
        plugin_job, "Prepare Better Hindsight runtime/build dependency roots"
    )
    install_audit_tools = _workflow_step(plugin_job, "Install dependency audit tools")
    verify_lock = _workflow_step(plugin_job, "Verify project lock is current")
    export_lock = _workflow_step(plugin_job, "Export locked project-owned dependency closure")
    audit_roots = _workflow_step(
        plugin_job, "Audit Better Hindsight runtime/build dependency closure"
    )
    audit_lock = _workflow_step(plugin_job, "Audit locked project-owned dependency closure")
    for step in (dependency_roots, verify_lock, export_lock, audit_roots, audit_lock):
        assert "if" not in step
        assert "continue-on-error" not in step

    assert _shell_tokens(install_audit_tools) == [
        "python",
        "-m",
        "pip",
        "install",
        "pip-audit==2.10.1",
        "uv==0.11.4",
    ]
    assert _shell_tokens(verify_lock) == ["uv", "lock", "--check"]

    dependency_script = dependency_roots.get("run")
    assert isinstance(dependency_script, str)
    _assert_terms(
        dependency_script,
        'dependencies.extend(project.get("dependencies", []))',
        'output.write_text(chr(10).join(dependencies), encoding="utf-8")',
    )
    assert "GITHUB_OUTPUT" not in dependency_script

    assert _shell_tokens(audit_roots) == [
        "pip-audit",
        "--progress-spinner",
        "off",
        "-r",
        "${RUNNER_TEMP}/project-dependencies.txt",
    ]
    assert _shell_tokens(export_lock) == [
        "uv",
        "export",
        "--quiet",
        "--frozen",
        "--all-extras",
        "--no-emit-project",
        "--format",
        "requirements-txt",
        "--output-file",
        "${RUNNER_TEMP}/locked-project-dependencies.txt",
    ]
    assert _shell_tokens(audit_lock) == [
        "pip-audit",
        "--progress-spinner",
        "off",
        "--no-deps",
        "--disable-pip",
        "-r",
        "${RUNNER_TEMP}/locked-project-dependencies.txt",
    ]

    assert "supported-hermes-security:" not in security
    host_job = _workflow_job("supported-hermes-observation")
    assert "if" not in host_job
    assert "continue-on-error" not in host_job
    host_checkout = _workflow_step(host_job, "Check out current Hermes release source")
    checkout_with = host_checkout.get("with")
    assert isinstance(checkout_with, dict)
    assert checkout_with.get("repository") == "NousResearch/hermes-agent"
    assert checkout_with.get("ref") == "3c27eb6234bf91b8ceee9e9071591b31e9b148cb"
    host_build = _workflow_step(host_job, "Build supported compatibility environment")
    host_build_script = host_build.get("run")
    assert isinstance(host_build_script, str)
    assert (
        host_build_script.count('uv pip check --python "${RUNNER_TEMP}/supported-host/bin/python"')
        == 1
    )
    assert "--upgrade" not in host_build_script
    host_audit = _workflow_step(host_job, "Record supported-host dependency observations")
    assert "if" not in host_audit
    assert "continue-on-error" not in host_audit
    host_script = host_audit.get("run")
    assert isinstance(host_script, str)
    host_audit_command = (
        'if pip-audit --progress-spinner off --format json --output "${audit_report}" \\\n'
        '  --path "${RUNNER_TEMP}/supported-host/lib/python3.12/site-packages"; then\n'
    )
    parser_boundary = (
        'AUDIT_EXIT="${audit_exit}" AUDIT_REPORT="${audit_report}" \\\n'
        '  HOST_SITE_PACKAGES="${RUNNER_TEMP}/supported-host/lib/python3.12/site-packages" \\\n'
        "  python - <<'PY'\nimport importlib.metadata\n"
    )
    assert host_audit_command in host_script
    assert parser_boundary in host_script
    assert host_script.rstrip().endswith("PY")
    for soft_failure in ("|| true", "; true", "set +e", "exit 0"):
        assert soft_failure not in host_script
    _assert_terms(
        host_script,
        "if pip-audit --progress-spinner off --format json --output",
        '--path "${RUNNER_TEMP}/supported-host/lib/python3.12/site-packages"',
        'data = json.loads(report.read_text(encoding="utf-8"))',
        "if not isinstance(dependencies, list) or not dependencies:",
        "if normalized_name in reported_names:",
        'allowed_skip_names = {"better-hermes-hindsight", "hermes-agent"}',
        "unexpected unaudited host package",
        "if not isinstance(advisory_id, str) or not advisory_id:",
        "if reported_names != installed_names:",
        "reported version mismatch for",
        "expected_exit = 1 if vulnerability_count else 0",
        "if audit_exit != expected_exit:",
        "raise SystemExit(audit_exit or 1)",
        "::warning title=Supported Hermes dependency observations",
        "GITHUB_STEP_SUMMARY",
    )

    compatibility = _read("docs/compatibility.md")
    checklist = _read("docs/public-release-checklist.md")
    for document in (compatibility, checklist):
        _assert_terms(
            document,
            "rolling compatibility policy",
            "current stable Hermes release",
            "historical characterization",
            "not a runtime prerequisite",
            "complete runtime dependency closure",
            "required lifecycle",
        )


def test_supported_host_observation_validates_complete_audit_reports(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    metadata = site_packages / "better_hermes_hindsight-1.2.3.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: better-hermes-hindsight\nVersion: 1.2.3\n",
        encoding="utf-8",
    )
    validator = _host_audit_validator_script()
    clean_dependency = {"name": "better-hermes-hindsight", "version": "1.2.3", "vulns": []}
    advisory = {
        "id": "TEST-1",
        "aliases": ["CVE-TEST"],
        "fix_versions": ["1.2.4"],
        "description": "synthetic advisory",
    }

    def run_case(
        name: str, payload: object, *, audit_exit: int
    ) -> subprocess.CompletedProcess[str]:
        report = tmp_path / f"{name}.json"
        summary = tmp_path / f"{name}.summary"
        report.write_text(json.dumps(payload), encoding="utf-8")
        environment = {
            **os.environ,
            "AUDIT_EXIT": str(audit_exit),
            "AUDIT_REPORT": str(report),
            "HOST_SITE_PACKAGES": str(site_packages),
            "GITHUB_STEP_SUMMARY": str(summary),
        }
        return subprocess.run(
            [sys.executable, "-c", validator],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )

    clean = run_case(
        "clean",
        {"dependencies": [clean_dependency], "fixes": []},
        audit_exit=0,
    )
    assert clean.returncode == 0
    assert "::warning" not in clean.stdout

    finding = run_case(
        "finding",
        {
            "dependencies": [{**clean_dependency, "vulns": [advisory]}],
            "fixes": [],
        },
        audit_exit=1,
    )
    assert finding.returncode == 0
    assert "::warning title=Supported Hermes dependency observations" in finding.stdout

    skipped = run_case(
        "skipped",
        {
            "dependencies": [
                {
                    "name": "better-hermes-hindsight",
                    "skip_reason": "Dependency not found on PyPI: better-hermes-hindsight (1.2.3)",
                }
            ],
            "fixes": [],
        },
        audit_exit=0,
    )
    assert skipped.returncode == 0

    rejected = {
        "empty": ({"dependencies": [], "fixes": []}, 0),
        "invalid-advisory": (
            {"dependencies": [{**clean_dependency, "vulns": [{}]}], "fixes": []},
            1,
        ),
        "incomplete": (
            {
                "dependencies": [{"name": "other", "version": "1", "vulns": []}],
                "fixes": [],
            },
            0,
        ),
        "wrong-version": (
            {
                "dependencies": [{"name": "better-hermes-hindsight", "version": "9", "vulns": []}],
                "fixes": [],
            },
            0,
        ),
        "wrong-skip-version": (
            {
                "dependencies": [
                    {
                        "name": "better-hermes-hindsight",
                        "skip_reason": "not on PyPI (9)",
                    }
                ],
                "fixes": [],
            },
            0,
        ),
        "tool-error": (
            {"dependencies": [{**clean_dependency, "vulns": [advisory]}], "fixes": []},
            2,
        ),
    }
    for name, (payload, audit_exit) in rejected.items():
        result = run_case(name, payload, audit_exit=audit_exit)
        assert result.returncode != 0, name


def test_unrelated_host_findings_are_informational_without_plugin_reachability() -> None:
    contract = "\n".join(
        _read(path)
        for path in (
            "IMPLEMENTATION.md",
            "docs/audit-findings.md",
            "docs/compatibility.md",
            "docs/installation.md",
            "docs/public-release-checklist.md",
        )
    )

    _assert_terms(
        contract,
        "v2026.8.3",
        "cryptography==48.0.1",
        "PYSEC-2026-3552",
        "PYSEC-2026-3553",
        "PYSEC-2026-3554",
        "informational upstream",
        "do not block Task 7 or public release",
        "does not declare or import `cryptography`",
        "no allowlist or dependency override",
        "0.1.0a1` is a reviewed development prerelease candidate",
        "publication remain separately authorized",
    )
    plugin_job = _workflow_job("project-dependency-security")
    for name in (
        "Audit Better Hindsight runtime/build dependency closure",
        "Audit locked project-owned dependency closure",
    ):
        step = _workflow_step(plugin_job, name)
        assert "if" not in step
        assert "continue-on-error" not in step
        assert "--ignore-vuln" not in _shell_tokens(step)
    host_audit = _workflow_step(
        _workflow_job("supported-hermes-observation"),
        "Record supported-host dependency observations",
    )
    host_script = host_audit.get("run")
    assert isinstance(host_script, str)
    assert "--ignore-vuln" not in host_script
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    assert project["dependencies"] == ["hindsight-client==0.8.5"]
    lock = tomllib.loads(_read("uv.lock"))
    cryptography_versions = [
        package["version"] for package in lock["package"] if package["name"] == "cryptography"
    ]
    assert cryptography_versions == ["50.0.0"]
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src").rglob("*.py"))
    assert "cryptography" not in source.casefold()


def test_first_prerelease_metadata_and_operator_paths_are_consistent() -> None:
    version = tomllib.loads(_read("pyproject.toml"))["project"]["version"]
    assert version == "0.1.0a1"

    for manifest_path in (
        "plugin.yaml",
        "src/better_hermes_hindsight/hermes_plugin/plugin.yaml",
    ):
        manifest = yaml.safe_load(_read(manifest_path))
        assert isinstance(manifest, dict)
        assert manifest["version"] == version

    release_notes = _read("docs/releases/0.1.0a1.md")
    changelog = _read("CHANGELOG.md")
    installation = _read("docs/installation.md")
    security = _read("SECURITY.md")
    _assert_terms(release_notes, "0.1.0a1", "limitations", "rollback", "Hindsight 0.8.5")
    _assert_terms(changelog, "[0.1.0a1] - 2026-08-10", "Added", "Security")
    _assert_terms(security, "0.1.0a1", "prerelease", "not supported for production")
    assert "better_hermes_hindsight-0.0.0" not in installation
    assert installation.count("better_hermes_hindsight-0.1.0a1-py3-none-any.whl") == 2

    sdist_manifest = _read("MANIFEST.in")
    _assert_terms(
        sdist_manifest,
        "include *.md",
        "include *.py",
        "include uv.lock",
        "recursive-include .github *.yml",
        "recursive-include docs *.md",
        "recursive-include tests *.py",
    )


def test_public_source_scan_excludes_external_compatibility_checkouts(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("public\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/plugin.py").write_text("public\n", encoding="utf-8")
    external = tmp_path / ".compat/hermes-current"
    external.mkdir(parents=True)
    (external / "upstream.py").write_text("external\n", encoding="utf-8")

    relative_paths = {
        path.relative_to(tmp_path) for path in _iter_public_source_text_paths(tmp_path)
    }

    assert relative_paths == {Path("README.md"), Path("src/plugin.py")}


def test_public_source_has_no_tool_truncation_artifacts() -> None:
    truncation_artifact = "..." + "[truncated]"
    for path in _iter_public_source_text_paths(ROOT):
        relative = path.relative_to(ROOT)
        assert truncation_artifact not in path.read_text(encoding="utf-8"), relative


def test_prerelease_publication_is_manual_tag_bound_and_environment_gated() -> None:
    workflow_path = ".github/workflows/prerelease.yml"
    raw_workflow = cast(object, yaml.safe_load(_read(workflow_path)))
    assert isinstance(raw_workflow, dict)
    workflow = cast(dict[object, object], raw_workflow)

    trigger = workflow.get(True)  # PyYAML 1.1 parses the unquoted `on` key as true.
    assert isinstance(trigger, dict)
    assert set(trigger) == {"workflow_dispatch"}
    permissions = workflow.get("permissions")
    assert permissions == {"contents": "read"}

    build = _workflow_job("build-candidate", workflow=workflow_path)
    build_if = build.get("if")
    assert isinstance(build_if, str)
    _assert_terms(build_if, "github.ref_type == 'tag'", "inputs.expected_commit == github.sha")
    identity = _workflow_step(build, "Verify immutable prerelease identity")
    identity_script = identity.get("run")
    assert isinstance(identity_script, str)
    _assert_terms(
        identity_script,
        'version != "0.1.0a1"',
        'os.environ["GITHUB_REF_NAME"] != f"v{version}"',
    )
    assert "packaging" not in identity_script
    upload = _workflow_step(build, "Upload immutable candidate artifacts")
    assert upload["uses"] == ("actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a")
    build_step = _workflow_step(build, "Run release gates and build distributions")
    build_script = build_step.get("run")
    assert isinstance(build_script, str)
    _assert_terms(
        build_script,
        ".venv/bin/python -m ruff check src tests __init__.py cli.py",
        ".venv/bin/python -m ruff format --check src tests __init__.py cli.py",
        "cd dist",
        "sha256sum ./*.whl ./*.tar.gz > SHA256SUMS",
    )
    assert ".venv/bin/python -m ruff check ." not in build_script
    assert ".venv/bin/python -m ruff format --check ." not in build_script
    assert "sha256sum dist/" not in build_script

    publish = _workflow_job("publish-pypi", workflow=workflow_path)
    assert publish.get("needs") == ["build-candidate", "validate-publish-configuration"]
    publish_if = publish.get("if")
    assert isinstance(publish_if, str)
    assert "inputs.publish_pypi == true" in publish_if
    assert publish.get("environment") == {"name": "pypi"}
    assert publish.get("permissions") == {"contents": "read", "id-token": "write"}
    publish_step = _workflow_step(publish, "Publish exact candidate to PyPI")
    assert publish_step["uses"] == (
        "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
    )
    checksum_step = _workflow_step(publish, "Verify candidate checksums")
    checksum_script = checksum_step.get("run")
    assert isinstance(checksum_script, str)
    assert "cd candidate && sha256sum --check SHA256SUMS" in checksum_script

    validation = _workflow_job("validate-publish-configuration", workflow=workflow_path)
    assert validation.get("needs") == "build-candidate"
    validation_if = validation.get("if")
    assert isinstance(validation_if, str)
    assert "inputs.publish_pypi == true" in validation_if
    validation_step = _workflow_step(
        validation,
        "Fail closed until PyPI and environment protections are configured",
    )
    validation_script = validation_step.get("run")
    assert isinstance(validation_script, str)
    _assert_terms(
        validation_script,
        "PYPI_RELEASE_CONFIGURED",
        "protecting the pypi environment",
        "PyPI trusted publishing",
    )
