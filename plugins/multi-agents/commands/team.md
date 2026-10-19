---
description: Hand a task to a team of coding agents and review the patch they return
argument-hint: "[spec] <task>"
allowed-tools: Bash(uvx --from git+https://github.com/BichengWang/multi-agents multi-agents run:*), Bash(git status:*), Bash(command -v:*), Read
---

Run a team of coding agents on this task, following the `multi-agents` skill from this plugin
(spec choice, how to run, how to read the summary, and how to review before applying):

$ARGUMENTS

If the first word above looks like a team spec (it starts with `solo:`, `review:`, `best:` or
`plan:`), use it as `--team` and treat the rest as the task. Otherwise use the skill's default spec
for Claude Code. The target repository is the current directory (`--repo .`). Add `--max-cost USD`
only if the user gave a budget.

If there is no task text, ask the user for one instead of running anything. Show the patch and ask
before applying it, unless the user said to apply.
