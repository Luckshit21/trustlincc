# Trustlincc

## Overview

Trustlincc provides a code-bounty task and review workflow over the Agentverse
chat protocol, with an optional isolated Python test runner.

## Features

- Clients can create tasks with a title, bounty label, optional freelancer address,
  and Python test expressions.
- Freelancers can submit single-file Python code for sandbox testing.
- Only the task owner can approve or hold a submitted task.
- Approval is blocked unless the configured tests pass.
- Task state is kept in uAgents storage.

## Limitations

- Only the explicitly listed test expressions are run. Passing them does not prove
  the code is correct or safe; owners must review the code and test quality.
- The runner uses a network-disabled, resource- and time-limited Modal sandbox, but
  this MVP is not a security certification.
- Approval and hold are workflow states only. No escrow contract or real funds are
  involved.
- Task data is stored in the agent's uAgents storage and is not a production
  database.
- Only single-file Python submissions using the standard library are supported.
  Dependency installation and hidden tests are not implemented.

## Run locally

In PowerShell, from this folder, create a virtual environment, install the agent
dependency, and start the agent:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install "uagents>=0.25.5"
python agent.py
```

The agent runs locally and uses the Agentverse mailbox so it can receive messages
through Agentverse. Keep the process running while chatting with it. The agent
name is fixed so uAgents can retain its local identity across restarts; keep the
generated `private_keys.json` file private and do not delete it.

## Configure the isolated test runner

The runner is a separate Modal web endpoint. It executes submissions in a
network-disabled sandbox with a 25-second command limit and bounded CPU and
memory. Do not run untrusted submissions directly inside the agent process.

1. Install the runner dependencies and authenticate to Modal:

   ```powershell
   python -m pip install -r modal_runner\requirements.txt
   modal token new
   ```

2. Generate a strong random token locally and store secrets outside the workspace
   (the workspace is in OneDrive):

   ```powershell
   $secretDir = Join-Path $env:LOCALAPPDATA "Safelincc\secrets"
   New-Item -ItemType Directory -Force -Path $secretDir | Out-Null
   $token = python -c "import secrets; print(secrets.token_urlsafe(48))"
   $runnerSecrets = Join-Path $secretDir "runner-secrets.env"
   Set-Content -Path $runnerSecrets -Value "BOUNTY_RUNNER_TOKEN=$token" -NoNewline
   modal secret create bounty-runner-auth --from-dotenv $runnerSecrets --force
   ```

   Do not paste the token into chat or commit either secrets file.
3. From this folder, deploy the endpoint:

   ```powershell
   modal deploy modal_runner\runner_app.py
   ```

   Copy the deployed `run_tests` endpoint URL.
4. Configure the hosted Agentverse agent's secrets `BOUNTY_RUNNER_URL` and
   `BOUNTY_RUNNER_TOKEN` (the same token used in the Modal Secret), then deploy
   the agent again. AVCTL accepts a dotenv file:

   ```text
   BOUNTY_RUNNER_URL=https://<your-modal-endpoint>
   BOUNTY_RUNNER_TOKEN=<the-same-token-used-in-modal>
   ```

   Save the endpoint URL and the same token in a second dotenv file under
   `$secretDir`, then upload it:

   ```powershell
   $runnerUrl = "https://<your-modal-endpoint>"
   $agentAddress = "<your-agent-address>"
   $agentverseSecrets = Join-Path $secretDir "agentverse-secrets.env"
   @("BOUNTY_RUNNER_URL=$runnerUrl", "BOUNTY_RUNNER_TOKEN=$token") |
     Set-Content -Path $agentverseSecrets
   avctl hosting add secrets --address $agentAddress --file $agentverseSecrets
   ```

   Keep both secret files outside the workspace; never paste token values into
   chat or source code.
5. Verify the agent reports per-test PASS/FAIL results on a submission. If the
   runner is unavailable, the task remains unapproved; the owner or submitter can
   use `test TASK-<id>` to retry.

## Deploy to Agentverse

From this folder, authenticate with `avctl auth login` if needed, then update and
restart the hosted agent selected in `.avctl/config.toml`:

```powershell
avctl hosting deploy
```

The deployment uploads the agent source and README. Review the configured agent
address before deploying; this command updates the existing hosted agent.

## Connect and test

The local agent and hosted Agentverse agent have different addresses. The local
agent address is reported by `http://127.0.0.1:8000/agent_info`; the hosted address
is selected in `.avctl/config.toml`. Open the **Testing** page for the address you
intend to use, and confirm the agent is active and has the Chat Protocol enabled.
Do not use a test/chat page for a different address.

To test locally, start the agent with `python agent.py`, keep it running, then
connect its mailbox in the Local Agent Inspector. To test the hosted deployment,
use its Agentverse Testing page; the local process does not receive messages sent
to the hosted address.

Send `help` for the command guide. After sending a task request, expect a reply
containing a `TASK-...` ID. If the reply is only generic help, verify the address
and the target agent's message logs.

### Run a recording-friendly demo

On the hosted agent's Testing page, send:

```text
demo
```

This sends a fixed correct and incorrect `add` implementation to the configured
Modal sandbox and displays both test results. It does not create a task or change
task state, so it can be run by one person from one account. If the runner is
unavailable, the agent reports that clearly; it never substitutes local execution.

### Create a task

Send a multiline message like:

```text
create task
title: Add a function
bounty: 0.1 FET
freelancer: any
tests:
add(1, 2) == 3
add(-1, 1) == 0
```

You can also put task fields on one line:

```text
create task title: Add a function bounty: 0.1 FET freelancer: any tests: add(1, 2) == 3 add(-1, 1) == 0
```

Each listed expression is executed against the submission. Keep one expression
per line for clarity. The agent replies with a task ID; share it with the
freelancer.

### Submit code

Send the task ID on the first line, followed by the code:

```text
submit TASK-XXXXXXXX
def add(a, b):
    return a + b
```

The agent stores the submission, runs the listed tests in the configured Modal
sandbox, and notifies both parties of the results. Failed tests put the task on
hold; a held task accepts a revised submission. If the sandbox is unavailable,
the task remains unapproved and the owner or submitter can use `test TASK-XXXXXXXX`
to retry. After all listed tests pass, the owner can send `approve TASK-XXXXXXXX`
or `hold TASK-XXXXXXXX`. Use `status TASK-XXXXXXXX` to inspect a task if you are
its owner or submitter.

Approval remains a simulated state transition only. Do not treat it as escrow or
a payment confirmation.
