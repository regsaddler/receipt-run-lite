#!/usr/bin/env python3
"""Run one command and write a bounded, unsigned execution receipt."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Sequence

SCHEMA = "receipt-run-lite/v0.1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_exclusive_binary(path: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "wb")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode(
        "utf-8"
    )
    try:
        with _open_exclusive_binary(temporary) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link publishes without replacing a path created after preflight.
        os.link(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _require_new_paths(paths: Sequence[Path]) -> None:
    for path in paths:
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"refusing to overwrite existing output: {path.name}")


def run_command(
    command: Sequence[str],
    *,
    receipt_path: Path,
    label: str,
    timeout_seconds: float | None,
    record_command: bool,
) -> tuple[int, dict[str, Any]]:
    if not command:
        raise ValueError("command must not be empty")

    receipt_path = receipt_path.resolve()
    stem = receipt_path.name.removesuffix(receipt_path.suffix)
    stdout_path = receipt_path.with_name(f"{stem}.stdout")
    stderr_path = receipt_path.with_name(f"{stem}.stderr")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    _require_new_paths((receipt_path, stdout_path, stderr_path))

    canonical_command = json.dumps(
        list(command), separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    started_at = _utc_now()
    started_clock = time.monotonic()
    timed_out = False

    created_streams: list[Path] = []
    try:
        stdout_handle = _open_exclusive_binary(stdout_path)
        created_streams.append(stdout_path)
        try:
            stderr_handle = _open_exclusive_binary(stderr_path)
            created_streams.append(stderr_path)
        except OSError:
            stdout_handle.close()
            raise

        with stdout_handle, stderr_handle:
            process = subprocess.Popen(
                list(command),
                stdout=stdout_handle,
                stderr=stderr_handle,
            )
            try:
                exit_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                exit_code = process.wait()
    except OSError:
        for stream_path in created_streams:
            stream_path.unlink(missing_ok=True)
        raise

    duration_ms = round((time.monotonic() - started_clock) * 1000)
    command_record: dict[str, Any] = {
        "executable": Path(command[0]).name,
        "argv_sha256": _sha256_bytes(canonical_command),
        "argv_recorded": record_command,
    }
    if record_command:
        command_record["argv"] = list(command)

    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "label": label,
        "command": command_record,
        "execution": {
            "started_at": started_at,
            "finished_at": _utc_now(),
            "duration_ms": duration_ms,
            "exit_code": exit_code,
            "timed_out": timed_out,
        },
        "outputs": {
            "stdout": {
                "path": stdout_path.name,
                "bytes": stdout_path.stat().st_size,
                "sha256": _sha256_file(stdout_path),
            },
            "stderr": {
                "path": stderr_path.name,
                "bytes": stderr_path.stat().st_size,
                "sha256": _sha256_file(stderr_path),
            },
        },
        "authority": {
            "signed_attestation": False,
            "independent_validation": False,
            "correctness_established": False,
        },
    }
    _atomic_json(receipt_path, receipt)
    return exit_code, receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one non-interactive command and write hashed stdout/stderr "
            "plus an unsigned JSON receipt."
        )
    )
    parser.add_argument("--output", required=True, type=Path, help="Receipt JSON path.")
    parser.add_argument("--label", default="command-run", help="Public-safe run label.")
    parser.add_argument("--timeout", type=float, help="Kill the command after this many seconds.")
    parser.add_argument(
        "--record-command",
        action="store_true",
        help="Include raw argv. Off by default because arguments may contain secrets.",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    try:
        exit_code, _ = run_command(
            command,
            receipt_path=args.output,
            label=args.label,
            timeout_seconds=args.timeout,
            record_command=args.record_command,
        )
    except (FileExistsError, OSError) as error:
        sys.stderr.write(f"receipt-run-lite: {error}\n")
        return 2
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
