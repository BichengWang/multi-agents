# Required checks

KW should enable an active branch ruleset for `main` after these workflows land
and both check names have appeared. Require `ci-gate` and `attribution-guard`,
each pinned to the GitHub Actions integration (ID `15368`), with an empty bypass
list. Require pull requests; prohibit force pushes and
branch deletion. No approval count is needed for this slice.

`ci-gate` runs on every PR and merge group, calls the reusable test workflow,
and includes the offline attribution replay. The final job runs even after
failures and accepts only successful or skipped dependencies.
`attribution-guard` reads code and rules from the base branch and checks
PR metadata and all commit messages and identities through the API. Denied
patterns fail; warning patterns annotate the run. Dependency update PRs from
the exempt actor pass. Branch names follow `<area>/<topic>`; generic area
names are permitted by the shared vendor patterns.

The command below creates the ruleset. It has **not** been applied. Run from
this repository after reviewing the JSON. This ruleset does not enable a merge
queue; the attribution workflow currently checks PRs only.

```sh
gh api --method POST repos/BichengWang/multi-agents/rulesets \
  -H 'Accept: application/vnd.github+json' \
  -H 'X-GitHub-Api-Version: 2022-11-28' \
  --input - <<'JSON'
{
  "name": "main required checks",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {
    "ref_name": {"include": ["refs/heads/main"], "exclude": []}
  },
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {
      "type": "pull_request",
      "parameters": {
        "dismiss_stale_reviews_on_push": false,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_approving_review_count": 0,
        "required_review_thread_resolution": false
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          {"context": "ci-gate", "integration_id": 15368},
          {"context": "attribution-guard", "integration_id": 15368}
        ]
      }
    }
  ]
}
JSON
```

Run the replay locally with `node --test .github/scripts/*.test.cjs` in a full
checkout. It checks the last 30 commits, merge-subject branches and local refs.
The branch fixture preserves PR metadata read from the public API for deleted
or squashed branches (#32 merged; #33–#36 were closed and replaced).
