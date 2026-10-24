import re
import ssl
import sys
import unittest
import os
from datetime import datetime, timezone
from json import dumps
from pathlib import Path
from unittest.mock import AsyncMock, patch

from uagents_core.contrib.protocols.chat import ChatMessage, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocols import (
    TASK_STORAGE_PREFIX,
    TestRunnerUnavailable,
    _send_test_request,
    handle_message,
    parse_task_request,
    run_submission_tests,
)


class FakeStorage:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class FakeContext:
    def __init__(self):
        self.storage = FakeStorage()
        self.send = AsyncMock()


def make_message(text):
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        content=[TextContent(type="text", text=text)],
    )


class TaskRequestTests(unittest.TestCase):
    def test_parses_task_and_test_specifications(self):
        task = parse_task_request(
            "create task\n"
            "title: Add a function\n"
            "bounty: 0.1 FET\n"
            "freelancer: worker-address\n"
            "tests:\n"
            "add(1, 2) == 3\n"
            "- add(-1, 1) == 0\n"
        )

        self.assertEqual(task["title"], "Add a function")
        self.assertEqual(task["bounty"], "0.1 FET")
        self.assertEqual(task["freelancer"], "worker-address")
        self.assertEqual(task["tests"], ["add(1, 2) == 3", "add(-1, 1) == 0"])

    def test_parses_single_line_task_request(self):
        task = parse_task_request(
            "create task title: Add a function bounty: 0.1 FET "
            "freelancer: any tests: add(1, 2) == 3 "
            "add(-1, 1) == 0"
        )

        self.assertEqual(task["title"], "Add a function")
        self.assertEqual(task["bounty"], "0.1 FET")
        self.assertEqual(task["freelancer"], "any")
        self.assertEqual(
            task["tests"],
            ["add(1, 2) == 3", "add(-1, 1) == 0"],
        )

    def test_requires_title_bounty_and_at_least_one_test(self):
        with self.assertRaisesRegex(ValueError, "title, bounty"):
            parse_task_request("create task\ntests:\nadd(1, 2) == 3")

        with self.assertRaisesRegex(ValueError, "at least one test"):
            parse_task_request("create task\ntitle: Add\nbounty: 1 FET\ntests:")

    def test_runner_client_sends_bearer_token_and_validates_results(self):
        test = "add(1, 2) == 3"
        response_data = {
            "status": "completed",
            "passed": True,
            "tests_passed": 1,
            "tests_total": 1,
            "results": [
                {"index": 1, "expression": test, "passed": True},
            ],
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _size):
                return dumps(response_data).encode("utf-8")

        with patch("protocols.urllib.request.urlopen", return_value=FakeResponse()) as open_url:
            result = _send_test_request(
                "https://runner.example/run",
                "test-token",
                "def add(a, b): return a + b",
                [test],
            )

        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        context = open_url.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertTrue(result["passed"])

        response_data["tests_total"] = 0
        with patch("protocols.urllib.request.urlopen", return_value=FakeResponse()):
            with self.assertRaisesRegex(TestRunnerUnavailable, "invalid test report"):
                _send_test_request(
                    "https://runner.example/run",
                    "test-token",
                    "pass",
                    [test],
                )


class RunnerConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_requires_https_and_long_token(self):
        with patch.dict(
            os.environ,
            {
                "BOUNTY_RUNNER_URL": "http://runner.example/run",
                "BOUNTY_RUNNER_TOKEN": "x" * 40,
            },
            clear=True,
        ):
            with self.assertRaisesRegex(TestRunnerUnavailable, "HTTPS"):
                await run_submission_tests("pass", ["True"])

        with patch.dict(
            os.environ,
            {
                "BOUNTY_RUNNER_URL": "https://runner.example/run",
                "BOUNTY_RUNNER_TOKEN": "short",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(TestRunnerUnavailable, "not long enough"):
                await run_submission_tests("pass", ["True"])


class TaskWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ctx = FakeContext()
        self.owner = "client-address"
        self.worker = "worker-address"
        self.runner = AsyncMock(side_effect=self.passing_result)
        self.runner_patch = patch("protocols.run_submission_tests", new=self.runner)
        self.runner_patch.start()

    async def asyncTearDown(self):
        self.runner_patch.stop()

    async def passing_result(self, _code, tests):
        return {
            "status": "completed",
            "passed": True,
            "tests_passed": len(tests),
            "tests_total": len(tests),
            "results": [
                {"index": index, "expression": test, "passed": True}
                for index, test in enumerate(tests, start=1)
            ],
        }

    async def create_task(self):
        await handle_message(
            self.ctx,
            self.owner,
            make_message(
                "create task\n"
                "title: Add a function\n"
                "bounty: 0.1 FET\n"
                "freelancer: any\n"
                "tests:\n"
                "add(1, 2) == 3"
            ),
        )
        response = self.ctx.send.await_args_list[-1].args[1].text()
        task_id = re.search(r"TASK-[A-F0-9]{8}", response).group()
        return task_id

    async def test_exact_single_line_message_creates_task(self):
        text = (
            "create task title: Add a function bounty: 0.1 FET "
            "freelancer: any tests: add(1, 2) == 3 "
            "add(-1, 1) == 0"
        )

        await handle_message(self.ctx, self.owner, make_message(text))

        response = self.ctx.send.await_args_list[-1].args[1].text()
        task_match = re.search(r"TASK-[A-F0-9]{8}", response)
        self.assertIsNotNone(task_match)
        task = self.ctx.storage.get(f"{TASK_STORAGE_PREFIX}{task_match.group()}")
        self.assertEqual(task["title"], "Add a function")
        self.assertEqual(task["bounty"], "0.1 FET")
        self.assertEqual(task["freelancer"], "any")
        self.assertEqual(task["status"], "OPEN")

    async def test_only_owner_can_approve_and_repeated_approval_is_blocked(self):
        task_id = await self.create_task()
        await handle_message(
            self.ctx,
            self.worker,
            make_message(f"submit {task_id}\ndef add(a, b): return a + b"),
        )

        owner_review = self.ctx.send.await_args_list[-2]
        self.assertEqual(owner_review.args[0], self.owner)
        self.assertIn("def add(a, b): return a + b", owner_review.args[1].text())
        task = self.ctx.storage.get(f"{TASK_STORAGE_PREFIX}{task_id}")
        self.assertEqual(task["status"], "TESTS_PASSED")

        await handle_message(self.ctx, self.worker, make_message(f"approve {task_id}"))
        task = self.ctx.storage.get(f"{TASK_STORAGE_PREFIX}{task_id}")
        self.assertEqual(task["status"], "TESTS_PASSED")

        await handle_message(self.ctx, self.owner, make_message(f"approve {task_id}"))
        task = self.ctx.storage.get(f"{TASK_STORAGE_PREFIX}{task_id}")
        self.assertEqual(task["status"], "APPROVED_SIMULATED")

        await handle_message(self.ctx, self.owner, make_message(f"approve {task_id}"))
        task = self.ctx.storage.get(f"{TASK_STORAGE_PREFIX}{task_id}")
        self.assertEqual(task["status"], "APPROVED_SIMULATED")

    async def test_held_task_accepts_revision(self):
        task_id = await self.create_task()
        await handle_message(
            self.ctx,
            self.worker,
            make_message(f"submit {task_id}\ndef add(a, b): return a + b"),
        )
        await handle_message(self.ctx, self.owner, make_message(f"hold {task_id}"))
        self.assertEqual(
            self.ctx.storage.get(f"{TASK_STORAGE_PREFIX}{task_id}")["status"],
            "HELD",
        )

        await handle_message(
            self.ctx,
            self.worker,
            make_message(f"submit {task_id}\ndef add(a, b):\n    return a + b"),
        )
        task = self.ctx.storage.get(f"{TASK_STORAGE_PREFIX}{task_id}")
        self.assertEqual(task["status"], "TESTS_PASSED")
        self.assertIn("return a + b", task["submission"])

    async def test_failed_sandbox_tests_hold_task_and_block_approval(self):
        self.runner.side_effect = None
        self.runner.return_value = {
            "status": "completed",
            "passed": False,
            "tests_passed": 0,
            "tests_total": 1,
            "results": [
                {
                    "index": 1,
                    "expression": "add(1, 2) == 3",
                    "passed": False,
                }
            ],
        }
        task_id = await self.create_task()

        await handle_message(
            self.ctx,
            self.worker,
            make_message(f"submit {task_id}\ndef add(a, b): return a - b"),
        )

        task = self.ctx.storage.get(f"{TASK_STORAGE_PREFIX}{task_id}")
        self.assertEqual(task["status"], "HELD")
        await handle_message(self.ctx, self.owner, make_message(f"approve {task_id}"))
        self.assertEqual(task["status"], "HELD")

    async def test_runner_unavailable_never_records_pass_or_allows_approval(self):
        self.runner.side_effect = TestRunnerUnavailable("offline")
        task_id = await self.create_task()

        await handle_message(
            self.ctx,
            self.worker,
            make_message(f"submit {task_id}\ndef add(a, b): return a + b"),
        )

        task = self.ctx.storage.get(f"{TASK_STORAGE_PREFIX}{task_id}")
        self.assertEqual(task["status"], "SUBMITTED")
        self.assertIsNone(task["test_result"])
        await handle_message(self.ctx, self.owner, make_message(f"approve {task_id}"))
        self.assertEqual(task["status"], "SUBMITTED")

    async def test_assigned_freelancer_is_enforced(self):
        await handle_message(
            self.ctx,
            self.owner,
            make_message(
                "create task\n"
                "title: Add a function\n"
                "bounty: 0.1 FET\n"
                "freelancer: worker-address\n"
                "tests:\n"
                "add(1, 2) == 3"
            ),
        )
        response = self.ctx.send.await_args_list[-1].args[1].text()
        task_id = re.search(r"TASK-[A-F0-9]{8}", response).group()

        await handle_message(
            self.ctx,
            "other-worker",
            make_message(f"submit {task_id}\ndef add(a, b): return a + b"),
        )
        self.assertIsNone(self.ctx.storage.get(f"{TASK_STORAGE_PREFIX}{task_id}")["submission"])


if __name__ == "__main__":
    unittest.main()
