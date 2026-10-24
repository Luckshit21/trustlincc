import asyncio
import json
import os
import re
import ssl
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import certifi
from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    MetadataContent,
    TextContent,
    chat_protocol_spec,
)

TASK_STORAGE_PREFIX = "bounty:task:"
MAX_SUBMISSION_LENGTH = 30_000
MAX_TEST_CASES = 20
MAX_RUNNER_RESPONSE_BYTES = 20_000

HELP_TEXT = (
    "I manage a code-bounty review workflow. Submissions are run against the task's "
    "listed tests in a network-disabled Modal sandbox when configured. Owners still "
    "review the code manually; approval is simulated and no real funds move.\n\n"
    "Create a task:\n"
    "create task\n"
    "title: Add a function\n"
    "bounty: 0.1 FET\n"
    "freelancer: any\n"
    "tests:\n"
    "add(1, 2) == 3\n"
    "add(-1, 1) == 0\n\n"
    "You can also send a single-line request, for example:\n"
    "create task title: Add a function bounty: 0.1 FET freelancer: any "
    "tests: add(1, 2) == 3\n\n"
    "Submit code with `submit TASK-<id>` on the first line, then paste the code. "
    "Use `test TASK-<id>` to rerun the sandbox tests. After they pass, the task owner "
    "can use `approve TASK-<id>` or `hold TASK-<id>`. "
    "Use `status TASK-<id>` to check a task. Send `demo` to run a sandbox example "
    "without creating a task."
)

bounty_protocol = Protocol(spec=chat_protocol_spec)


class TestRunnerUnavailable(RuntimeError):
    pass


def create_text_chat(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid.uuid4(),
        content=[TextContent(type="text", text=text)],
    )


def parse_task_request(text: str) -> dict[str, Any]:
    text = re.sub(
        r"\s+(?=(?:title|bounty|freelancer|tests)\s*:)",
        "\n",
        text.strip(),
        flags=re.IGNORECASE,
    )
    lines = text.strip().splitlines()
    fields: dict[str, str] = {}
    tests: list[str] = []
    reading_tests = False

    for line in lines[1:]:
        value = line.strip()
        if not value:
            continue
        if reading_tests:
            tests.append(value.removeprefix("-").strip())
            continue
        key, separator, field_value = value.partition(":")
        if not separator:
            raise ValueError("Use `field: value` lines and put test cases after `tests:`.")
        key = key.strip().lower()
        if key == "tests":
            reading_tests = True
            if field_value.strip():
                tests.extend(
                    re.split(r";\s*|\s+(?=[A-Za-z_]\w*\s*\()", field_value.strip())
                )
        elif key in {"title", "bounty", "freelancer"}:
            fields[key] = field_value.strip()

    missing = [key for key in ("title", "bounty") if not fields.get(key)]
    if missing:
        raise ValueError(f"Please provide {', '.join(missing)}.")
    if not tests:
        raise ValueError("Add at least one test case after `tests:`.")
    if len(tests) > MAX_TEST_CASES:
        raise ValueError(f"Use no more than {MAX_TEST_CASES} test cases.")
    if any(len(test) > 500 for test in tests):
        raise ValueError("Each test case must be 500 characters or fewer.")

    return {
        "title": fields["title"][:120],
        "bounty": fields["bounty"][:60],
        "freelancer": fields.get("freelancer", "any").strip() or "any",
        "tests": tests,
    }


def parse_command(text: str) -> tuple[str, str, str] | None:
    match = re.match(
        r"^\s*(create\s+task|submit|approve|hold|status|test|demo)\b([^\r\n]*)",
        text,
        re.I,
    )
    if not match:
        return None
    command = re.sub(r"\s+", " ", match.group(1).lower())
    argument = match.group(2).strip()
    remainder = text[match.end():].lstrip("\r\n")
    return command, argument, remainder


def get_task(ctx: Context, task_id: str) -> dict[str, Any] | None:
    return ctx.storage.get(f"{TASK_STORAGE_PREFIX}{task_id.upper()}")


def save_task(ctx: Context, task: dict[str, Any]) -> None:
    ctx.storage.set(f"{TASK_STORAGE_PREFIX}{task['task_id']}", task)


def _send_test_request(
    endpoint: str, token: str, code: str, tests: list[str]
) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps({"code": code, "tests": tests}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=60, context=context) as response:
            response_body = response.read(MAX_RUNNER_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RUNNER_RESPONSE_BYTES:
                raise TestRunnerUnavailable("The sandbox service response was too large.")
            result = json.loads(response_body.decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read(1000).decode("utf-8", errors="replace")
        raise TestRunnerUnavailable(
            f"The sandbox service returned HTTP {error.code}: {detail}"
        ) from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise TestRunnerUnavailable(f"The sandbox service could not be reached: {error}") from error

    if (
        not isinstance(result, dict)
        or result.get("status") != "completed"
        or not isinstance(result.get("passed"), bool)
        or not isinstance(result.get("tests_passed"), int)
        or not isinstance(result.get("tests_total"), int)
        or not isinstance(result.get("results"), list)
        or result["tests_total"] != len(tests)
        or result["tests_passed"] < 0
        or result["tests_passed"] > result["tests_total"]
        or len(result["results"]) != result["tests_total"]
    ):
        raise TestRunnerUnavailable("The sandbox service returned an invalid test report.")
    actual_passed = 0
    for index, item in enumerate(result["results"], start=1):
        if (
            not isinstance(item, dict)
            or item.get("index") != index
            or item.get("expression") != tests[index - 1]
            or not isinstance(item.get("passed"), bool)
        ):
            raise TestRunnerUnavailable("The sandbox service returned an invalid test report.")
        actual_passed += int(item["passed"])
    if (
        actual_passed != result["tests_passed"]
        or result["passed"] != (actual_passed == result["tests_total"])
    ):
        raise TestRunnerUnavailable("The sandbox service returned inconsistent test results.")
    return result


async def run_submission_tests(code: str, tests: list[str]) -> dict[str, Any]:
    endpoint = os.environ.get("BOUNTY_RUNNER_URL", "").strip()
    token = os.environ.get("BOUNTY_RUNNER_TOKEN", "").strip()
    if not endpoint or not token:
        raise TestRunnerUnavailable(
            "The isolated test runner is not configured; no code was executed."
        )
    parsed_endpoint = urlparse(endpoint)
    if (
        parsed_endpoint.scheme != "https"
        or not parsed_endpoint.netloc
        or parsed_endpoint.username
        or parsed_endpoint.password
        or parsed_endpoint.fragment
    ):
        raise TestRunnerUnavailable("The sandbox endpoint must be a valid HTTPS URL.")
    if len(token) < 32:
        raise TestRunnerUnavailable("The sandbox access token is not long enough.")
    return await asyncio.to_thread(_send_test_request, endpoint, token, code, tests)


def build_submission_card(task: dict[str, Any]) -> MetadataContent:
    payload = {
        "title": "Submission ready for client review",
        "summary_rows": [
            {"label": "Task", "value": task["title"]},
            {"label": "Task ID", "value": task["task_id"]},
            {"label": "Tests specified", "value": str(len(task["tests"]))},
            {
                "label": "Sandbox tests",
                "value": (
                    f"{task['test_result']['tests_passed']}/"
                    f"{task['test_result']['tests_total']} passed"
                ),
            },
            {"label": "Status", "value": task["status"]},
            {"label": "Bounty (simulated)", "value": task["bounty"]},
        ],
        "approve_cta": {
            "label": "Approve (simulated)",
            "selection": {"action": "approve_escrow", "task_id": task["task_id"]},
        },
        "reject_cta": {
            "label": "Hold for revision",
            "selection": {"action": "reject_escrow", "task_id": task["task_id"]},
        },
    }
    return MetadataContent(
        type="metadata",
        metadata={
            "card_protocol_version": "1",
            "requires_card_interaction": "true",
            "card_kind": "review",
            "card_payload": json.dumps(payload),
        },
    )


def parse_selection(text: str) -> dict[str, str] | None:
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        action = data.get("action")
        task_id = data.get("task_id")
        if (
            isinstance(action, str)
            and action in {"approve_escrow", "reject_escrow"}
            and isinstance(task_id, str)
        ):
            return {"action": action, "task_id": task_id}

    command = parse_command(text)
    if command is None:
        return None
    action, task_id, _ = command
    if action in {"approve", "hold"} and task_id:
        return {
            "action": "approve_escrow" if action == "approve" else "reject_escrow",
            "task_id": task_id,
        }
    return None


def respond_to_question(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())

    if normalized in {"help", "commands", "menu"} or re.search(
        r"\b(show|list|give)\b.*\b(commands|help)\b", normalized
    ):
        return HELP_TEXT
    if re.search(r"\b(create|open|post|make)\b.*\b(task|bounty)\b", normalized):
        return (
            "To create a task, send the title, simulated bounty, freelancer, and "
            "one test expression per line. For example:\n"
            "create task\n"
            "title: Add a function\n"
            "bounty: 0.1 FET\n"
            "freelancer: any\n"
            "tests:\n"
            "add(1, 2) == 3\n"
            "add(-1, 1) == 0"
        )
    if re.search(r"\b(submit|submission|freelancer|send code)\b", normalized):
        return (
            "A freelancer submits with `submit TASK-<id>` on the first line, then "
            "the Python code below it. The task owner cannot submit to their own "
            "task; use a different sender. Submissions run against the listed tests "
            "in the configured isolated sandbox."
        )
    if re.search(r"\b(approve|approval|payment|pay|escrow|release)\b", normalized):
        return (
            "Only the task owner can approve or hold a submission, and approval is "
            "blocked until the listed sandbox tests pass. Approval and bounty amounts "
            "are simulated; no real escrow or payment occurs."
        )
    if re.search(r"\b(status|progress|task id|check task)\b", normalized):
        return (
            "Use `status TASK-<id>` to see a task's status and latest sandbox result. "
            "Only its owner or submitter can view that status."
        )
    if re.search(r"\b(test|tests|sandbox|run code|execute)\b", normalized):
        return (
            "Submissions are checked only against the task's listed tests in a "
            "network-disabled Modal sandbox. Use `test TASK-<id>` to rerun them. "
            "Passing tests are not a security guarantee."
        )
    if re.search(r"\b(what can you do|who are you|your purpose|about you)\b", normalized):
        return (
            "I’m Trustlincc, a code-bounty workflow agent. I create tasks, receive "
            "freelancer submissions, run listed tests in an isolated sandbox, and "
            "let the owner review passing results. Try `demo` for a live sandbox "
            "check. Payments are simulated."
        )
    return (
        "I handle code-bounty tasks rather than general questions. I can create a "
        "task, explain how to submit code, report task status, and describe the "
        "simulated approval process. Send `help` for commands or `demo` to try the "
        "sandbox."
    )


async def handle_message(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc),
            acknowledged_msg_id=msg.msg_id,
        ),
    )
    text = msg.text().strip()
    if not text:
        return

    selection = parse_selection(text)
    if selection is not None:
        await handle_selection(ctx, sender, selection)
        return

    parsed = parse_command(text)
    if parsed is None:
        await ctx.send(sender, create_text_chat(respond_to_question(text)))
        return
    command, argument, remainder = parsed

    if command == "demo":
        tests = ["add(1, 2) == 3", "add(-1, 1) == 0"]
        examples = (
            ("Correct implementation", "def add(a, b):\n    return a + b"),
            ("Incorrect implementation", "def add(a, b):\n    return a - b"),
        )
        results: list[tuple[str, dict[str, Any]]] = []
        try:
            for label, code in examples:
                results.append((label, await run_submission_tests(code, tests)))
        except TestRunnerUnavailable as error:
            await ctx.send(
                sender,
                create_text_chat(
                    f"Sandbox demo unavailable: {error}\n"
                    "No task was created and no code was executed locally."
                ),
            )
            return

        report = "\n".join(
            f"{label}: {result['tests_passed']}/{result['tests_total']} passed "
            f"({'PASS' if result['passed'] else 'FAIL'})"
            for label, result in results
        )
        await ctx.send(
            sender,
            create_text_chat(
                "Trustlincc sandbox demo (no task created; no funds move)\n"
                f"{report}\n"
                "The failing example would remain blocked from approval."
            ),
        )
        return

    if command == "create task":
        try:
            task_details = parse_task_request(text)
        except ValueError as error:
            await ctx.send(sender, create_text_chat(f"{error}\n\n{HELP_TEXT}"))
            return
        task_id = f"TASK-{uuid.uuid4().hex[:8].upper()}"
        task = {
            **task_details,
            "task_id": task_id,
            "owner": sender,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "OPEN",
            "submission": None,
            "submitter": None,
            "test_result": None,
        }
        save_task(ctx, task)
        await ctx.send(
            sender,
            create_text_chat(
                f"Task **{task_id}** created: {task['title']}\n"
                f"Bounty: {task['bounty']} (simulated)\n"
                f"Test cases recorded: {len(task['tests'])}. Submissions are checked by "
                "an isolated runner when configured, then reviewed by the owner.\n"
                f"Share `submit {task_id}` with the freelancer."
            ),
        )
        return

    if command == "submit":
        task_id = argument.upper()
        task = get_task(ctx, task_id)
        if not task:
            await ctx.send(sender, create_text_chat("Task not found. Check its TASK-... ID."))
            return
        code = remainder.strip()
        if not code:
            await ctx.send(
                sender,
                create_text_chat(f"Put the code on a new line after `submit {task_id}`."),
            )
            return
        if len(code) > MAX_SUBMISSION_LENGTH:
            await ctx.send(
                sender,
                create_text_chat(f"Submission is too large; limit is {MAX_SUBMISSION_LENGTH} characters."),
            )
            return
        if sender == task["owner"]:
            await ctx.send(sender, create_text_chat("The task owner cannot submit to their own task."))
            return
        freelancer = task["freelancer"]
        if freelancer.lower() != "any" and sender.lower() != freelancer.lower():
            await ctx.send(sender, create_text_chat("This task is assigned to a different freelancer."))
            return
        if task["status"] not in {"OPEN", "HELD"}:
            await ctx.send(
                sender,
                create_text_chat(f"Task {task_id} is {task['status']} and cannot accept a submission."),
            )
            return
        task["submission"] = code
        task["submitter"] = sender
        task["status"] = "RUNNING_TESTS"
        task["test_result"] = None
        task.pop("test_error", None)
        task["submitted_at"] = datetime.now(timezone.utc).isoformat()
        save_task(ctx, task)
        await run_task_tests(ctx, task)
        return

    if command == "test":
        task_id = argument.upper()
        task = get_task(ctx, task_id)
        if not task:
            await ctx.send(sender, create_text_chat("Task not found. Check its TASK-... ID."))
            return
        if sender not in {task["owner"], task["submitter"]}:
            await ctx.send(sender, create_text_chat("Only the task owner and submitter can rerun tests."))
            return
        if not task.get("submission"):
            await ctx.send(sender, create_text_chat("This task has no submission to test yet."))
            return
        if task["status"] == "APPROVED_SIMULATED":
            await ctx.send(sender, create_text_chat("This task is already approved and cannot be retested."))
            return
        task["status"] = "RUNNING_TESTS"
        task.pop("test_error", None)
        save_task(ctx, task)
        await run_task_tests(ctx, task)
        return

    if command == "status":
        task_id = argument.upper()
        task = get_task(ctx, task_id)
        if not task:
            await ctx.send(sender, create_text_chat("Task not found. Check its TASK-... ID."))
            return
        if sender not in {task["owner"], task["submitter"]}:
            await ctx.send(sender, create_text_chat("Only the task owner and submitter can view its status."))
            return
        result = task.get("test_result")
        test_status = (
            f"{result['tests_passed']}/{result['tests_total']} passed"
            if result
            else task.get("test_error", "not run")
        )
        await ctx.send(
            sender,
            create_text_chat(
                f"Task **{task_id}** — {task['title']}\n"
                f"Status: **{task['status']}**\n"
                f"Bounty: {task['bounty']} (simulated)\n"
                f"Sandbox tests: {test_status}."
            ),
        )
        return

    await ctx.send(sender, create_text_chat(HELP_TEXT))


def format_test_report(result: dict[str, Any]) -> str:
    summary = (
        f"Sandbox test result: {result['tests_passed']}/"
        f"{result['tests_total']} passed."
    )
    if result.get("execution_error"):
        summary += f"\nExecution error: {result['execution_error']}"
    for item in result["results"]:
        label = "PASS" if item["passed"] else "FAIL"
        summary += f"\n{label}: {item['expression']}"
        if item.get("error"):
            summary += f" — {item['error']}"
    return summary


async def run_task_tests(ctx: Context, task: dict[str, Any]) -> None:
    try:
        result = await run_submission_tests(task["submission"], task["tests"])
    except TestRunnerUnavailable as error:
        task["status"] = "SUBMITTED"
        task["test_error"] = str(error)[:500]
        save_task(ctx, task)
        await ctx.send(
            task["owner"],
            create_text_chat(
                f"Tests could not be run for {task['task_id']}: {task['test_error']}\n"
                "No code execution was confirmed, and approval is disabled until tests pass. "
                f"Configure the sandbox service or retry with `test {task['task_id']}`."
            ),
        )
        await ctx.send(
            task["submitter"],
            create_text_chat(
                f"Submission received for {task['task_id']}, but sandbox tests could not run. "
                "The owner was notified; no test result or approval was recorded."
            ),
        )
        return

    task["test_result"] = result
    task.pop("test_error", None)
    task["status"] = "TESTS_PASSED" if result["passed"] else "HELD"
    save_task(ctx, task)
    report = format_test_report(result)
    owner_text = (
        f"Review requested for **{task['task_id']} — {task['title']}**.\n"
        f"{report}\n"
        f"Submission from `{task['submitter']}`:\n```python\n{task['submission']}\n```\n"
        "This checks only the task's listed tests; the owner must review the code. "
        "Approval is simulated and moves no funds."
    )
    content: list[TextContent | MetadataContent] = [
        TextContent(type="text", text=owner_text)
    ]
    if result["passed"]:
        content.append(build_submission_card(task))
    await ctx.send(
        task["owner"],
        ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid.uuid4(),
            content=content,
        ),
    )
    await ctx.send(
        task["submitter"],
        create_text_chat(
            f"Sandbox tests for {task['task_id']}: {report}\n"
            + (
                "The owner was notified for manual review."
                if result["passed"]
                else "The task is on hold; revise the submission and submit it again."
            )
        ),
    )


async def handle_selection(ctx: Context, sender: str, selection: dict[str, str]) -> None:
    action = selection.get("action")
    task_id = selection.get("task_id", "").upper()
    task = get_task(ctx, task_id)
    if not task:
        await ctx.send(sender, create_text_chat("Task not found. Check its TASK-... ID."))
        return
    if sender != task["owner"]:
        await ctx.send(sender, create_text_chat("Only the task owner can approve or hold this task."))
        return
    if task["status"] != "TESTS_PASSED":
        await ctx.send(
            sender,
            create_text_chat(
                f"Task {task_id} is {task['status']}; sandbox tests must pass before approval. "
                f"Use `test {task_id}` to retry."
            ),
        )
        return

    if action == "approve_escrow":
        task["status"] = "APPROVED_SIMULATED"
        save_task(ctx, task)
        await ctx.send(
            sender,
            create_text_chat(
                f"Task **{task_id}** approved by its owner. This is a simulated approval only; "
                "no tests were executed and no funds or tokens were transferred."
            ),
        )
        return
    if action == "reject_escrow":
        task["status"] = "HELD"
        save_task(ctx, task)
        await ctx.send(
            sender,
            create_text_chat(
                f"Task **{task_id}** is on hold. No payout occurred. The freelancer may submit "
                "a revision with the same task ID."
            ),
        )
        return
    await ctx.send(sender, create_text_chat(HELP_TEXT))


@bounty_protocol.on_message(ChatMessage)
async def handle_chat_message(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await handle_message(ctx, sender, msg)


@bounty_protocol.on_message(ChatAcknowledgement)
async def handle_ack(_ctx: Context, _sender: str, _msg: ChatAcknowledgement) -> None:
    return
