#!/usr/bin/env python3
"""Run one command and write a bounded, unsigned execution receipt."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any, BinaryIO, Sequence

SCHEMA = "receipt-run-lite/v0.2"
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


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


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    process.kill()


def _capture_stream(
    source: BinaryIO,
    destination: BinaryIO,
    stream_name: str,
    *,
    max_output_bytes: int,
    captured: dict[str, int],
    capture_lock: threading.Lock,
    output_limit_reached: threading.Event,
    truncated_streams: set[str],
) -> None:
    try:
        while True:
            chunk = source.read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            with capture_lock:
                remaining = max_output_bytes - captured["bytes"]
                if remaining > 0:
                    kept = chunk[:remaining]
                    destination.write(kept)
                    captured["bytes"] += len(kept)
                if len(chunk) > remaining:
                    truncated_streams.add(stream_name)
                    output_limit_reached.set()
                    return
    finally:
        source.close()


def run_command(
    command: Sequence[str],
    *,
    receipt_path: Path,
    label: str,
    timeout_seconds: float | None,
    record_command: bool,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> tuple[int, dict[str, Any]]:
    if not command:
        raise ValueError("command must not be empty")
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be greater than zero")

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
    output_limited = False
    termination_reason: str | None = None
    process_tree_kill_supported = os.name == "posix"

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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=process_tree_kill_supported,
            )
            assert process.stdout is not None
            assert process.stderr is not None
            captured = {"bytes": 0}
            capture_lock = threading.Lock()
            output_limit_reached = threading.Event()
            truncated_streams: set[str] = set()
            readers = [
                threading.Thread(
                    target=_capture_stream,
                    args=(process.stdout, stdout_handle, "stdout"),
                    kwargs={
                        "max_output_bytes": max_output_bytes,
                        "captured": captured,
                        "capture_lock": capture_lock,
                        "output_limit_reached": output_limit_reached,
                        "truncated_streams": truncated_streams,
                    },
                    daemon=True,
                ),
                threading.Thread(
                    target=_capture_stream,
                    args=(process.stderr, stderr_handle, "stderr"),
                    kwargs={
                        "max_output_bytes": max_output_bytes,
                        "captured": captured,
                        "capture_lock": capture_lock,
                        "output_limit_reached": output_limit_reached,
                        "truncated_streams": truncated_streams,
                    },
                    daemon=True,
                ),
            ]
            for reader in readers:
                reader.start()

            deadline = (
                started_clock + timeout_seconds if timeout_seconds is not None else None
            )
            while process.poll() is None:
                if output_limit_reached.is_set():
                    output_limited = True
                    termination_reason = "output_limit"
                    _terminate_process(process)
                    break
                wait_seconds = 0.05
                if deadline is not None:
                    remaining_seconds = deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        timed_out = True
                        termination_reason = "timeout"
                        _terminate_process(process)
                        break
                    wait_seconds = min(wait_seconds, remaining_seconds)
                try:
                    process.wait(timeout=wait_seconds)
                except subprocess.TimeoutExpired:
                    pass

            exit_code = process.wait()
            for reader in readers:
                reader.join()
            output_limited = output_limited or output_limit_reached.is_set()
            if output_limited and termination_reason is None:
                termination_reason = "output_limit"
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

    runner_exit_code = 3 if output_limited else exit_code
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "label": label,
        "command": command_record,
        "execution": {
            "started_at": started_at,
            "finished_at": _utc_now(),
            "duration_ms": duration_ms,
            "exit_code": exit_code,
            "runner_exit_code": runner_exit_code,
            "timed_out": timed_out,
            "output_limited": output_limited,
            "max_output_bytes": max_output_bytes,
            "termination_reason": termination_reason,
            "process_tree_kill_supported": process_tree_kill_supported,
        },
        "outputs": {
            "stdout": {
                "path": stdout_path.name,
                "bytes": stdout_path.stat().st_size,
                "sha256": _sha256_file(stdout_path),
                "truncated": "stdout" in truncated_streams,
            },
            "stderr": {
                "path": stderr_path.name,
                "bytes": stderr_path.stat().st_size,
                "sha256": _sha256_file(stderr_path),
                "truncated": "stderr" in truncated_streams,
            },
        },
        "authority": {
            "signed_attestation": False,
            "independent_validation": False,
            "correctness_established": False,
        },
    }
    _atomic_json(receipt_path, receipt)
    return runner_exit_code, receipt


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
        "--max-output-bytes",
        type=int,
        default=DEFAULT_MAX_OUTPUT_BYTES,
        help=(
            "Combined stdout/stderr byte ceiling before termination "
            f"(default: {DEFAULT_MAX_OUTPUT_BYTES})."
        ),
    )
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
    if args.max_output_bytes <= 0:
        parser.error("--max-output-bytes must be greater than zero")
    try:
        exit_code, _ = run_command(
            command,
            receipt_path=args.output,
            label=args.label,
            timeout_seconds=args.timeout,
            record_command=args.record_command,
            max_output_bytes=args.max_output_bytes,
        )
    except (FileExistsError, OSError) as error:
        sys.stderr.write(f"receipt-run-lite: {error}\n")
        return 2
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
