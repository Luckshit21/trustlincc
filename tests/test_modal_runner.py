import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modal_runner.runner_app import build_test_script, validate_request


class ModalRunnerTests(unittest.TestCase):
    def test_validates_code_and_test_limits(self):
        code, tests = validate_request(
            {
                "code": "def add(a, b): return a + b",
                "tests": ["add(1, 2) == 3"],
            }
        )

        self.assertEqual(code, "def add(a, b): return a + b")
        self.assertEqual(tests, ["add(1, 2) == 3"])

        with self.assertRaisesRegex(ValueError, "1 to 20"):
            validate_request({"code": "pass", "tests": []})

    def test_execution_script_reports_each_test_without_echoing_submission_output(self):
        code = "def add(a, b):\n    print('private submission output')\n    return a + b"
        tests = ["add(1, 2) == 3", "add(-1, 1) == 0"]

        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            script_path = Path(directory) / "runner.py"
            script_path.write_text(
                build_test_script(code, tests, str(result_path)),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )

            report = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(completed.stdout, "")
        self.assertTrue(report["passed"])
        self.assertEqual(report["tests_passed"], 2)
        self.assertEqual(report["tests_total"], 2)
        self.assertEqual([item["passed"] for item in report["results"]], [True, True])

    def test_execution_script_reports_failed_assertion(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            script_path = Path(directory) / "runner.py"
            script_path.write_text(
                build_test_script(
                    "def add(a, b): return a - b",
                    ["add(1, 2) == 3"],
                    str(result_path),
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [sys.executable, str(script_path)],
                check=True,
                timeout=5,
            )

            report = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertFalse(report["passed"])
        self.assertEqual(report["tests_passed"], 0)
        self.assertEqual(report["tests_total"], 1)


if __name__ == "__main__":
    unittest.main()
