# Local Primary V1 Shadow Runbook

This note covers a single-process macOS shadow when the Host, Codex session,
and operator terminal run as separate processes. It is for local acceptance,
not unattended production deployment.

## Process And Credential Boundaries

An exported variable belongs to the process that received it and its future
children. A value exported in one Terminal window is not visible to an already
running Codex process or Host supervisor. `launchctl setenv` is also not a
reliable bridge: Terminal, GUI applications, sandboxed commands, and escalated
shells may use different bootstrap namespaces.

The interactive `@taptap/data-skills` MCP normally uses SSO. That authenticated
session cannot be transferred to the native Host. For a non-browser Host, use
the service-provided `DVIEW_MCP_TOKEN` and map it to
`XUANJI_DVIEW_BEARER_TOKEN` only inside the Host supervisor. Never print the
token, place it on a command line, paste it into chat, or save it below a Git
checkout.

Keychain is optional. If it is used, let `security` prompt for the value so the
token never enters a command argument. Store and retrieve with the same service
and account identity, then verify only the command status:

```bash
security add-generic-password -U -a "$USER" -s DVIEW_MCP_TOKEN -w

security find-generic-password -a "$USER" -s DVIEW_MCP_TOKEN -w \
  >/dev/null
```

`The specified item could not be found in the keychain` means the exact
service/account entry is absent in that keychain. Do not assume a successful
SSO login or an environment variable created a Keychain item.

## One-Time File Handoff

When the operator terminal and Host supervisor cannot share environment state,
use a one-time file outside every repository:

```bash
umask 077
read -s "dview_token?DVIEW_MCP_TOKEN: "
echo
printf '%s' "$dview_token" \
  > /private/tmp/xuanji-primary-v1-dview-token
unset dview_token
chmod 600 /private/tmp/xuanji-primary-v1-dview-token
stat -f 'size=%z mode=%Lp owner=%Su' \
  /private/tmp/xuanji-primary-v1-dview-token
```

The operator shares only the path. Before import, the supervisor verifies that
the file is a non-empty regular file, is owned by the expected user, and has
mode `0600`. It then imports and immediately removes it without displaying the
value:

```bash
token_file=/private/tmp/xuanji-primary-v1-dview-token
test -f "$token_file" && test -s "$token_file"
test "$(stat -f %Lp "$token_file")" = 600
test "$(stat -f %Su "$token_file")" = "$USER"
export XUANJI_DVIEW_BEARER_TOKEN="$(< "$token_file")"
test "${#XUANJI_DVIEW_BEARER_TOKEN}" -ge 16
rm -f -- "$token_file"
unset token_file
```

Do not create a token `.txt` file in the project. If a handoff is interrupted,
remove the one-time file before trying again.

## Locked Local Runtime

Run from a clean `main` checkout at the exact release commit. Create an
isolated temporary environment from the committed dependency lock:

```bash
venv_root="$(mktemp -d /private/tmp/xuanji-primary-v1-venv.XXXXXX)"
python3.12 -m venv "$venv_root"
"$venv_root/bin/pip" install -r requirements.lock
```

Use a fresh private data root for each new shadow. Do not reuse an invalid or
partially finalized task as post-fix evidence:

```bash
umask 077
shadow_root="$(mktemp -d /private/tmp/xuanji-primary-v1-shadow.XXXXXX)"
mkdir -m 700 "$shadow_root/runs" "$shadow_root/tasks" \
  "$shadow_root/results" "$shadow_root/transcripts"
```

Generate the Host and receipt secrets inside the supervisor. They are distinct
from the DView token and from each other:

```bash
export XUANJI_HOST_PUBLIC_URL=http://127.0.0.1:8091
export XUANJI_HOST=127.0.0.1
export XUANJI_PORT=8091
export XUANJI_ANALYSIS_PROFILE=primary_v1
export XUANJI_HOST_BEARER_TOKEN="$(openssl rand -hex 32)"
export XUANJI_DVIEW_MCP_URL=https://dview-mcp-public.tapsvc.com/mcp/query
export XUANJI_RECEIPT_KEY_ID=primary-v1-local-shadow
export XUANJI_RECEIPT_SECRET="$(openssl rand -hex 32)"
export XUANJI_RUNS_ROOT="$shadow_root/runs"
export XUANJI_TASKS_ROOT="$shadow_root/tasks"
export XUANJI_RESULTS_ROOT="$shadow_root/results"
```

Start one localhost-only Host and retain its PID:

```bash
"$venv_root/bin/python" -m host_service.server \
  > "$shadow_root/host.log" 2>&1 &
host_pid=$!
```

Before a task, verify that an unauthenticated MCP initialize returns `401` and
that an authenticated tool listing contains exactly:

```text
xuanji_run_task
xuanji_submit_repair
xuanji_finalize
```

Do not expose the raw DView `query` tool to the model session.

## Restart And Resume

Use a fresh task ID with one immutable payload. After the first investigation
has produced its writer pack, stop and restart the Host once with the same
environment and data roots:

```bash
kill "$host_pid"
wait "$host_pid"
"$venv_root/bin/python" -m host_service.server \
  >> "$shadow_root/host.log" 2>&1 &
host_pid=$!
```

Call `xuanji_run_task` again with the same task ID and byte-equivalent payload.
The same pending action must return without repeated root queries. Continue
serially, test an identical finalize retry, then verify that a valid but
different finalize patch is rejected and does not change the task sink.

Run `scripts/primary_v1_shadow_acceptance.py` for each supported scenario. Keep
the full model-visible transcript, but report only bounded counts and the
downstream idempotency identity. Verify that task completion also returns a
stable `pipeline_handoff` with no private evidence. Never substitute a bare
`analysis_preview` for the authoritative result at:

```text
<data-root>/results/tasks/<task-id>/validated-task-result.json
```

## Cleanup

Stop the Host before clearing secrets:

```bash
kill "$host_pid"
wait "$host_pid"
unset XUANJI_HOST_BEARER_TOKEN
unset XUANJI_DVIEW_BEARER_TOKEN
unset XUANJI_RECEIPT_SECRET
unset host_pid
rm -f -- /private/tmp/xuanji-primary-v1-dview-token
```

Retain the private shadow data root only while its acceptance evidence is under
review. Delete it through an explicit, separately reviewed cleanup action; do
not embed a recursive delete in the normal run command.

The external `taptap-data-analysis` Skill is read-only input. A local shadow may
run `scripts/compile_metric_definitions.py --check` against it, but must never
modify, install, sync, or commit anything in that Skill repository.
