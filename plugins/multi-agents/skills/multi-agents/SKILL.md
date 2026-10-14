---
name: multi-agents
description: Hand a coding task to a team of coding agents (Claude Code, Codex) that work in isolated git worktrees and return a patch plus a cost record. Use for hard or ambiguous tasks, when you want a second opinion from the other CLI, or to compare two CLIs on the same task. Not for small edits you can finish yourself.
---

# Hand a task to a team

`multi-agents` runs a team of coding agents on a git repository and returns **one patch and an
itemized record** (tokens, cost, seconds per agent call). Each worker gets its own git worktree,
so your working tree is untouched until you apply the patch.

## When to use a team

Use it when a second agent is worth the extra minutes and cost:

- the task is hard, ambiguous, or you already failed at it once;
- the user wants a second opinion, or a patch checked by a different model;
- the user wants to compare Claude Code and Codex on the same task.

Do not use it for small edits, questions, or anything you can finish quickly yourself. A team costs
real money and takes minutes. Say so when you start one.

## Pick a spec

`H` is a harness with an optional model: `claude`, `codex`, `claude@haiku`, `codex@gpt-6-luna`.

| Spec | Use it when |
|---|---|
| `solo:worker=H` | You only want the other CLI to try, or the other CLI is not installed. Cheapest. |
| `review:coder=H,reviewer=H[,rounds=N]` | **Default.** One agent codes, another reviews the patch and sends feedback; the best-scoring patch wins. |
| `best:workers=H+H[+H...][,judge=H]` | The user wants a comparison: workers run in parallel and a judge picks the best patch. |
| `plan:planner=H,workers=H[+H...][,integrator=H][,max_tasks=N]` | A large task that splits into independent parts: a planner splits it, workers solve the parts in parallel, an integrator merges. Newer CLI only; use it only when the user asks or the task clearly splits. |

**Default (cheap):** the coder is the *other* CLI than the one you are running in, so the patch is
an independent attempt. The reviewer is *your own* CLI, which is certainly installed and logged in.
Both use small models and two rounds:

- running in Claude Code: `review:coder=codex@gpt-6-luna,reviewer=claude@haiku,rounds=2`
- running in Codex: `review:coder=claude@haiku,reviewer=codex@gpt-6-luna,rounds=2`

If the other CLI is not installed (`command -v codex` / `command -v claude`), use
`solo:worker=<your CLI>@<small model>` instead. Use larger models or `best:` only when the user asks
or the task justifies the cost. If the user gives a spec, use it as written.

### Add an objective check

If the project has a test, build or lint command, use its real invocation (from the README, CI
config or Makefile; check that it runs here, do not guess) and pass it with `--check '<command>'`.
The final patch is run through it in a fork and the record says whether it passed. The `check`
harness can replace an LLM reviewer or judge, which makes the loop test-driven and cheaper:

```text
review:coder=codex@gpt-6-luna,reviewer=check,rounds=2   --check 'pytest -q'
```

`--check` is newer than the first release of the CLI. If the CLI answers that it does not know
`--check` or the `check` harness, drop it and use an LLM reviewer.

## Run it

Run it from the repository the task is about, with the shell tool. Quote the task; put the whole
task in it, since the workers do not see this conversation:

```bash
uvx --from git+https://github.com/BichengWang/multi-agents multi-agents run \
  --repo . --timeout 600 --team '<spec>' '<task>'
```

Single-quote the task, and replace every `'` inside it with `'\''`. Task text often contains
backticks or `$`, which a double-quoted shell argument would execute or expand.
`--timeout 600` bounds each agent call to ten minutes.

Add `--check '<command>'` when you have one. Requirements: `uv`, `git`, and a logged-in `claude`
and/or `codex` (the CLIs use their own login; no API keys are needed). The repository needs at least one commit; workers start from
`HEAD`, so **uncommitted changes are not visible to them**. Tell the user if the tree is dirty.

The run needs network access (to fetch the CLI and for the worker CLIs), write access to
`~/.cache/multi-agents` and `~/.cache/uv`, and permission to start `claude` and `codex`. If your
shell is sandboxed or asks for approval, request that this one command run outside the sandbox. If
it cannot run (no network, approval denied, a CLI is not logged in), **stop and tell the user why**.
Never fall back to doing the task yourself under this skill: the user asked for a team.

A run takes one to several minutes. Run it in the background (or with the shell tool's longest
timeout) and wait for it. Do not kill it early: the worker CLIs run in their own process groups and
would keep running and billing. Do not start a second run while one is going.

## Read the summary

The command prints:

- one row per agent call: role (`worker`, `coder`, `reviewer`, `judge`, and for `plan` `planner`, `integrator`), `harness@model`, tokens
  in/cached/out, cost, seconds; a red `x` marks a call that exited non-zero;
- `Total` tokens, cost and seconds;
- `scores` (0-10, one per attempt or candidate), `chosen`, `best_round`, `accepted`; low scores or
  `accepted: False` mean the reviewer was not satisfied;
- `failure:` lines for calls that crashed or returned something unusable;
- the files changed with `+`/`-` counts, the `Record:` and `Patch:` paths, and the `Apply:` command.

`No changes were produced.` is exit code 1 and has two causes. With `failure:` lines or red `x`
rows, the workers failed (missing or logged-out CLI, sandbox, timeout): report the error, do not
say the team found nothing to change. Without them, the team found nothing to change.
When a check ran,
the summary and the `check` entry of `record.json` say whether it passed; treat a failing check as a
failed run.

## Review, then apply

Never apply the patch silently.

1. Report the team, the cost, the scores, the check result if any, and the files changed.
2. Show the patch (`final.patch` at the printed path). Read it yourself and say whether it does what
   the user asked, and anything that looks wrong or out of scope.
3. Ask whether to apply it. Apply it only after the user says yes, or if they already told you to
   apply the result. Use the printed command: `git -C <repo> apply <path>/final.patch`. It changes
   the working tree only; do not commit unless asked.

If the patch is wrong, say so and offer another run with a different spec or a sharper task instead
of editing the patch by hand.
