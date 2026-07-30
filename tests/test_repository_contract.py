"""Tests for the active best-effort product and compatibility contracts."""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from markdown_it import MarkdownIt
from markdown_it.token import Token

ROOT = Path(__file__).resolve().parents[1]
LOCAL_PLAN_PATH = ROOT / ".hermes/plans/2026-07-27_071437-best-effort-plugin.md"
LOCAL_PLAN_INDEX_PATH = ROOT / ".hermes/plans/README.md"
LOCAL_PLAN_INDEX_SHA256 = "c6a8ec1e9b398cbf16624fc373d6b04a27763bf1d3abd350e0bfe7264f990a47"

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
    "docs/operations.md",
    "docs/public-release-checklist.md",
    "src/better_hermes_hindsight/config.py",
    "src/better_hermes_hindsight/hermes_plugin/cli.py",
)

_TASK4_FROZEN_AUTHORITY_SHA256 = {
    "IMPLEMENTATION.md": "264b95734ddac82c6c5b7278b6caf7897dd5dd9e991390d9b2f90af86d29a66e",
    "README.md": "adec2a160ced49b710250594c64b6e63b2e6904dad932ed6af409653507ee7ca",
    "DESIGN.md": "30cf941f2399f10fd4e2ae0fcaf955e7d675810664fb5bfaa02fdca47773622c",
    "docs/audit-findings.md": "af1134c0772062eacb7185018a0b2585260cd79955974a5acbf145c2fc63fba2",
    "docs/compatibility.md": "20c2699d734e275ed7401ab50c7ddd275897902d8fd6ae0d786da749f8e3b14c",
    "docs/configuration.md": "08f660c7e8f311640a26b495ef160e187137156fc6632b37d7bb180b64a975d5",
    "docs/operations.md": "7fe0cee6645dd5d5cdad110b013695289af72e093630dfb82724a3d2e4b7bfb0",
    "docs/public-release-checklist.md": (
        "09b75deb1eb5376b8c0f4f816b74ace9bcc517149334997036dea508b8c19d54"
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


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).casefold()


def _assert_terms(text: str, *terms: str) -> None:
    normalized = _normalized(text)
    missing = [term for term in terms if term.casefold() not in normalized]
    assert not missing, f"missing repository contract terms: {missing}"


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
        "local durability starts only after provider admission",
        "no direct-user provenance claim",
        "no pre-return or no-loss guarantee",
        "no Hermes-core prerequisite",
        "`codex_app_server` is unsupported on the pinned release",
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
        "sender delivery is implemented",
        "completed sender-delivery checkpoint",
        "retention remains disabled by default",
        "managed installation",
        "isolated live-write proof remain incomplete",
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
        "retired plans",
        "2026-07-25_194157-better-hermes-hindsight-implementation.md",
        "2026-07-27_055353-plugin-only-rescope.md",
        "must never drive implementation",
        "passed independent specification, quality, and adversarial review",
        "Write deterministic RED tests",
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
        "no Hermes core patch or patched SHA",
        "released `sync_turn()` callback",
        "durability begins only after the provider admission commit",
        "no direct-user provenance",
        "no pre-return or no-loss guarantee",
        "`codex_app_server` remains unsupported",
        "retention is disabled by default",
        "no model-facing memory tools",
        "isolated Hindsight instance and Hermes profile",
        "separate canary instance and bank",
        "preserves the old deployment",
    )


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
        "9843a9b802ce54b5483a2adb7e95aff989d1df0f",
        "passed independent specification, quality, and adversarial review",
        "Write deterministic RED tests",
    )
    normalized_router = _normalized(router)
    for premature_completion in (
        "Task 5 is complete",
        "Tasks 0–5 are complete",
        "completed Task 5",
    ):
        assert premature_completion.casefold() not in normalized_router

    local_plan_pair = _read_local_plan_pair()
    if local_plan_pair is None:
        pytest.skip("ignored local planning aids are absent from this clean checkout")
    plan_bytes, plan_index_bytes = local_plan_pair
    plan = plan_bytes.decode("utf-8", errors="strict")
    plan_index = plan_index_bytes.decode("utf-8", errors="strict")
    active_match = re.search(r"Canonical plan:\*\* `([^`]+)`", router)
    hash_match = re.search(r"Canonical SHA-256:\*\* `([0-9a-f]{64})`", router)
    index_hash_match = re.search(r"SHA-256: `([0-9a-f]{64})`", plan_index)
    assert active_match is not None
    assert hash_match is not None
    assert index_hash_match is not None
    assert active_match.group(1).endswith("2026-07-27_071437-best-effort-plugin.md")
    assert hash_match.group(1) == index_hash_match.group(1)
    _assert_terms(plan_index, "9843a9b", "active-plan Task 5")

    active_path = ROOT / active_match.group(1)
    assert active_path == LOCAL_PLAN_PATH
    assert hashlib.sha256(plan_bytes).hexdigest() == hash_match.group(1)
    crlf_plan_bytes = plan_bytes.replace(b"\n", b"\r\n")
    assert crlf_plan_bytes != plan_bytes
    assert hashlib.sha256(crlf_plan_bytes).hexdigest() != hash_match.group(1)
    assert "ACTIVE — CANONICAL IMPLEMENTATION PLAN" in plan[:1000]

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


def test_task4_completed_implementation_and_task5_route_are_frozen() -> None:
    router = _read("IMPLEMENTATION.md")
    _assert_terms(
        router,
        "bb575de8723f2a2d70054700c2713d0154ae59181cd5546ee611ecae328ddf62",
        "Tasks 0–4 are complete",
        "0bc681cf6ab066bce6a9793c9d72157886aae2e4",
        "9843a9b802ce54b5483a2adb7e95aff989d1df0f",
        "review-amended Task 5 contract",
        "approved `0.8.5`/`0.6.1` operator package transition",
        "scanner-proof nested transaction trees",
        "root-safe owned checked-hash bytecode",
        "exact command outcomes and host-module provenance",
        "retry-convergent cleanup/uninstall state machines",
        "passed independent specification, quality, and adversarial review",
        "6652212a5aaa72833e3df050523652fa3b935583",
        "Write deterministic RED tests",
        "Do not write production installer code until those tests fail",
        "Do not install into a live Hermes home",
    )
    router_status = _extract_heading_section_at(
        router,
        tag="h2",
        ordinal=1,
        title="Current status",
        ancestry=_IMPLEMENTATION_ROOT_ANCESTRY,
    )
    router_sha = _extract_list_item_at(
        router_status,
        parent_ordinal=0,
        parent_type="bullet_list_open",
        item_ordinal=1,
        expected_text="Canonical SHA-256",
    )
    _assert_exact_normalized(
        router_sha,
        """- **Canonical SHA-256:**
        `bb575de8723f2a2d70054700c2713d0154ae59181cd5546ee611ecae328ddf62`""",
    )
    router_state = _extract_list_item_at(
        router_status,
        parent_ordinal=0,
        parent_type="bullet_list_open",
        item_ordinal=2,
        expected_text="Plan state",
    )
    _assert_exact_normalized(
        router_state,
        """- **Plan state:** Active; Tasks 0–4 are complete. The Task 4 contract and SQLite WAL/SHM
        amendment remain checkpointed as `9579d8af0098899cdb0ebe3447c2bb57fb4519da` and
        `0bc681cf6ab066bce6a9793c9d72157886aae2e4`; the exact implementation passed independent
        specification and adversarial review before checkpoint. The review-amended Task 5 contract
        combines the approved `0.8.5`/`0.6.1` operator package transition with scanner-proof nested
        transaction trees, root-safe owned checked-hash bytecode, exact command outcomes and
        host-module provenance, and phase-complete retry-convergent cleanup/uninstall state
        machines. That contract passed independent specification, quality, and adversarial review
        and
        is checkpointed as `6652212a5aaa72833e3df050523652fa3b935583`.""",
    )
    router_next = _extract_list_item_at(
        router_status,
        parent_ordinal=0,
        parent_type="bullet_list_open",
        item_ordinal=4,
        expected_text="Next action",
    )
    _assert_exact_normalized(
        router_next,
        """- **Next action:** Write deterministic RED tests for Task 5 transactional
        install/publication, upgrade rollback, ownership refusal, uninstall/retry cleanup, archive
        contents, released-host discovery/health, and Better `0.8.5` versus bundled `0.6.1` version
        agreement. Do not write production installer code until those tests fail for the intended
        missing behavior. Do not install into a live Hermes home/interpreter, invoke pip from the
        shim
        manager, select a provider, restart Hermes, touch profile configuration/outboxes, contact
        Hindsight, deploy, or roll out production.""",
    )
    assert "Independent approval is pending" not in router
    assert "Only after approval, write deterministic RED tests" not in router

    readme = _read("README.md")
    _assert_terms(
        readme,
        "completed sender-delivery checkpoint",
        "completed diagnostics/mission-command checkpoint",
        "approved managed-installation Task 5 contract",
        "active deterministic RED-test stage",
    )
    readme_authority = _extract_heading_section_at(
        readme,
        tag="h2",
        ordinal=6,
        title="Repository and implementation authority",
        ancestry=_README_ROOT_ANCESTRY,
    )
    readme_authority_owner = _extract_paragraph_at(
        readme_authority,
        ordinal=0,
        expected_text="The tracked implementation router identifies the canonical local plan",
    )
    _assert_exact_normalized(
        readme_authority_owner,
        """The tracked [implementation router](IMPLEMENTATION.md) identifies the canonical local
        plan, its hash, the completed sender-delivery checkpoint, the completed
        diagnostics/mission-command
        checkpoint, the approved managed-installation Task 5 contract and active deterministic
        RED-test stage, and two explicitly retired plans. Never infer implementation requirements
        from a retired plan or from stale proof wording.
        The separate Hermes-core worktree is frozen research and must not be imported, installed,
        committed, or treated as a prerequisite.""",
    )
    assert "active managed-installation Task 5 review stage" not in readme


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
        "bb575de8723f2a2d70054700c2713d0154ae59181cd5546ee611ecae328ddf62",
        "Tasks 0–4 are complete",
        "review-amended active-plan Task 5 contract",
        "approved explicit `0.8.5`/`0.6.1` transition",
        "scanner-proof nested transactions",
        "root-safe checked-hash bytecode",
        "exact CLI outcomes and host-module provenance",
        "retry-convergent cleanup/uninstall state machines",
        "independently approved",
        "contract checkpoint `6652212`",
        "write deterministic RED tests",
        "9843a9b",
    )
    plan_index_active = _extract_heading_section_at(
        plan_index,
        tag="h2",
        ordinal=1,
        title="Active canonical plan",
        ancestry=_PLAN_INDEX_ROOT_ANCESTRY,
    )
    plan_index_sha = _extract_list_item_at(
        plan_index_active,
        parent_ordinal=1,
        parent_type="bullet_list_open",
        item_ordinal=0,
        expected_text="SHA-256",
    )
    _assert_exact_normalized(
        plan_index_sha,
        """- SHA-256:
        `bb575de8723f2a2d70054700c2713d0154ae59181cd5546ee611ecae328ddf62`""",
    )
    plan_index_state = _extract_list_item_at(
        plan_index_active,
        parent_ordinal=1,
        parent_type="bullet_list_open",
        item_ordinal=1,
        expected_text="State",
    )
    _assert_exact_normalized(
        plan_index_state,
        """- State: active; Tasks 0–4 are complete; the review-amended active-plan Task 5 contract
        combines the approved explicit `0.8.5`/`0.6.1` transition with scanner-proof nested
        transactions, root-safe checked-hash bytecode, exact CLI outcomes and host-module
        provenance,
        and retry-convergent cleanup/uninstall state machines; it is independently approved and
        tracked at contract checkpoint `6652212`""",
    )
    plan_index_next = _extract_list_item_at(
        plan_index_active,
        parent_ordinal=1,
        parent_type="bullet_list_open",
        item_ordinal=3,
        expected_text="Next action",
    )
    _assert_exact_normalized(
        plan_index_next,
        """- Next action: write deterministic RED tests for Task 5 transactional
        install/publication, upgrade rollback, ownership refusal, uninstall/retry cleanup, archive
        contents, released-host
        discovery/health, and Better `0.8.5` versus bundled `0.6.1` version agreement; do not write
        production installer code or touch a live Hermes home, profile, service, interpreter, or
        Hindsight instance""",
    )
    assert "independent approval is pending" not in plan_index
    assert "independently approve and checkpoint" not in plan_index


def test_local_task5_managed_installation_contract_is_frozen_when_present() -> None:
    local_plan_pair = _read_local_plan_pair()
    if local_plan_pair is None:
        pytest.skip("ignored local planning aids are absent from this clean checkout")
    plan_bytes, _plan_index_bytes = local_plan_pair
    plan = plan_bytes.decode("utf-8", errors="strict")
    task5 = _extract_heading_section_at(
        plan,
        tag="h3",
        ordinal=16,
        title="Task 5: Add managed installation, health checks, and version-aware rollback",
        ancestry=_PLAN_TASK_ANCESTRY,
    )

    assert task5.count("**Frozen managed-installation contract**") == 1
    _assert_terms(
        task5,
        "version-aware rollback",
        "python -m better_hermes_hindsight.install {install,health,uninstall}",
        "Host-owned argparse usage errors emit no JSON on stdout",
        'json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)',
        "exactly the string returned by `importlib.metadata.version",
        "The operation/state mapping is exact",
        "`install`; exact current-resource target",
        "`already_installed`",
        "`health`; exact current target passes every check",
        "`healthy`",
        "`artifact_mismatch`",
        "`not_installed`",
        "No other public outcome is allowed",
        "there is no implicit live-home default",
        "`$HERMES_HOME/plugins/better_hindsight`",
        "`$HERMES_HOME/plugins/.better-hindsight-transactions`",
        "`stage-<32 lowercase hex>`",
        "fixed `rollback`",
        "`failed-<32 lowercase hex>`",
        "fixed `uninstalling`",
        "contains only a child named `tree`",
        "general plugin registry reaches the wrapper at its recursion cap",
        "never visits `tree/plugin.yaml`",
        "fsyncs both changed source and destination directories",
        "manifest path is the canonical target",
        "Provider-name equality alone is insufficient provenance",
        "shim manager never invokes pip/uv",
        "same-interpreter isolated health probe",
        "exactly five direct entries",
        "exactly six generated files",
        "py_compile.PycInvalidationMode.CHECKED_HASH",
        "optimization levels `0/1/2`",
        "final canonical source paths as `dfile`",
        'schema="better-hermes-hindsight-managed-shim-v2"',
        "`python_cache_tag`",
        "`python_magic_hex`",
        "exact nine-entry `files` map",
        "recorded magic equals the first four bytes of every owned cache file",
        "checked-hash flags produced by `PycInvalidationMode.CHECKED_HASH`",
        "All nine regular owned files and the marker use mode `0444`",
        "complete `tree` and `__pycache__` use `0555`",
        "transaction root/wrappers use `0700`",
        "Same-version artifact-A/target versus artifact-B/wheel source/manifest drift",
        "strictly newer installed PEP 440 version",
        "supported recorded cache tag/magic differs",
        "this is the only same-version replacement",
        "root-capable released-provider and CLI discovery test",
        "write-trap test",
        "Deterministic cleanup primitive",
        "source/marker/cache path belongs to its recorded stage allowlist",
        "changes `tree` from `0555` to `0700`",
        "removes the ownership marker last",
        "cleanup residues may contain exact subsets only",
        "Publication state machine",
        "destination wrapper is created empty at `0700`",
        "fsynced before a rename enters it",
        "Upgrade canonical→`rollback/tree` rename fails before moving",
        "rename is visible but either changed-directory fsync fails",
        "Stage-tree→canonical is visible",
        "New canonical import/discovery/health verification fails",
        "This is commit",
        "`backup_cleanup_failed`",
        "Fault tests inject every write/chmod/rename/source-fsync/destination-fsync",
        "fresh `sys.executable -I` child",
        "exact `Distribution.files` membership",
        "`hermes_cli.plugins.__file__`",
        "`tools.lazy_deps.__file__`",
        "must each equal their exact `hermes-agent` distribution member and bytes",
        "directly invokes that provenance-bound `hermes_cli.plugins.PluginManager._scan_directory`",
        "Without running global discovery or reading config",
        "zero managed-tree mutation",
        "bundled lazy requirement is exactly `hindsight-client==0.6.1`",
        "Uninstall state machine",
        "fsyncing `plugins` after root creation",
        "transaction root after wrapper creation",
        "rename fails before moving",
        "If the move is visible but either directory fsync fails",
        "restores `tree`→canonical",
        "successful rename plus both fsyncs is the uninstall commit point",
        "full strict mode-`0555` tomb tree",
        "With canonical target absent",
        "marker-absent empty mode `0700`",
        "empty mode-`0700` `uninstalling` wrapper",
        "transaction root is itself empty after wrapper removal",
        "fsyncs/removes the root and fsyncs `plugins`",
        "next visible state remains accepted",
        "coexists with a strict canonical target after failed precommit destination cleanup",
        "state is never treated as committed removal",
        "`uninstall_cleanup_failed`",
        "One interpreter cannot satisfy Better's `hindsight-client==0.8.5`",
        "docs never call this configuration-only",
        "both bundled lazy-install branches",
        "THIRD_PARTY_NOTICES.md",
        "Generated bytecode/marker are runtime-managed and absent from archives",
        "`src/better_hermes_hindsight/install.py`, `docs/installation.md`, and `docs/rollback.md`",
        "all three public help/guide owners",
        "unconditional tracked authority path inventory",
        "clean-checkout mutation tests",
        "literal optional index SHA-256 assertion",
        "corresponding repository-oracle hashes/state assertions",
        "None of install/no-op/upgrade/health/failure/uninstall edits Hermes",
    )

    product_boundary = _extract_heading_section_at(
        plan,
        tag="h2",
        ordinal=8,
        title="3. Product boundary",
        ancestry=_PLAN_ROOT_ANCESTRY,
    )
    product_rollback = _extract_table_row_at(
        product_boundary,
        parent_ordinal=0,
        table_text="CapabilityBest-effort first prerelease",
        row_ordinal=19,
        row_text="preserve Better outbox and both banks",
    )
    _assert_exact_normalized(
        product_rollback,
        """| Rollback | Stop Hermes; uninstall the verified shim while the Better wheel still
        supplies the command; select bundled `hindsight`; remove the Better wheel; restore client
        `0.6.1`; restart; preserve Better outbox and both banks |""",
    )
    _assert_terms_in_order(
        product_rollback,
        "Stop Hermes",
        "uninstall the verified shim while the Better wheel still supplies the command",
        "select bundled `hindsight`",
        "remove the Better wheel",
        "restore client `0.6.1`",
        "restart",
    )

    task5_rollback = _extract_paragraph_at(
        task5,
        ordinal=28,
        expected_text="Version-aware bundled rollback.",
    )
    _assert_exact_normalized(
        task5_rollback,
        """**Version-aware bundled rollback.** One interpreter cannot satisfy Better's
        `hindsight-client==0.8.5` and released Hermes's exact bundled `hindsight-client==0.6.1`
        simultaneously. With Hermes stopped, the operator first uninstalls the verified Better shim
        while the Better wheel still supplies the command, changes provider configuration to bundled
        `hindsight`, removes the Better wheel, explicitly restores/verifies `0.6.1`, then
        restarts and
        proves bundled recall. Returning to Better stops Hermes, installs the Better wheel and exact
        `0.8.5`, installs/health-checks the shim, selects `better_hindsight`, and restarts.
        Docs never
        call this configuration-only or rely on bundled lazy installation; Better's outbox and both
        deployments/banks remain. Tests intercept both bundled lazy-install branches under `0.8.5`
        and prove the explicit `0.6.1` transition needs no package-manager call.""",
    )
    assert task5_rollback.count("Returning to Better") == 1
    rollback_forward, rollback_reverse_tail = task5_rollback.split(
        "Returning to Better", maxsplit=1
    )
    rollback_reverse = "Returning to Better" + rollback_reverse_tail
    _assert_terms_in_order(
        rollback_forward,
        "With Hermes stopped",
        "uninstalls the verified Better shim while the Better wheel still supplies the command",
        "changes provider configuration to bundled `hindsight`",
        "removes the Better wheel",
        "restores/verifies `0.6.1`",
        "restarts",
        "proves bundled recall",
    )
    _assert_terms_in_order(
        rollback_reverse,
        "Returning to Better stops Hermes",
        "installs the Better wheel and exact `0.8.5`",
        "installs/health-checks the shim",
        "selects `better_hindsight`",
        "restarts",
    )

    task5_step2 = _extract_between_paragraphs_at(
        task5,
        start_ordinal=31,
        start_text="Step 2: Write failing transactional ownership tests",
        start_source="**Step 2: Write failing transactional ownership tests**",
        end_ordinal=34,
        end_text="Step 3: Document activation and version-aware rollback",
        end_source="**Step 3: Document activation and version-aware rollback**",
    )
    sdk_order_bullet = _extract_list_item_at(
        task5_step2,
        parent_ordinal=1,
        parent_type="bullet_list_open",
        item_ordinal=9,
        expected_text="order-sensitive tests require",
    )
    _assert_exact_normalized(
        sdk_order_bullet,
        """- Better `0.8.5` and bundled `0.6.1` cannot be operationally co-satisfied:
        order-sensitive tests require stopped Hermes → verified shim uninstall while its command
        exists → bundled selection → Better wheel removal → explicit `0.6.1` restore; both bundled
        lazy-install branches are intercepted, while that transition permits bundled first use
        without package-manager invocation;""",
    )
    _assert_terms_in_order(
        sdk_order_bullet,
        "stopped Hermes",
        "verified shim uninstall while its command exists",
        "bundled selection",
        "Better wheel removal",
        "explicit `0.6.1` restore",
    )

    task5_step3 = _extract_between_paragraphs_at(
        task5,
        start_ordinal=34,
        start_text="Step 3: Document activation and version-aware rollback",
        start_source="**Step 3: Document activation and version-aware rollback**",
        end_ordinal=39,
        end_text="Step 4: Verify and checkpoint",
        end_source="**Step 4: Verify and checkpoint**",
    )
    step3_numbered_order = _extract_between_paragraphs_at(
        task5_step3,
        start_ordinal=1,
        start_text="Its exact order is:",
        start_source=(
            "Rollback to released bundled `hindsight` is not configuration-only. "
            "Its exact order is:"
        ),
        end_ordinal=3,
        end_text="Returning to Better reverses the package boundary",
        end_source=(
            "Returning to Better reverses the package boundary while Hermes is stopped: install "
            "the Better wheel and exact `hindsight-client==0.8.5`, install and health-check the "
            "managed shim, select `better_hindsight`, restart, and verify recall before separately "
            "re-enabling "
            "retention. Neither direction migrates or deletes a bank/outbox."
        ),
    )
    _assert_exact_normalized(
        step3_numbered_order,
        """1. stop the active Hermes process and verify no old Better sender owns the profile lock;
        2. while the Better wheel and `hindsight-client==0.8.5` still supply a healthy command,
        uninstall only the verified managed shim;
        3. select bundled `hindsight` and its preserved URL/key/bank configuration while Hermes
        remains stopped;
        4. explicitly remove the Better wheel, restore `hindsight-client==0.6.1` in the same Hermes
        interpreter, and verify the exact installed version without relying on Hermes lazy
        installation;
        5. restart and verify bundled recall without a package-manager call;
        6. preserve Better's outbox/instance and both banks for diagnosis or later replay.""",
    )
    assert re.findall(r"(?m)^(\d+)\. ", step3_numbered_order) == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
    ]
    _assert_terms_in_order(
        step3_numbered_order,
        "1. stop the active Hermes process",
        "2. while the Better wheel and `hindsight-client==0.8.5` still supply a healthy command",
        "3. select bundled `hindsight`",
        "4. explicitly remove the Better wheel",
        "restore `hindsight-client==0.6.1`",
        "5. restart and verify bundled recall",
        "6. preserve Better's outbox/instance and both banks",
    )
    step3_reverse = _extract_paragraph_at(
        task5_step3,
        ordinal=3,
        expected_text="Returning to Better reverses the package boundary",
    )
    _assert_exact_normalized(
        step3_reverse,
        """Returning to Better reverses the package boundary while Hermes is stopped: install the
        Better wheel and exact `hindsight-client==0.8.5`, install and health-check the managed shim,
        select `better_hindsight`, restart, and verify recall before separately re-enabling
        retention.
        Neither direction migrates or deletes a bank/outbox.""",
    )
    _assert_terms_in_order(
        step3_reverse,
        "Returning to Better reverses the package boundary while Hermes is stopped",
        "install the Better wheel and exact `hindsight-client==0.8.5`",
        "install and health-check the managed shim",
        "select `better_hindsight`",
        "restart",
        "verify recall",
        "re-enabling retention",
    )

    task5_step4 = _extract_between_paragraphs_at(
        task5,
        start_ordinal=39,
        start_text="Step 4: Verify and checkpoint",
        start_source="**Step 4: Verify and checkpoint**",
        end_ordinal=41,
        end_text="Checkpoint:",
        end_source="Checkpoint:",
    )
    step4_verification = _extract_paragraph_at(
        task5_step4,
        ordinal=0,
        expected_text="Build wheel/sdist",
    )
    _assert_exact_normalized(
        step4_verification,
        """Build wheel/sdist in disposable output, run twine checks, inspect exact archive members,
        install each artifact independently into fresh virtual environments with exact released
        Hermes,
        and run discovery/CLI/health/transaction/residue tests under temporary `HERMES_HOME` values.
        Run the no-bytecode-drift proof in a root-capable released-Hermes container as well as the
        deterministic writer trap. In disposable environments prove both explicit SDK states: Better
        with `0.8.5`, then bundled first use with `0.6.1` and no lazy package-manager call, then the
        reverse Better reinstall. Verify `src/better_hermes_hindsight/install.py`,
        `docs/installation.md`, and `docs/rollback.md` are in the unconditional tracked
        authority/hash
        corpus, the optional local plan index raw hash is pinned when present, and clean-checkout
        mutation tests still run with ignored plans absent. No package transition runs against a
        live
        Hermes interpreter.""",
    )
    _assert_terms_in_order(
        step4_verification,
        "Better with `0.8.5`",
        "then bundled first use with `0.6.1`",
        "then the reverse Better reinstall",
    )

    acceptance = _extract_heading_section_at(
        plan,
        tag="h2",
        ordinal=20,
        title="6. Acceptance criteria",
        ancestry=_PLAN_ROOT_ANCESTRY,
    )
    acceptance_sdk = _extract_list_item_at(
        acceptance,
        parent_ordinal=1,
        parent_type="ordered_list_open",
        item_ordinal=1,
        expected_text="same-interpreter use requires the documented explicit transition",
    )
    _assert_exact_normalized(
        acceptance_sdk,
        """2. It registers only `better_hindsight`; bundled `hindsight` remains the preserved
        rollback
        target, but same-interpreter use requires the documented explicit transition from Better
        `0.8.5` to bundled `0.6.1` rather than a configuration-only claim.""",
    )
    _assert_terms_in_order(
        acceptance_sdk,
        "Better `0.8.5`",
        "bundled `0.6.1`",
    )

    task6 = _extract_heading_section_at(
        plan,
        tag="h3",
        ordinal=17,
        title="Task 6: Prove writes against an isolated Hindsight development instance",
        ancestry=_PLAN_TASK_ANCESTRY,
    )
    _assert_terms(
        task6,
        "Modify: `tests/test_repository_contract.py`",
        "Refresh the unconditional literal whole-file authority hashes",
        "`docs/operations.md`, `docs/rollback.md`, and `docs/public-release-checklist.md`",
        "retain their clean-checkout mutation coverage",
    )
    task6_rollback = _extract_list_item_at(
        task6,
        parent_ordinal=11,
        parent_type="bullet_list_open",
        item_ordinal=11,
        expected_text="in a disposable stopped Hermes environment",
    )
    _assert_exact_normalized(
        task6_rollback,
        """- in a disposable stopped Hermes environment, uninstall the verified managed shim while
        the Better wheel/`0.8.5` command remains, switch the temporary profile to bundled
        `hindsight`, remove the Better wheel, restore exact `hindsight-client==0.6.1`, prove first
        recall makes no
        lazy package-manager call, and confirm Better's isolated bank/outbox remain intact until
        explicit cleanup.""",
    )
    _assert_terms_in_order(
        task6_rollback,
        "in a disposable stopped Hermes environment",
        "uninstall the verified managed shim while the Better wheel/`0.8.5` command remains",
        "switch the temporary profile to bundled `hindsight`",
        "remove the Better wheel",
        "restore exact `hindsight-client==0.6.1`",
        "prove first recall",
        "confirm Better's isolated bank/outbox remain intact",
    )


def test_task5_rollback_docs_freeze_explicit_sdk_transition() -> None:
    expected = {
        "README.md": (
            "not configuration-only",
            "hindsight-client==0.8.5",
            "bundled `hindsight` requires exact `0.6.1`",
        ),
        "DESIGN.md": (
            "not configuration-only",
            "selects bundled `hindsight`, removes the Better wheel, restores exact",
            "reinstalls the wheel and exact `0.8.5` client",
            "version-aware provider/package rollback",
        ),
        "docs/compatibility.md": (
            "same interpreter cannot run the two providers as a configuration-only switch",
            'tools.lazy_deps.ensure("memory.hindsight", prompt=False)',
            "Importing the bundled provider under `0.8.5` is not rollback proof",
            ".better-hindsight-transactions/<wrapper>/tree",
            "general plugin registry does not skip it",
            "marker-owned checked-hash cache files",
            "released Docker image may run as root",
            "executed `plugins.memory`, `hermes_cli.plugins`, and `tools.lazy_deps`",
            "exact `hermes-agent==0.19.0` distribution members",
        ),
        "docs/public-release-checklist.md": (
            "Rollback is not called configuration-only",
            "shim while the wheel still supplies the command",
            "selects bundled `hindsight`, removes the Better wheel",
            "first recall without lazy package installation",
            "explicit Better `0.8.5` and bundled `0.6.1` SDK states",
            "both released scanner traversal boundaries",
            "root-capable released-Hermes import causes no managed-target mutation",
            "empty uninstall-wrapper, empty transaction-root",
            "Hermes `plugins.memory`, `hermes_cli.plugins`, and `tools.lazy_deps` provenance",
            "tracked installer help owner plus installation and rollback guides",
            "unconditional whole-file authority/hash corpus",
        ),
    }
    for path, terms in expected.items():
        _assert_terms(_read(path), *terms)

    readme = _read("README.md")
    readme_root = _extract_heading_section_at(
        readme,
        tag="h1",
        ordinal=0,
        title="Better Hermes Hindsight",
        ancestry=(),
    )
    readme_transition = _extract_paragraph_at(
        readme_root,
        ordinal=1,
        expected_text="Better Hermes Hindsight is an unofficial Hermes memory provider",
    )
    _assert_exact_normalized(
        readme_transition,
        """Better Hermes Hindsight is an unofficial Hermes memory provider for external/self-hosted
        Hindsight. The provider ID is `better_hindsight`, deliberately distinct from bundled
        `hindsight` so rollback does not require a data migration. On released Hermes 0.19.0 it is
        not
        configuration-only: Better requires `hindsight-client==0.8.5`, while bundled `hindsight`
        requires exact `0.6.1`, so switching providers also requires the documented stopped-process
        package-version transition.""",
    )
    readme_purpose = _extract_heading_section_at(
        readme,
        tag="h2",
        ordinal=1,
        title="What it is for",
        ancestry=_README_ROOT_ANCESTRY,
    )
    readme_rollback = _extract_list_item_at(
        readme_purpose,
        parent_ordinal=1,
        parent_type="bullet_list_open",
        item_ordinal=4,
        expected_text="documented rollback to the bundled provider",
    )
    _assert_exact_normalized(
        readme_rollback,
        """- documented rollback to the bundled provider while Better's outbox and both banks stay
        untouched.""",
    )

    stale_claims = (
        ("README.md", "rollback is a configuration change rather than a data migration"),
        (
            "DESIGN.md",
            "Rollback selects bundled `hindsight`, restarts through ordinary operator procedure",
        ),
        (
            "DESIGN.md",
            "removes the Better shim/wheel, restores bundled `hindsight-client==0.6.1`, "
            "selects bundled `hindsight`",
        ),
        (
            "docs/public-release-checklist.md",
            "Rollback selects bundled `hindsight`, restarts through ordinary operator procedure",
        ),
        (
            "docs/public-release-checklist.md",
            "removes the Better shim/wheel, restores bundled `hindsight-client==0.6.1`, "
            "selects bundled `hindsight`",
        ),
    )
    for path, stale in stale_claims:
        assert _normalized(stale) not in _normalized(_read(path))

    design_rollback_section = _extract_heading_section_at(
        _read("DESIGN.md"),
        tag="h2",
        ordinal=10,
        title="Isolation, canary, and rollback",
        ancestry=_DESIGN_ROOT_ANCESTRY,
    )
    design_rollback = _extract_paragraph_at(
        design_rollback_section,
        ordinal=2,
        expected_text="Rollback preserves the Better outbox plus both banks",
    )
    _assert_exact_normalized(
        design_rollback,
        """Rollback preserves the Better outbox plus both banks, but it is not configuration-only in
        released Hermes 0.19.0. With Hermes stopped, the operator removes the verified Better shim
        while
        its wheel still supplies the command, selects bundled `hindsight`, removes the Better wheel,
        restores exact `hindsight-client==0.6.1`, restarts, and verifies recall. Returning to Better
        reinstalls the wheel and exact `0.8.5` client before shim health and provider selection.""",
    )
    assert design_rollback.count("Returning to Better") == 1
    design_forward, design_reverse_tail = design_rollback.split("Returning to Better", maxsplit=1)
    design_reverse = "Returning to Better" + design_reverse_tail
    _assert_terms_in_order(
        design_forward,
        "With Hermes stopped",
        "removes the verified Better shim while its wheel still supplies the command",
        "selects bundled `hindsight`",
        "removes the Better wheel",
        "restores exact `hindsight-client==0.6.1`",
        "restarts",
        "verifies recall",
    )
    _assert_terms_in_order(
        design_reverse,
        "Returning to Better reinstalls the wheel and exact `0.8.5` client",
        "before shim health",
        "provider selection",
    )

    compatibility_baseline = _extract_heading_section_at(
        _read("docs/compatibility.md"),
        tag="h2",
        ordinal=3,
        title="Frozen version baseline",
        ancestry=_COMPATIBILITY_ROOT_ANCESTRY,
        allowed_raw_html_blocks=_COMPATIBILITY_RAW_HTML_BLOCKS,
    )
    compatibility_transition = _extract_paragraph_at(
        compatibility_baseline,
        ordinal=1,
        expected_text="Released Hermes's bundled provider calls",
    )
    _assert_exact_normalized(
        compatibility_transition,
        """Released Hermes's bundled provider calls
        `tools.lazy_deps.ensure("memory.hindsight", prompt=False)` before constructing its external
        client. That registry pins `hindsight-client==0.6.1` and treats installed `0.8.5` as
        unsatisfied, so first bundled use either attempts a downgrade or fails when lazy
        installation is disabled. Importing the bundled provider under `0.8.5` is not rollback
        proof. Task 5 instead tests an explicit disposable transition to `0.6.1` with no lazy
        package-manager call, and the
        reverse reinstall of Better plus `0.8.5`; neither transition edits Hermes source or migrates
        data.""",
    )
    _assert_terms_in_order(
        compatibility_transition,
        "installed `0.8.5` as unsatisfied",
        "explicit disposable transition to `0.6.1`",
        "reverse reinstall of Better plus `0.8.5`",
    )

    checklist = _read("docs/public-release-checklist.md")
    checklist_rollback = _extract_heading_section_at(
        checklist,
        tag="h2",
        ordinal=5,
        title="Production canary and rollback",
        ancestry=_CHECKLIST_ROOT_ANCESTRY,
    )
    checklist_forward = _extract_list_item_at(
        checklist_rollback,
        parent_ordinal=0,
        parent_type="bullet_list_open",
        item_ordinal=3,
        expected_text="Rollback is not called configuration-only",
    )
    _assert_exact_normalized(
        checklist_forward,
        """- [ ] Rollback is not called configuration-only: with Hermes stopped it removes the
        verified Better shim while the wheel still supplies the command, selects bundled
        `hindsight`,
        removes the Better wheel, restores exact `hindsight-client==0.6.1`, verifies first recall
        without lazy package installation, and preserves Better's outbox plus both banks.""",
    )
    _assert_terms_in_order(
        checklist_forward,
        "with Hermes stopped",
        "shim while the wheel still supplies the command",
        "selects bundled `hindsight`",
        "removes the Better wheel",
        "restores exact `hindsight-client==0.6.1`",
        "verifies first recall",
        "preserves Better's outbox plus both banks",
    )
    checklist_reverse = _extract_list_item_at(
        checklist_rollback,
        parent_ordinal=0,
        parent_type="bullet_list_open",
        item_ordinal=4,
        expected_text="Returning to Better explicitly reinstalls",
    )
    _assert_exact_normalized(
        checklist_reverse,
        """- [ ] Returning to Better explicitly reinstalls the wheel and
        `hindsight-client==0.8.5`, verifies the managed shim before selection/restart, and does not
        migrate or delete either bank/outbox.""",
    )
    _assert_terms_in_order(
        checklist_reverse,
        "Returning to Better explicitly reinstalls the wheel and `hindsight-client==0.8.5`",
        "verifies the managed shim",
        "before selection/restart",
    )


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
