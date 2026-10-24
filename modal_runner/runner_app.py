import hmac
import json
import os
import secrets
from typing import Any

import modal
from fastapi import Header, HTTPException
from modal.stream_type import StreamType

MAX_CODE_LENGTH = 30_000
MAX_TEST_CASES = 20
MAX_TEST_LENGTH = 500
MAX_TOTAL_TEST_LENGTH = 10_000
SANDBOX_TIMEOUT_SECONDS = 35
COMMAND_TIMEOUT_SECONDS = 25
MAX_RESULT_LENGTH = 20_000
app = modal.App("safelincc-bounty-runner")
endpoint_image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "fastapi[standard]"
)
sandbox_image = modal.Image.debian_slim(python_version="3.11")


def validate_request(payload: dict[str, Any]) -> tuple[str, list[str]]:
    code = payload.get("code")
    tests = payload.get("tests")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("`code` must be a non-empty string.")
    if len(code) > MAX_CODE_LENGTH:
        raise ValueError(f"Code must be {MAX_CODE_LENGTH} characters or fewer.")
    if (
        not isinstance(tests, list)
        or not tests
        or len(tests) > MAX_TEST_CASES
        or any(not isinstance(test, str) or not test.strip() for test in tests)
    ):
        raise ValueError(f"`tests` must contain 1 to {MAX_TEST_CASES} non-empty strings.")
    if any(len(test) > MAX_TEST_LENGTH for test in tests):
        raise ValueError(f"Each test must be {MAX_TEST_LENGTH} characters or fewer.")
    if sum(map(len, tests)) > MAX_TOTAL_TEST_LENGTH:
        raise ValueError(
            f"Combined tests must be {MAX_TOTAL_TEST_LENGTH} characters or fewer."
        )
    return code, [test.strip() for test in tests]


def build_test_script(code: str, tests: list[str], result_path: str) -> str:
    code_literal = json.dumps(code)
    tests_literal = json.dumps(tests)
    result_literal = json.dumps(result_path)
    return f"""import contextlib
import json
import time

class DiscardOutput:
    def write(self, value):
        return len(value)

    def flush(self):
        pass

source = {code_literal}
test_cases = {tests_literal}
result_path = {result_literal}
started = time.monotonic()
results = []
execution_error = None
namespace = {{"__name__": "__bounty_submission__"}}

with contextlib.redirect_stdout(DiscardOutput()), contextlib.redirect_stderr(DiscardOutput()):
    try:
        exec(compile(source, "<submission>", "exec"), namespace, namespace)
    except BaseException as error:
        execution_error = f"{{type(error).__name__}}: {{error}}"[:300]
        results = [
            {{
                "index": index,
                "expression": expression,
                "passed": False,
                "error": execution_error,
            }}
            for index, expression in enumerate(test_cases, start=1)
        ]
    else:
        for index, expression in enumerate(test_cases, start=1):
            try:
                outcome = eval(compile(expression, f"<test-{{index}}>", "eval"), namespace, namespace)
                results.append({{"index": index, "expression": expression, "passed": bool(outcome)}})
            except BaseException as error:
                results.append({{
                    "index": index,
                    "expression": expression,
                    "passed": False,
                    "error": f"{{type(error).__name__}}: {{error}}"[:300],
                }})

result = {{
    "status": "completed",
    "passed": execution_error is None and bool(results) and all(item["passed"] for item in results),
    "tests_passed": sum(1 for item in results if item["passed"]),
    "tests_total": len(test_cases),
    "results": results,
    "execution_error": execution_error,
    "duration_ms": int((time.monotonic() - started) * 1000),
}}
with open(result_path, "w", encoding="utf-8") as result_file:
    json.dump(result, result_file)
"""


def run_in_sandbox(code: str, tests: list[str]) -> dict[str, Any]:
    sandbox = modal.Sandbox.create(
        app=app,
        image=sandbox_image,
        timeout=SANDBOX_TIMEOUT_SECONDS,
        cpu=(0.25, 1),
        memory=(256, 512),
        block_network=True,
    )
    result_path = f"/tmp/bounty-result-{secrets.token_hex(12)}.json"
    script_path = f"/tmp/bounty-run-{secrets.token_hex(12)}.py"
    try:
        sandbox.filesystem.write_text(
            build_test_script(code, tests, result_path),
            script_path,
        )
        process = sandbox.exec(
            "python",
            "-I",
            script_path,
            stdout=StreamType.DEVNULL,
            stderr=StreamType.DEVNULL,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        exit_code = process.wait()
        if exit_code != 0:
            return {
                "status": "completed",
                "passed": False,
                "tests_passed": 0,
                "tests_total": len(tests),
                "results": [
                    {
                        "index": index,
                        "expression": expression,
                        "passed": False,
                        "error": "Submission exited before producing test results.",
                    }
                    for index, expression in enumerate(tests, start=1)
                ],
                "execution_error": "Submission exited before producing test results.",
            }
        output = sandbox.filesystem.read_text(result_path)
        if len(output) > MAX_RESULT_LENGTH:
            raise ValueError("Sandbox result exceeded the size limit.")
        result = json.loads(output)
        if (
            not isinstance(result, dict)
            or result.get("status") != "completed"
            or not isinstance(result.get("results"), list)
        ):
            raise ValueError("Sandbox returned an invalid result.")
        return result
    finally:
        sandbox.terminate()


@app.function(
    image=endpoint_image,
    secrets=[modal.Secret.from_name("bounty-runner-auth")],
    timeout=50,
)
@modal.fastapi_endpoint(method="POST")
def run_tests(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    expected_token = os.environ.get("BOUNTY_RUNNER_TOKEN", "")
    if len(expected_token) < 32:
        raise HTTPException(status_code=503, detail="Runner authentication is not configured.")
    supplied_token = (
        authorization.removeprefix("Bearer ").strip()
        if isinstance(authorization, str)
        else ""
    )
    if not expected_token or not hmac.compare_digest(supplied_token, expected_token):
        raise HTTPException(status_code=401, detail="Unauthorized.")

    try:
        code, tests = validate_request(payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    try:
        return run_in_sandbox(code, tests)
    except TimeoutError:
        return {
            "status": "completed",
            "passed": False,
            "tests_passed": 0,
            "tests_total": len(tests),
            "results": [
                {
                    "index": index,
                    "expression": expression,
                    "passed": False,
                    "error": "Submission exceeded the execution time limit.",
                }
                for index, expression in enumerate(tests, start=1)
            ],
            "execution_error": "Submission exceeded the execution time limit.",
        }
