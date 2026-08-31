"""Private file output helpers for local recall-quality evaluation artifacts."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import stat
from pathlib import Path


class PrivateOutputError(ValueError):
    """Raised when a private evaluation artifact cannot be written safely."""


_TEMPORARY_NAME_LENGTH = len(".rq-" + "0" * 16 + ".tmp")


def _temporary_name() -> str:
    return f".rq-{secrets.token_hex(8)}.tmp"


def _validate_name_capacity(parent_descriptor: int, destination_name: str) -> None:
    try:
        name_max = os.fpathconf(parent_descriptor, "PC_NAME_MAX")
    except (OSError, ValueError) as error:
        raise PrivateOutputError("private output filename capacity could not be checked") from error
    if len(os.fsencode(destination_name)) > name_max or name_max < _TEMPORARY_NAME_LENGTH:
        raise PrivateOutputError("private output filename is too long")


def _open_private_parent(parent: Path) -> int:
    """Create and bind an absolute directory without following symlink components."""

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    try:
        descriptor = os.open("/", directory_flags)
    except OSError as error:
        raise PrivateOutputError("private output directory is unavailable") from error

    try:
        for component in parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise PrivateOutputError("private output path contains an unsafe component")
            try:
                next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            except FileNotFoundError:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor

        parent_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise PrivateOutputError("private output parent must be a real directory")
        if parent_stat.st_uid != os.getuid():
            raise PrivateOutputError("private output directory must be owned by the current user")
        if stat.S_IMODE(parent_stat.st_mode) & 0o077:
            raise PrivateOutputError(
                "private output directory must not allow group or other access"
            )
        if stat.S_IMODE(parent_stat.st_mode) & 0o300 != 0o300:
            raise PrivateOutputError("private output directory must be owner-writable")
        return descriptor
    except PrivateOutputError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise PrivateOutputError("private output directory is unavailable") from error


def _validate_private_path_shape(path: Path) -> None:
    if not path.is_absolute():
        raise PrivateOutputError("private output path must be absolute")
    if path.name in {"", ".", ".."}:
        raise PrivateOutputError("private output path must name a file")


def validate_private_output_path(path: Path) -> None:
    """Preflight deterministic destination failures without creating the output file."""

    _validate_private_path_shape(path)
    parent_descriptor = _open_private_parent(path.parent)
    try:
        _validate_name_capacity(parent_descriptor, path.name)
        try:
            os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise PrivateOutputError("private output file already exists")
    except OSError as error:
        raise PrivateOutputError("private output path could not be checked") from error
    finally:
        os.close(parent_descriptor)


def write_private_json(path: Path, payload: object) -> None:
    """Atomically publish one owner-only JSON file without overwriting an artifact."""

    _validate_private_path_shape(path)

    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    parent_descriptor = _open_private_parent(path.parent)
    descriptor = -1
    temporary_name = _temporary_name()
    temporary_exists = False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        _validate_name_capacity(parent_descriptor, path.name)
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
        temporary_exists = True
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise PrivateOutputError("private output file already exists") from None
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
            temporary_exists = False
        except OSError:
            pass
        with contextlib.suppress(OSError):
            os.fsync(parent_descriptor)
    except PrivateOutputError:
        raise
    except OSError as error:
        raise PrivateOutputError("private output file could not be written") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_exists:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name, dir_fd=parent_descriptor)
        os.close(parent_descriptor)


__all__ = ["PrivateOutputError", "validate_private_output_path", "write_private_json"]
