const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { mkdtempSync, readFileSync, rmSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
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

test('auto-merge retries pending mergeability, rejects conflicts, and skips closed or merged PRs', async () => {
  const script = /script: \|\n([\s\S]*?)\n\s*- name: Enable automerge/u.exec(workflow('auto-merge.yml'))[1];
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const execute = new AsyncFunction('github', 'context', 'core', 'setTimeout', script);
  for (const [states, open, labeled, expected, failures, merged = false] of [
    [[null, null, true], true, true, 'true', 0],
    [[true], true, true, 'true', 0],
    [[null, true], true, [true, false], 'false', 0],
    [[false], true, true, undefined, 1],
    [[null], true, true, undefined, 1],
    [[true], false, true, 'false', 0],
    [[null], false, true, 'false', 0],
    [[null], false, true, 'false', 0, true],
    [[null, null], [true, false], true, 'false', 0, true],
    [[null], true, false, 'false', 0],
  ]) {
    let reads = 0;
    let delays = 0;
    const output = {};
    const errors = [];
    const github = { rest: { pulls: { get: async () => {
      const index = reads++;
      const hasLabel = Array.isArray(labeled) ? labeled[Math.min(index, labeled.length - 1)] : labeled;
      const isOpen = Array.isArray(open) ? open[Math.min(index, open.length - 1)] : open;
      return { data: {
        mergeable: states[Math.min(index, states.length - 1)],
        state: isOpen ? 'open' : 'closed',
        merged: merged && !isOpen, labels: hasLabel ? [{ name: 'auto-merge' }] : [],
      } };
    } } } };
    const core = { info() {}, setFailed: message => errors.push(message), setOutput: (key, value) => { output[key] = value; } };
    const context = { repo: { owner: 'BichengWang', repo: 'multi-agents' }, payload: { pull_request: { number: 44 } } };
    await execute(github, context, core, callback => { delays++; callback(); });
    assert.equal(output.should_automerge, expected);
    assert.equal(errors.length, failures);
    assert.equal(reads, states[0] === null && labeled && open !== false ? (states.length > 1 ? states.length : 6) : 1);
    assert.equal(delays, reads - 1);
  }
});

test('auto-merge handles concurrent merges and reports other errors', () => {
  const step = workflow('auto-merge.yml').split('- name: Enable automerge')[1];
  const script = /        run: (?:\|\n([\s\S]*?)\n        env:|([^\n]+))/u.exec(step);
  const bash = (script[1] ? script[1].replace(/^          /gm, '') : script[2])
    .replaceAll('${{ github.repository }}', 'BichengWang/multi-agents')
    .replaceAll('${{ github.event.pull_request.number }}', '44');
  const baseChanged = 'GraphQL: Base branch was modified. Review and try the merge again. (mergePullRequest)';
  for (const [responses, expected] of [
    [['Merged'], 0],
    [[baseChanged, 'Merged'], 0],
    [[baseChanged, baseChanged, 'Merged'], 0],
    [['GraphQL: Merge already in progress (mergePullRequest)'], 0],
    [['Pull request is already merged'], 0],
    [['GraphQL: Resource not accessible by integration'], 1],
    [[baseChanged], 1],
    [[baseChanged, 'GraphQL: Permission denied'], 1],
  ]) {
    const dir = mkdtempSync(path.join(tmpdir(), 'automerge-'));
    try {
      writeFileSync(path.join(dir, 'gh'), `#!${process.execPath}
const fs = require('node:fs');
const calls = process.env.CALLS;
const count = fs.existsSync(calls) ? fs.readFileSync(calls, 'utf8').trim().split('\\n').length : 0;
fs.appendFileSync(calls, JSON.stringify(process.argv.slice(2)) + '\\n');
const responses = JSON.parse(process.env.RESPONSES);
const response = responses[Math.min(count, responses.length - 1)];
console[response === 'Merged' ? 'log' : 'error'](response);
process.exit(response === 'Merged' ? 0 : 1);
`, { mode: 0o755 });
      writeFileSync(path.join(dir, 'sleep'), '#!/bin/sh\nprintf "sleep\\n" >> "$SLEEPS"\n', { mode: 0o755 });
      const actual = spawnSync('bash', ['--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', bash], {
        env: {
          ...process.env, PATH: `${dir}:${process.env.PATH}`, RESPONSES: JSON.stringify(responses),
          CALLS: path.join(dir, 'calls'), SLEEPS: path.join(dir, 'sleeps'),
        },
        encoding: 'utf8', timeout: 10000,
      });
      assert.equal(actual.status, expected, `${responses}: ${actual.stdout}${actual.stderr}`);
      const calls = readFileSync(path.join(dir, 'calls'), 'utf8').trim().split('\n').map(JSON.parse);
      if (responses.length > 1) assert.equal(calls.length, responses.length);
      else if (responses[0] === baseChanged) assert.ok(calls.length > 1 && calls.length <= 10);
      else assert.equal(calls.length, 1);
      if (calls.length > 1) {
        assert.equal(readFileSync(path.join(dir, 'sleeps'), 'utf8').trim().split('\n').length, calls.length - 1);
      }
      if (expected !== 0) assert.ok(`${actual.stdout}${actual.stderr}`.includes(responses.at(-1)));
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }
});
