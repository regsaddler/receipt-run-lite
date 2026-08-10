from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "receipt_run.py"


class ReceiptRunTests(unittest.TestCase):
    def invoke(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_success_writes_hashed_streams_without_raw_argv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "run.json"
            result = self.invoke(
                "--output",
                str(receipt),
                "--label",
                "smoke-test",
                "--",
                sys.executable,
                "-c",
                "print('hello')",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "receipt-run-lite/v0.1")
            self.assertEqual(payload["execution"]["exit_code"], 0)
            self.assertFalse(payload["command"]["argv_recorded"])
            self.assertNotIn("argv", payload["command"])
            self.assertEqual((Path(directory) / "run.stdout").read_text(), "hello\n")
            self.assertEqual(payload["outputs"]["stdout"]["bytes"], 6)

    def test_failure_is_preserved_and_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "failed.json"
            result = self.invoke(
                "--output",
                str(receipt),
                "--",
                sys.executable,
                "-c",
                "import sys; print('bad', file=sys.stderr); sys.exit(7)",
            )
            self.assertEqual(result.returncode, 7)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["execution"]["exit_code"], 7)
            self.assertEqual((Path(directory) / "failed.stderr").read_text(), "bad\n")

    def test_record_command_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "visible.json"
            result = self.invoke(
                "--output",
                str(receipt),
                "--record-command",
                "--",
                sys.executable,
                "-c",
                "pass",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertTrue(payload["command"]["argv_recorded"])
            self.assertEqual(payload["command"]["argv"][0], sys.executable)

    def test_timeout_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "timeout.json"
            result = self.invoke(
                "--output",
                str(receipt),
                "--timeout",
                "0.01",
                "--",
                sys.executable,
                "-c",
                "import time; time.sleep(1)",
            )
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertTrue(payload["execution"]["timed_out"])

    def test_existing_stream_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "protected.json"
            stdout = Path(directory) / "protected.stdout"
            stdout.write_text("keep me\n", encoding="utf-8")
            result = self.invoke(
                "--output",
                str(receipt),
                "--",
                sys.executable,
                "-c",
                "print('replacement')",
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(stdout.read_text(encoding="utf-8"), "keep me\n")
            self.assertFalse(receipt.exists())

    def test_failed_launch_leaves_no_partial_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "missing.json"
            result = self.invoke(
                "--output",
                str(receipt),
                "--",
                "receipt-run-lite-command-that-does-not-exist-94b7f6",
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(receipt.exists())
            self.assertFalse((Path(directory) / "missing.stdout").exists())
            self.assertFalse((Path(directory) / "missing.stderr").exists())

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_symlink_stream_is_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "linked.json"
            target = Path(directory) / "target.txt"
            target.write_text("keep me\n", encoding="utf-8")
            stdout = Path(directory) / "linked.stdout"
            stdout.symlink_to(target)
            result = self.invoke(
                "--output",
                str(receipt),
                "--",
                sys.executable,
                "-c",
                "print('replacement')",
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")
            self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
