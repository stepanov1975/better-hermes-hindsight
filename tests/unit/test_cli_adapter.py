"""Fast contracts for the packaged Better Hindsight CLI adapter."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from pathlib import Path

import pytest

from better_hermes_hindsight.operator_cli import better_hindsight_command, register_cli


def _parser() -> ArgumentParser:
    parser = ArgumentParser(prog="hermes better_hindsight")
    register_cli(parser)
    return parser


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["missions"],
        ["missions", "apply"],
        ["missions", "apply", "--con"],
        ["missions", "--confirm", "apply"],
        ["retry"],
        ["drain"],
        ["status", "--confirm"],
    ],
)
def test_parser_rejects_incomplete_abbreviated_misplaced_and_removed_commands(
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        _parser().parse_args(argv)

    assert caught.value.code == 2


def test_nested_apply_help_is_local_and_documents_confirmation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        _parser().parse_args(["missions", "apply", "--help"])

    assert caught.value.code == 0
    captured = capsys.readouterr()
    assert "--confirm" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("args", "command"),
    [
        (Namespace(better_hindsight_action="status"), "status"),
        (
            Namespace(
                better_hindsight_action="missions",
                better_hindsight_mission_action="check",
            ),
            "missions_check",
        ),
        (
            Namespace(
                better_hindsight_action="missions",
                better_hindsight_mission_action="apply",
                confirm=True,
            ),
            "missions_apply",
        ),
    ],
)
def test_malformed_configuration_maps_to_fixed_cli_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: Namespace,
    command: str,
) -> None:
    config_dir = tmp_path / "better_hindsight"
    config_dir.mkdir()
    (config_dir / "config.json").write_text('{"single_principal":', encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with pytest.raises(SystemExit) as caught:
        better_hindsight_command(args)

    assert caught.value.code == 3
    captured = capsys.readouterr()
    assert captured.out == (
        f'{{"command":"{command}","error":"configuration_invalid","result":"error"}}\n'
    )
    assert captured.err == ""
