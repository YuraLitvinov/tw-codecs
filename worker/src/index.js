// Starts the build-matrix workflow when openSUSE publishes a new Tumbleweed snapshot.
//
// Cloudflare cron triggers fire on time, unlike GitHub scheduled workflows. The Worker only
// decides *when* to start a run; the workflow's own `snapshot` job still compares the id with
// snapshot.txt, so a spurious dispatch is cheap and harmless.
//
// The Worker keeps no state: it dispatches whenever the current id differs from snapshot.txt on
// the default branch (written by the workflow's `record` job after a successful publish) and no
// run of the workflow is queued or in progress. Without that second check every tick during a
// multi-hour build would dispatch again. A build that fails is not recorded, so it is
// dispatched again on the next tick after the failed run completes.

const MEDIA_URL = "https://download.opensuse.org/tumbleweed/repo/oss/media.1/media";
const ID_PATTERN = /^openSUSE-\d{8}-\S+$/;

export function parseMedia(text) {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  if (lines.length < 2 || !ID_PATTERN.test(lines[1])) {
    throw new Error(`unexpected media.1/media content: ${JSON.stringify(text.slice(0, 200))}`);
  }
  return lines[1];
}

async function github(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "User-Agent": "packmanless-snapshot-watcher",
      "X-GitHub-Api-Version": "2022-11-28",
      ...init.headers,
    },
  });
}

async function recordedSnapshot(env) {
  // The query string defeats the raw.githubusercontent cache (about 5 minutes).
  const url = `https://raw.githubusercontent.com/${env.GITHUB_REPO}/refs/heads/${env.GITHUB_REF}/snapshot.txt?t=${Date.now()}`;
  const r = await fetch(url, { cf: { cacheTtl: 0 } });
  if (r.status === 404) return ""; // first run: nothing recorded yet
  if (!r.ok) throw new Error(`reading snapshot.txt: HTTP ${r.status}`);
  return (await r.text()).split("\n")[0].trim();
}

// Runs that have not completed: "queued", "in_progress", "waiting", "pending", "requested".
async function activeRuns(env) {
  const r = await github(env, `actions/workflows/${env.WORKFLOW}/runs?per_page=20`);
  if (!r.ok) throw new Error(`listing runs: HTTP ${r.status}`);
  return (await r.json()).workflow_runs.filter((run) => run.status !== "completed");
}

export async function check(env) {
  const media = await fetch(MEDIA_URL, { cf: { cacheTtl: 0 } });
  if (!media.ok) throw new Error(`media.1/media: HTTP ${media.status}`);
  const current = parseMedia(await media.text());

  if (current === (await recordedSnapshot(env))) return `up to date (${current})`;

  const active = await activeRuns(env);
  if (active.length) return `build already running (${current}, run ${active[0].id})`;

  const r = await github(env, `actions/workflows/${env.WORKFLOW}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: env.GITHUB_REF, inputs: { force: "false", full: "false" } }),
  });
  if (r.status !== 204) throw new Error(`dispatch: HTTP ${r.status} ${await r.text()}`);
  return `dispatched ${current}`;
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(check(env).then((msg) => console.log(msg)));
  },

  // GET the Worker URL to run a check by hand and see the result.
  async fetch(_request, env) {
    try {
      return new Response((await check(env)) + "\n");
    } catch (e) {
      return new Response(`error: ${e.message}\n`, { status: 502 });
    }
  },
};
