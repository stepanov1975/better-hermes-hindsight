from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from better_hermes_hindsight.private_output import PrivateOutputError, write_private_json
from scripts.evaluate_recall_quality import (
    EvaluationInputError,
    LabeledResult,
    QualityCase,
    VariantResponse,
    capture_corpus_payload,
    capture_summary,
    evaluate,
    load_corpus,
)
from scripts.prepare_recall_quality_corpus import (
    _Candidate,
    _select_candidates,
    clean_historical_query,
    collect_historical_queries,
    corpus_payload,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "prepare_recall_quality_corpus.py"
EVALUATOR_SCRIPT = ROOT / "scripts" / "evaluate_recall_quality.py"


def _state_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            parent_session_id TEXT
        );
        CREATE TABLE messages (
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL,
            display_kind TEXT,
            display_metadata TEXT,
            _compressed_summary INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    connection.executemany(
        "INSERT INTO sessions(id, source, parent_session_id) VALUES (?, ?, ?)",
        [
            ("direct", "telegram", None),
            ("cli", "cli", None),
            ("child", "telegram", "direct"),
            ("tui", "tui", None),
            ("subagent", "subagent", None),
            ("cron", "cron", None),
        ],
    )
    connection.executemany(
        """
        INSERT INTO messages(
            session_id, role, content, timestamp, display_kind, display_metadata
        ) VALUES (?, 'user', ?, 1000000, ?, ?)
        """,
        [
            (
                "direct",
                "[Sun 2026-08-30 12:00:00 UTC] Which host runs the media service?\n\n"
                "<memory-context>private injected history</memory-context>",
                None,
                None,
            ),
            ("direct", "Which host runs the media service?", None, None),
            ("direct", "api_" + "key=synthetic-secret", None, None),
            ("direct", "[INTERNAL DELEGATION CLOSEOUT — lifecycle metadata]", None, None),
            ("direct", "Should not use typed display traffic", "internal_notification", None),
            ("direct", "Should not use attachment traffic", None, '{"attachment":true}'),
            ("cli", "What response style does the user prefer?", None, None),
            ("child", "What changed after the session was compressed?", None, None),
            ("tui", "Which decision did we make in the terminal?", None, None),
            ("subagent", "Do not collect delegated worker traffic", None, None),
            ("cron", "Do not collect cron traffic", None, None),
        ],
    )
    connection.execute(
        """
        INSERT INTO messages(
            session_id, role, content, timestamp, display_kind, display_metadata,
            _compressed_summary
        ) VALUES ('direct', 'user', 'Do not collect compaction scaffolding', 1000000, NULL, NULL, 1)
        """
    )
    connection.commit()
    connection.close()


def test_clean_historical_query_removes_transport_wrappers() -> None:
    assert (
        clean_historical_query(
            "[Sun 2026-08-30 12:00:00 UTC] Remember this query\n"
            "<memory-context>not part of the query</memory-context>"
        )
        == "Remember this query"
    )
    assert clean_historical_query("Keep a literal <memory-context> marker") == (
        "Keep a literal <memory-context> marker"
    )
    assert (
        clean_historical_query(
            "Keep <memory-context>literal</memory-context> query text\n"
            "<memory-context>injected</memory-context>"
        )
        == "Keep <memory-context>literal</memory-context> query text"
    )
    assert (
        clean_historical_query(
            "Real query\n<memory-context>evidence mentions <memory-context> literally"
            "</memory-context>"
        )
        == "Real query"
    )


def test_session_cap_uses_an_equivalent_query_from_an_uncapped_session() -> None:
    candidates = [
        _Candidate("session-a", "Another query", "another query", b"\x00"),
        _Candidate("session-a", "Repeated query", "repeated query", b"\x01"),
        _Candidate("session-b", "Repeated query", "repeated query", b"\x01"),
    ]

    selected, rejected = _select_candidates(candidates, limit=2, max_per_session=1)

    assert [candidate.query for candidate in selected] == ["Another query", "Repeated query"]
    assert [candidate.session_id for candidate in selected] == ["session-a", "session-b"]
    assert rejected == 1


def test_collect_historical_queries_is_bounded_private_and_provenance_free(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _state_db(database)

    queries, stats = collect_historical_queries(
        database,
        days=1,
        limit=10,
        max_per_session=2,
        max_chars=1_200,
        max_lines=12,
        sources=("telegram", "cli", "tui"),
        seed="synthetic-seed",
        now=1_000_100,
    )

    assert set(queries) == {
        "Which host runs the media service?",
        "What response style does the user prefer?",
        "What changed after the session was compressed?",
        "Which decision did we make in the terminal?",
    }
    assert stats["scanned"] == 7
    assert stats["selected"] == 4
    assert stats["excluded_credential_pattern"] == 1
    assert stats["excluded_duplicate"] == 1
    assert stats["excluded_internal"] == 1
    payload = corpus_payload(queries)
    encoded = json.dumps(payload)
    assert "session_id" not in encoded
    assert "message_id" not in encoded


def test_collector_reads_active_wal_without_changing_rows(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _state_db(database)
    writer = sqlite3.connect(database)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("direct", "user", "Which setting did we use?", 1_000_001),
    )
    writer.commit()
    before = writer.execute(
        "SELECT session_id, role, content, timestamp, display_kind, display_metadata "
        "FROM messages ORDER BY rowid"
    ).fetchall()

    queries, _stats = collect_historical_queries(
        database,
        days=1,
        limit=20,
        max_per_session=20,
        max_chars=1_200,
        max_lines=12,
        sources=("telegram", "cli", "tui"),
        seed="synthetic-seed",
        now=1_000_100,
    )

    after = writer.execute(
        "SELECT session_id, role, content, timestamp, display_kind, display_metadata "
        "FROM messages ORDER BY rowid"
    ).fetchall()
    writer.close()
    assert before == after
    assert "Which setting did we use?" in queries


def test_collector_bounds_raw_rows_and_streamed_candidate_window(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _state_db(database)
    connection = sqlite3.connect(database)
    connection.executemany(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
        [("direct", f"Bounded historical query number {index}", 1_000_002) for index in range(150)],
    )
    connection.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
        ("direct", "X" * 70_000, 1_000_003),
    )
    connection.commit()
    connection.close()

    queries, stats = collect_historical_queries(
        database,
        days=1,
        limit=1,
        max_per_session=200,
        max_chars=1_200,
        max_lines=12,
        sources=("telegram",),
        seed="synthetic-seed",
        now=1_000_100,
    )

    assert stats["scanned"] == 100
    assert stats["selected"] == 1
    assert len(queries) == 1
    assert len(queries[0]) <= 1_200


def test_collector_cli_prints_only_aggregate_counts_and_creates_private_file(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _state_db(database)
    output = tmp_path / "private" / "queries.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--state-db",
            str(database),
            "--output",
            str(output),
            "--days",
            "30000",
            "--limit",
            "10",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "media service" not in completed.stdout
    assert json.loads(completed.stdout)["selected"] == 4
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    cases = load_corpus(output)
    assert len(cases) == 4
    assert all(not case.labels_complete for case in cases)


def test_private_json_refuses_insecure_parent_and_existing_output(tmp_path: Path) -> None:
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    os.chmod(insecure, 0o755)
    with pytest.raises(PrivateOutputError, match="group or other"):
        write_private_json(insecure / "artifact.json", {"private": True})

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    first = private / "artifact.json"
    write_private_json(first, {"private": True})
    with pytest.raises(PrivateOutputError, match="already exists"):
        write_private_json(first, {"private": False})


def test_private_json_refuses_a_symlinked_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    symlink = tmp_path / "linked"
    symlink.symlink_to(real, target_is_directory=True)

    with pytest.raises(PrivateOutputError, match="directory is unavailable"):
        write_private_json(symlink / "nested" / "artifact.json", {"private": True})
    assert not (real / "nested" / "artifact.json").exists()


def test_incomplete_labels_cannot_be_scored() -> None:
    case = QualityCase(
        case_id="historical-001",
        query="Which host runs the service?",
        expect_recall=False,
        useful_result_ids=frozenset(),
        redundant_result_ids=frozenset(),
        irrelevant_result_ids=frozenset(),
        responses={},
        labels_complete=False,
    )
    responses = {"baseline": {case.case_id: VariantResponse(results=(), elapsed_ms=1.0)}}

    with pytest.raises(EvaluationInputError, match="incomplete labels"):
        evaluate((case,), responses)


def test_live_cli_rejects_incomplete_labels_before_loading_live_config(tmp_path: Path) -> None:
    corpus = tmp_path / "unlabelled.json"
    corpus.write_text(
        json.dumps(corpus_payload(["Which host runs the service?"])),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(EVALUATOR_SCRIPT),
            str(corpus),
            "--hermes-home",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "corpus contains incomplete labels" in completed.stderr
    assert "configured CLI principal" not in completed.stderr


def test_capture_cli_preflights_destination_before_loading_live_config(tmp_path: Path) -> None:
    corpus = tmp_path / "unlabelled.json"
    corpus.write_text(
        json.dumps(corpus_payload(["Which host runs the service?"])),
        encoding="utf-8",
    )
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    destination = private_dir / "capture.json"
    destination.write_text("occupied", encoding="utf-8")
    destination.chmod(0o600)

    completed = subprocess.run(
        [
            sys.executable,
            str(EVALUATOR_SCRIPT),
            str(corpus),
            "--hermes-home",
            str(tmp_path),
            "--capture-private",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "private output file already exists" in completed.stderr
    assert "configured CLI principal" not in completed.stderr


def test_capture_payload_round_trips_private_responses_without_provenance(tmp_path: Path) -> None:
    case = QualityCase(
        case_id="historical-001",
        query="Which host runs the service?",
        expect_recall=False,
        useful_result_ids=frozenset(),
        redundant_result_ids=frozenset(),
        irrelevant_result_ids=frozenset(),
        responses={},
    )
    responses = {
        "baseline": {
            case.case_id: VariantResponse(
                results=(LabeledResult("result-1", "The service runs on host one."),),
                elapsed_ms=12.5,
            )
        },
        "prefer_observations": {case.case_id: VariantResponse(results=(), elapsed_ms=11.0)},
    }

    payload = capture_corpus_payload((case,), responses)
    output = tmp_path / "private"
    output.mkdir(mode=0o700)
    corpus = output / "capture.json"
    write_private_json(corpus, payload)

    loaded = load_corpus(corpus)
    assert loaded[0].responses["baseline"].results[0].result_id == "result-1"
    assert loaded[0].labels_complete is False
    assert capture_summary((case,), responses) == {
        "result": "captured",
        "schema_version": 1,
        "case_count": 1,
        "variants": {
            "baseline": {"returned_records": 1, "returned_text_bytes": 29},
            "prefer_observations": {"returned_records": 0, "returned_text_bytes": 0},
        },
    }
