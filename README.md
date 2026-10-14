# multi-agents

This project is becoming a headless runner for teams of coding agents, benchmarked against
solo agents on fresh GitHub-issue tasks (resolve rate, cost, wall time). The direction,
architecture, team-spec grammar and milestones are in [docs/ROADMAP.md](docs/ROADMAP.md);
the contracts the runner is built on are in [`agent/teams/types.py`](agent/teams/types.py).
The runner and benchmark are still under construction.

## Setup

Python 3.10+ and [uv](https://docs.astral.sh/uv/) are required.

```bash
python3 -m pip install uv
uv sync --locked --extra dev
```

## Workflow patterns

[`agent/workflows`](agent/workflows/README.md) holds the reusable orchestration patterns built
on the OpenAI Agents SDK that the team runner reuses: `SequentialWorkflow`, `RefineLoop`,
`BestOfN` and `Router`, plus refinement checkpoints and run artifacts. Each takes an injectable
runner, so they can be tested offline. See the [workflow guide](agent/workflows/README.md) for
usage.

## Development

```bash
make test         # ruff + offline Python tests
make check        # also runs the GitHub workflow and attribution tests (Node.js 22+)
make help         # list all targets
```

None of the checks need API keys.

## Archived work

The earlier story, store-concept and financial-research workflows, their evaluation harness
and the ML experiments live in the archived repository
[BichengWang/multi-agents-legacy](https://github.com/BichengWang/multi-agents-legacy).

## License

[MIT](LICENSE)
