// The readthrough canvas: the live dashboard beside an agent session, in any Copilot host that renders canvases.
//
// `readthrough install canvas` puts this directory under the Copilot home's `extensions/`; the Copilot runtime
// loads it into each session, with its own copy of the SDK. It declares one canvas whose page is the dashboard
// the user already runs (`readthrough serve`), so the view is the same page a browser shows, told through
// `canvas=<instance>` in its URL that an agent session sits beside it. The agent steers it with the `show`
// action, and reads a PR's brief or study log, all through the readthrough CLI; nothing is written here.
//
// Experimental: the canvas API is marked so in the SDK, and may change under this file.

import { CanvasError, createCanvas, joinSession } from "@github/copilot-sdk/extension";

import { ACTIONS, CliError, OPEN_SCHEMA, Readthrough, text } from "./readthrough.mjs";

const canvasId = "readthrough";
let session;
const log = (message, level = "info") => {
  if (session) session.log(message, { level }).catch(() => {});
};
const readthrough = new Readthrough({ log });
// The repo each open instance shows, so an action on that instance means that repo and not the CLI's default.
// Filled by open(), which the host calls again for every instance it restores after a reload.
const showing = new Map();

const toCanvasError = error =>
  error instanceof CliError ? new CanvasError(error.code, error.message) : new CanvasError("readthrough_error", String(error?.message ?? error));

/** A stable instance id is also the page's name on its event stream, so steering reaches this view alone. */
const clientId = instanceId => {
  const id = `canvas-${String(instanceId).replace(/[^A-Za-z0-9._:-]/g, "-")}`.slice(0, 64);
  return text(id, "instance", { max: 64, pattern: /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/ });
};

const canvas = createCanvas({
  id: canvasId,
  displayName: "readthrough",
  description: "The live readthrough dashboard: a repo's open pull requests laid out on its architecture, with each one's brief and study log, beside the deep-review session.",
  inputSchema: OPEN_SCHEMA,
  actions: ACTIONS.map(action => ({
    name: action.name,
    description: action.description,
    inputSchema: action.inputSchema,
    handler: async ctx => {
      try {
        const input = { repo: showing.get(ctx.instanceId), ...(ctx.input ?? {}) };
        if (input.repo === undefined && !showing.has(ctx.instanceId)) {
          throw new CliError("readthrough_not_open", "This canvas isn't open yet; open it first, or name the repo.");
        }
        return await action.run(readthrough, input, { ...ctx, instanceId: clientId(ctx.instanceId) });
      } catch (error) {
        throw toCanvasError(error);
      }
    },
  })),

  async open(ctx) {
    const input = ctx.input ?? {};
    let repo;
    try {
      repo = readthrough.pick(await readthrough.repos(), input.repo);
    } catch (error) {
      throw toCanvasError(error);
    }
    const client = clientId(ctx.instanceId);
    showing.set(ctx.instanceId, repo.slug);
    // The URL is the same every time this instance opens, so re-opening focuses the view instead of reloading
    // it; where the page should look is said separately, once it is up.
    const url = `${repo.url}#map?canvas=${encodeURIComponent(client)}`;
    if (input.pr !== undefined && input.pr !== null) {
      readthrough
        .show({ repo: repo.slug, pr: input.pr, panel: input.panel, client })
        .then(result => log(`${repo.name}: ${result}`))
        .catch(error => log(`${repo.name}: couldn't point the page at #${input.pr}: ${error.message}`, "warning"));
    }
    const built = repo.built ? `built ${new Date(repo.built).toLocaleString()}` : "not built yet";
    const renders = ctx.host?.capabilities?.canvases !== false;
    return {
      url,
      title: `readthrough · ${repo.name}`,
      status: renders ? built : `${built} · this host doesn't render canvases; open ${repo.url} in a browser`,
    };
  },

  onClose(ctx) {
    // The dashboard is the user's, served for every page that shows it; closing a view stops nothing.
    showing.delete(ctx.instanceId);
    log(`canvas ${ctx.instanceId} closed`);
  },
});

try {
  session = await joinSession({ canvases: [canvas] });
} catch (error) {
  // An older runtime without canvases: there is nothing for this extension to do, and nothing to break.
  process.stderr.write(`readthrough canvas: not loaded (${error?.message ?? error})\n`);
}
