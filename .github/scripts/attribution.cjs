const rules = require('../attribution-rules.json');

const patterns = {
  branch: rules.branch.deny.map(pattern => new RegExp(pattern, 'iu')),
  deny: rules.text.deny.map(pattern => new RegExp(pattern, 'iu')),
  warn: rules.text.warn.map(pattern => new RegExp(pattern, 'iu')),
  email: rules.identity.deny_email.map(pattern => new RegExp(pattern, 'iu')),
  name: rules.identity.deny_name.map(pattern => new RegExp(pattern, 'iu')),
};

function normalize(value) {
  return String(value ?? '').normalize('NFKC')
    .replace(/[\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u2069\uFEFF]/gu, '');
}

function inspectPullRequest({ actor, branch, title, body, commits = [] }) {
  const result = { denied: [], warnings: [] };
  if (rules.identity.exempt_actors.includes(actor)) return result;

  function check(value, field, matchers, issues) {
    for (const pattern of matchers) {
      if (pattern.test(normalize(value))) issues.push({ field, pattern: pattern.source });
    }
  }

  function text(value, field) {
    check(value, field, patterns.deny, result.denied);
    check(value, field, patterns.warn, result.warnings);
  }

  check(branch, 'branch name', patterns.branch, result.denied);
  text(title, 'PR title');
  text(body, 'PR body');
  for (const commit of commits) {
    text(commit.message, `commit ${commit.sha} message`);
    for (const role of ['author', 'committer']) {
      check(commit[role]?.name, `commit ${commit.sha} ${role} name`, patterns.name, result.denied);
      check(commit[role]?.email, `commit ${commit.sha} ${role} email`, patterns.email, result.denied);
    }
  }
  return result;
}

async function run({ github, context, core }) {
  const params = { ...context.repo, pull_number: context.payload.pull_request.number };
  const { data: pr } = await github.rest.pulls.get(params);
  if (pr.head.sha !== context.payload.pull_request.head.sha) {
    throw new Error('PR head changed before verification. Rerun the guard.');
  }
  if (rules.identity.exempt_actors.includes(pr.user.login)) {
    core.info('PR author is exempt from attribution rules.');
    return;
  }

  const commits = [];
  let cursor = null;
  let page;
  do {
    const response = await github.graphql(`
      query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
        repository(owner: $owner, name: $repo) {
          pullRequest(number: $number) {
            headRefOid
            commits(first: 100, after: $cursor) {
              totalCount
              nodes { commit { oid message author { name email } committer { name email } } }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
      }
    `, { ...context.repo, number: pr.number, cursor });
    const snapshot = response.repository.pullRequest;
    if (snapshot.headRefOid !== pr.head.sha || snapshot.commits.totalCount !== pr.commits) {
      throw new Error('PR commits changed during verification. Rerun the guard.');
    }
    page = snapshot.commits;
    for (const { commit } of page.nodes) {
      if (!commit.author || !commit.committer) throw new Error('Commit identity is missing.');
      commits.push({ sha: commit.oid, ...commit });
    }
    if (page.pageInfo.hasNextPage && (!page.pageInfo.endCursor || page.pageInfo.endCursor === cursor)) {
      throw new Error('Commit pagination did not advance.');
    }
    cursor = page.pageInfo.endCursor;
  } while (page.pageInfo.hasNextPage);

  if (commits.length !== pr.commits || new Set(commits.map(commit => commit.sha)).size !== pr.commits) {
    throw new Error('Could not verify every PR commit.');
  }
  const { data: latest } = await github.rest.pulls.get(params);
  if (latest.head.sha !== pr.head.sha || latest.head.ref !== pr.head.ref ||
      latest.title !== pr.title || latest.body !== pr.body || latest.commits !== pr.commits) {
    throw new Error('PR changed during verification. Rerun the guard.');
  }

  const result = inspectPullRequest({
    actor: pr.user.login, branch: pr.head.ref, title: pr.title, body: pr.body, commits,
  });
  for (const issue of result.warnings) core.warning(`${issue.field} matches warning pattern: ${issue.pattern}`);
  for (const issue of result.denied) core.error(`${issue.field} matches prohibited pattern: ${issue.pattern}`);
  if (result.denied.length) {
    core.setFailed(`Attribution guard found ${result.denied.length} prohibited matches.`);
  } else {
    core.info(`Attribution rules passed for the PR and all ${commits.length} commits.`);
  }
}

module.exports = { normalize, inspectPullRequest, run };
