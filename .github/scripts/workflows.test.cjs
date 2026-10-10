const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

function workflow(name) {
  return readFileSync(path.join(__dirname, '../workflows', name), 'utf8');
}

test('ci-gate accepts only successful or skipped dependencies', () => {
  const script = /node <<'NODE'\n([\s\S]*?)\n\s*NODE/u.exec(workflow('ci-gate.yml'))[1];
  for (const [results, expected] of [
    [['success', 'success'], 0], [['success', 'skipped'], 0], [['skipped', 'skipped'], 0],
    [['success', 'failure'], 1], [['success', 'cancelled'], 1], [['success', 'pending'], 1], [[], 1],
  ]) {
    const needs = Object.fromEntries(results.map((result, index) => [`job${index}`, { result }]));
    const actual = spawnSync(process.execPath, ['-e', script], {
      env: { ...process.env, NEEDS_JSON: JSON.stringify(needs) }, encoding: 'utf8',
    });
    assert.equal(actual.status, expected, actual.stderr);
  }
});

test('auto-merge retries pending mergeability and stops on conflicts or closed PRs', async () => {
  const script = /script: \|\n([\s\S]*?)\n\s*- name: Enable automerge/u.exec(workflow('auto-merge.yml'))[1];
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const execute = new AsyncFunction('github', 'context', 'core', 'setTimeout', script);
  for (const [states, open, labeled, expected, failures] of [
    [[null, null, true], true, true, 'true', 0],
    [[true], true, true, 'true', 0],
    [[false], true, true, undefined, 1],
    [[null], true, true, undefined, 1],
    [[true], false, true, undefined, 1],
    [[null], true, false, 'false', 0],
  ]) {
    let reads = 0;
    let delays = 0;
    const output = {};
    const errors = [];
    const github = { rest: { pulls: { get: async () => ({ data: {
      mergeable: states[Math.min(reads++, states.length - 1)],
      state: open ? 'open' : 'closed', labels: labeled ? [{ name: 'auto-merge' }] : [],
    } }) } } };
    const core = { info() {}, setFailed: message => errors.push(message), setOutput: (key, value) => { output[key] = value; } };
    const context = { repo: { owner: 'BichengWang', repo: 'multi-agents' }, payload: { pull_request: { number: 44 } } };
    await execute(github, context, core, callback => { delays++; callback(); });
    assert.equal(output.should_automerge, expected);
    assert.equal(errors.length, failures);
    assert.equal(reads, states[0] === null && labeled ? (states.length > 1 ? states.length : 6) : 1);
    assert.equal(delays, reads - 1);
  }
});

test('the target workflow checks out only the base commit without persisted credentials', () => {
  const guard = workflow('attribution-guard.yml');
  assert.match(guard, /pull_request_target:/u);
  assert.match(guard, /ref: \$\{\{ github.event.pull_request.base.sha \}\}/u);
  assert.match(guard, /persist-credentials: false/u);
  assert.doesNotMatch(guard, /pull_request.head|pull_request.body|pull_request.title/u);
});
