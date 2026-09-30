#!/usr/bin/env node
/**
 * End-to-end check for the external-harness dispatch lane — with fake CLIs,
 * real drivers, and no model call anywhere.
 *
 *   node scripts/check-drivers.mjs
 *
 * What it proves, in order:
 *   1. the route carries a real driver and dispatch *executes* it: a claude
 *      node, a jcode node on the `default` arm (no model flag must reach the
 *      tool), and a muse node whose prompt rides the 0600 prompt file;
 *   2. the collected contract is lane-independent: log → result promotion,
 *      the ACCEPTANCE verdict, and the report.json are the same whether the
 *      worker ran in a tmux window or as a plain detached spawn;
 *   3. the watchdog kills a worker that never exits — `kill-session` on the
 *      lens, whole process group on the plain lane — and rlp_cancel stops the
 *      rest without touching finished nodes;
 *   4. a harness the catalog cannot build a driver for fails *its node* with
 *      the catalog's word, and the rest of the wave still dispatches.
 *
 * The fakes are shell scripts on a sandbox PATH writing RLP's worker contract
 * verbatim (they read the report path out of the prompt, exactly as a real
 * worker is told to). Their binaries are referenced by ABSOLUTE path in the
 * route, so even a pre-existing tmux server with the user's own environment
 * can only ever run a fake — this check must never invoke a real coding tool.
 *
 * It loads the extension the way pi does (jiti) and calls the tools directly.
 * That is not a shortcut around the harness: check-harness.mjs proves the
 * commands load in a real session, and what is being proven *here* is the
 * driver lane's spawn/collect/kill machinery, which is a function of argv and
 * files, not of pi's TUI. No engine warmup, no laya, no credential: the
 * sandbox ladder gates hybrid and the routes are hand-built from the real
 * Python catalog (`rlp_svc.harnesses.driver_for`).
 *
 * tmux convention: when `tmux -V` is absent the lens lane cannot run and the
 * check says so and continues on the plain lane (`selftest.sh` runs this on
 * hosts without tmux, and CI is one).
 */
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);

const failures = [];
const notes = [];
const bad = (text) => failures.push(text);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --- sandbox --------------------------------------------------------------------

const SANDBOX = mkdtempSync(join(tmpdir(), "rlp-drivers-"));
const BIN = join(SANDBOX, "bin"); // kept on PATH for `which`, but routes use absolute paths
const DUMP = join(SANDBOX, "argv");
const HOME = join(SANDBOX, "home");
const RLP_HOME = join(HOME, ".rlp");
const AGENT = join(HOME, ".rlp", "agent");
const REPO = join(SANDBOX, "repo");
for (const d of [BIN, DUMP, AGENT, RLP_HOME, REPO]) mkdirSync(d, { recursive: true });

const LADDER = join(AGENT, "orchestration.json");
writeFileSync(
	LADDER,
	JSON.stringify({ brain: null, workers: [{ id: "pi", harness: "pi", models: [] }], routing: { gate: "hybrid", tmux: "auto" } }, null, 2),
);

// Environment: set before the extension module is imported, because its
// RLP_HOME and IS_BRAIN consts read process.env at load time.
process.env.RLP_IDENTITY = "rlp";
process.env.RLP_HOME = RLP_HOME;
process.env.RLP_CODING_AGENT_DIR = AGENT;
process.env.RLP_ORCHESTRATION = LADDER;
process.env.HOME = HOME;
delete process.env.RLP_DIRECT;
process.env.PATH = `${BIN}:${process.env.PATH}`;

// --- the fakes -------------------------------------------------------------------

/**
 * A worker contract with no model: dump its argv (the prompt body replaced by
 * a marker so assertions read a token list, not 10 KB of prose), read the
 * report path out of the prompt exactly where workerPrompt states it, write
 * report.json, print the ACCEPTANCE line, exit. muse reads its prompt from the
 * file lane instead, so its branch follows `--prompt-file`.
 */
function fakeScript(name) {
	const dq = (p) => JSON.stringify(p); // shell-double-quoted absolute path
	const read =
		name === "muse"
			? `printf '%s\\n' "$@" > ${dq(join(DUMP, `${name}.argv`))}
prev=""
for a in "$@"; do
	[ "$prev" = "--prompt-file" ] && prompt=$(cat "$a")
	prev="$a"
done`
			: `{ for a in "$@"; do case "$a" in *"You are a RLP worker"*) echo "<PROMPT masked>";; *) echo "$a";; esac; done; } > ${dq(join(DUMP, `${name}.argv`))}
for a in "$@"; do prompt="$a"; done`;
	const path = join(BIN, name);
	writeFileSync(
		path,
		`#!/bin/sh
prompt=""
${read}
rp=$(printf '%s' "$prompt" | sed -n 's/.*exactly this path: \\([^[:space:]]*\\).*/\\1/p')
if [ -n "$rp" ]; then
	printf '{"status":"done","acceptance":"pass","acceptance_note":"fake ${name} run","files":["worker-proof.txt"],"commands":["fake ${name} -> ok"],"summary":"fake worker for check-drivers"}\\n' > "$rp"
fi
touch worker-proof.txt
echo "fake ${name} ran here"
echo "ACCEPTANCE: pass — fake ${name} evidence"
exit 0
`,
	);
	chmodSync(path, 0o755);
}
function sleepyScript(name) {
	const path = join(BIN, name);
	writeFileSync(path, `#!/bin/sh\necho "sleepy ${name}: never exiting"\nexec sleep 3600\n`);
	chmodSync(path, 0o755);
}
fakeScript("claude");
fakeScript("jcode");
fakeScript("muse");
sleepyScript("sleepy");

// --- the repo the workers run in ---------------------------------------------------

const git = (args) => {
	const r = spawnSync("git", args, {
		cwd: REPO,
		encoding: "utf8",
		env: { ...process.env, GIT_AUTHOR_NAME: "check", GIT_AUTHOR_EMAIL: "c@x", GIT_COMMITTER_NAME: "check", GIT_COMMITTER_EMAIL: "c@x" },
	});
	if (r.status !== 0) throw new Error(`git ${args.join(" ")}: ${r.stderr}`);
	return r.stdout;
};
git(["init", "-q"]);
writeFileSync(join(REPO, "seed.txt"), "seed\n");
git(["add", "seed.txt"]);
git(["commit", "-qm", "seed"]);

// --- real drivers from the Python catalog ------------------------------------------

const svcPy = spawnSync("sh", [join(ROOT, "scripts", "svc-py")], { encoding: "utf8" }).stdout.trim();
if (!svcPy) fail("check-drivers: scripts/svc-py resolved nothing — run sh scripts/install.sh first");
function fail(msg) {
	console.error(`check-drivers: ${msg}`);
	process.exit(1);
}
const driverOut = spawnSync(
	svcPy,
	[
		"-c",
		`import json, sys
from rlp_svc import harnesses
arms = {"claude": "claude/sonnet", "jcode": "jcode/default", "muse": "muse/muse-2"}
print(json.dumps({h: harnesses.driver_for(h, harnesses.native_model(a, h), harnesses.resolve_binary(h)) for h, a in arms.items()}))`,
	],
	{ encoding: "utf8", env: { ...process.env, HOME, PATH: `${BIN}:${process.env.PATH}` } },
);
if (driverOut.status !== 0) fail(`catalog driver_for failed: ${driverOut.stderr.slice(-400)}`);
const drivers = JSON.parse(driverOut.stdout);
for (const h of ["claude", "jcode", "muse"]) {
	if (!drivers[h]) fail(`driver_for(${h}) returned nothing — the catalog must carry a driver for it`);
	drivers[h].binary = join(BIN, h); // absolute: no PATH (not even the tmux server's) can reach a real tool
}
const sleepyDriver = { ...drivers.claude, binary: join(BIN, "sleepy"), harness: "claude" };

// --- plan files ---------------------------------------------------------------------

const tmuxHere = spawnSync("tmux", ["-V"], { encoding: "utf8" }).status === 0;
if (!tmuxHere) notes.push("no tmux on this host — the lens lane is skipped, the plain lane is fully checked");

const REQ_A = "check-drivers: three fake tools, one wave";
const REQ_B = "check-drivers: watchdog and a ghost";
const REQ_C = "check-drivers: cancel a running worker";

function route(arm, harness, driver, extra = {}) {
	return { agent: "pi", arm, harness, model_family: arm.split("/")[0], purpose: "implement", credential: "unknown", driver, ...extra };
}
function task(id) {
	return { id, title: `${id} via fake tool`, brief: `Do the one thing node ${id} was dispatched for.`, acceptance: `node ${id} produced worker-proof.txt`, depends_on: [] };
}
function writePlan(request, routes, workerTimeoutMs, tmux) {
	const plan = {
		mode: "orchestrate",
		request,
		tasks: Object.keys(routes).map(task),
		routes,
		waves: [Object.keys(routes)],
		max_dispatches_per_turn: 8,
		worker_timeout_ms: workerTimeoutMs,
		tmux,
		planning: { artifactPassing: true },
	};
	writeFileSync(join(RLP_HOME, "last-plan.json"), `${JSON.stringify(plan, null, 2)}\n`);
}
writePlan(
	REQ_A,
	{
		w1: route("claude/sonnet", "claude", drivers.claude),
		w2: route("jcode/default", "jcode", drivers.jcode),
		w3: route("muse/muse-2", "muse", drivers.muse),
	},
	120_000,
	tmuxHere ? "auto" : "off",
);

// --- load the extension the way pi does -----------------------------------------------

const { createJiti } = require(join(ROOT, "fork", "pi", "node_modules", "jiti"));
const jiti = createJiti(import.meta.url, {
	alias: { typebox: require.resolve("typebox", { paths: [join(ROOT, "fork", "pi", "node_modules")] }) },
});
const mod = await jiti.import(join(ROOT, "agent", "rlp", "extensions", "rlp-orchestrate.ts"));
const tools = new Map();
const commands = new Map();
mod.default({
	registerTool: (t) => tools.set(t.name, t),
	registerCommand: (name, c) => commands.set(name, c),
	on: () => {},
});
for (const need of ["rlp_dispatch", "rlp_collect", "rlp_watch", "rlp_state", "rlp_cancel"]) {
	if (!tools.has(need)) fail(`extension registered no tool ${need}`);
}
if (!commands.has("rlp-watch")) fail("extension registered no /rlp-watch command");

const ctx = { cwd: REPO };
const call = async (name, params) => {
	const r = await tools.get(name).execute(`t-${name}`, params, undefined, undefined, ctx);
	return r.content.map((c) => c.text).join("\n");
};

// --- 1. dispatch and collect the three-lane wave ----------------------------------------

const runIdRe = /Run (\d{8}-\d{6})/;
const dA = await call("rlp_dispatch", { nodes: ["w1", "w2", "w3"], request: REQ_A, waves: [[`w1`, `w2`, `w3`]] });
const runA = dA.match(runIdRe)?.[1];
if (!runA) fail(`dispatch produced no run id: ${dA.slice(0, 200)}`);
for (const id of ["w1", "w2", "w3"]) {
	if (!dA.includes(`${id}: started`)) bad(`dispatch line for ${id} missing:\n${dA}`);
}
if (tmuxHere) {
	for (const id of ["w1", "w2", "w3"]) {
		if (!dA.includes(`attach: tmux -L rlp attach -t rlp-${runA}-${id}`)) bad(`${id} did not get a window on the rlp socket:\n${dA}`);
	}
} else if (!dA.includes("pid")) bad("plain lane should still report a pid per node");

const cA = await call("rlp_collect", { waitMs: 25_000 });
for (const id of ["w1", "w2", "w3"]) {
	const block = cA.split(`### ${id}:`)[1] ?? "";
	if (!block.includes("status: done")) bad(`${id} did not collect as done: ${block.slice(0, 160) || "(absent)"}`);
	if (!block.includes("verdict: pass")) bad(`${id} lost its ACCEPTANCE verdict:\n${block.slice(0, 300)}`);
	if (!block.includes("report: status=done acceptance=pass")) bad(`${id}'s report.json was not read:\n${block.slice(0, 300)}`);
}

const dirA = join(RLP_HOME, "runs", runA);
const ledgerA = JSON.parse(readFileSync(join(dirA, "ledger.json"), "utf8"));
for (const id of ["w1", "w2", "w3"]) {
	const n = ledgerA.nodes[id];
	if (!n) { bad(`ledger has no ${id}`); continue; }
	// Each node is its own worktree (the isolation the external tools share with
	// pi); the fake touched worker-proof.txt in its cwd, so it must be there.
	const wtProof = join(dirname(REPO), ".rlp-worktrees", runA, id, "worker-proof.txt");
	const proof = existsSync(wtProof) || existsSync(join(n.worktree ?? "", "worker-proof.txt"));
	if (!proof) bad(`${id}: no worker-proof.txt in its worktree — the fake never ran in the node's cwd`);
	const pf = join(dirA, `${id}.prompt.txt`);
	if (!existsSync(pf)) bad(`${id}: no prompt file (the brief must ride a 0600 file either way)`);
	else if ((statSync(pf).mode & 0o077) !== 0) bad(`${id}: prompt file is world-readable — 0600 or nothing`);
	if (existsSync(join(dirA, `${id}.exit`)) === false) bad(`${id}: no exit file`);
	if (tmuxHere && n.tmuxSession !== `rlp-${runA}-${id}`) bad(`${id}: ledger tmuxSession wrong (${n.tmuxSession})`);
	if (!tmuxHere && n.tmuxSession) bad(`${id}: ledger carries a window on a host without tmux`);
}
if (ledgerA.tmux !== (tmuxHere ? "auto" : "off")) bad(`run.tmux lost from the ledger: ${ledgerA.tmux}`);

// argv shape: model flag before the prompt, and the default arm says nothing about models.
const tok = (name) => readFileSync(join(DUMP, `${name}.argv`), "utf8").split("\n").filter(Boolean);
const claudeTok = tok("claude");
const mIdx = claudeTok.indexOf("--model");
const pIdx = claudeTok.findIndex((t) => t.startsWith("<PROMPT"));
if (mIdx < 0 || pIdx < 0 || mIdx >= pIdx) bad(`claude argv must carry --model before the prompt: ${claudeTok.join(" ")}`);
else if (claudeTok[mIdx + 1] !== "sonnet") bad(`claude --model value wrong: ${claudeTok[mIdx + 1]}`);
if (tok("jcode").includes("-m")) bad("jcode ran the `default` arm — a -m flag must not reach the tool at all");
const museTok = tok("muse");
if (!museTok.includes("--prompt-file")) bad(`muse must read its prompt from the file lane: ${museTok.join(" ")}`);
else {
	const pf = museTok[museTok.indexOf("--prompt-file") + 1];
	if (!existsSync(pf)) bad(`muse was handed a prompt file that does not exist: ${pf}`);
	else if (!readFileSync(pf, "utf8").startsWith("You are a RLP worker")) bad("the prompt file lost the worker contract header");
}

// rlp_watch named the lens (or its absence) without touching it.
const wText = await call("rlp_watch", {});
if (!wText.includes(`Run ${runA}`)) bad(`rlp_watch lost the run header:\n${wText.slice(0, 200)}`);

// --- 2. watchdog + the driverless harness ------------------------------------------------

// A new second, so run B cannot reuse run A's directory id.
await sleep(1_100);
writePlan(
	REQ_B,
	{
		w4: route("claude/sonnet", "claude", sleepyDriver),
		w5: route("ghosttool/x", "ghosttool", null),
	},
	3_000,
	tmuxHere ? "auto" : "off",
);
const dB = await call("rlp_dispatch", { nodes: ["w4", "w5"], request: REQ_B });
if (!dB.includes("w4: started")) bad(`sleepy node did not dispatch:\n${dB}`);
if (!dB.includes("w5: NOT dispatched") || !dB.includes("no RLP driver for harness 'ghosttool'")) {
	bad(`a driverless harness must fail its own node with the catalog's word:\n${dB}`);
}
const runB = dB.match(runIdRe)?.[1];
const wB = await call("rlp_watch", {});
if (tmuxHere && !wB.includes(`tmux -L rlp attach -t rlp-${runB}-w4`)) bad(`rlp_watch must name the live window:\n${wB.slice(0, 300)}`);
const cB = await call("rlp_collect", { waitMs: 12_000 });
if (!cB.includes("status: failed") || !cB.includes("watchdog")) bad(`watchdog did not kill and mark the sleepy worker:\n${cB.slice(0, 400)}`);
if (tmuxHere && runB) {
	const alive = spawnSync("tmux", ["-L", "rlp", "has-session", "-t", `rlp-${runB}-w4`], { encoding: "utf8" });
	if (alive.status === 0) bad("watchdog left the tmux session alive — kill-session is the tree-wide kill (D4)");
}

// --- 3. cancel -----------------------------------------------------------------------------

await sleep(1_100);
writePlan(REQ_C, { w6: route("claude/sonnet", "claude", sleepyDriver) }, 600_000, tmuxHere ? "auto" : "off");
const dC = await call("rlp_dispatch", { nodes: ["w6"], request: REQ_C });
const runC = dC.match(runIdRe)?.[1];
const xC = await call("rlp_cancel", {});
if (!xC.includes("Cancelled 1")) bad(`cancel stopped nothing:\n${xC}`);
const ledgerC = JSON.parse(readFileSync(join(RLP_HOME, "runs", runC, "ledger.json"), "utf8"));
if (ledgerC.nodes.w6?.status !== "cancelled") bad(`cancel left w6 as ${ledgerC.nodes.w6?.status}`);
if (tmuxHere && runC) {
	const alive = spawnSync("tmux", ["-L", "rlp", "has-session", "-t", `rlp-${runC}-w6`], { encoding: "utf8" });
	if (alive.status === 0) bad("cancel left the tmux session alive");
}

// --- verdict ---------------------------------------------------------------------------------

for (const note of notes) console.log(`note: ${note}`);
if (failures.length > 0) {
	// Best-effort: never leave a sleepy worker running behind us on a shared host.
	try { await call("rlp_cancel", {}); } catch { /* assertions may have died before dispatch */ }
	console.error(`check-drivers: ${failures.length} failure(s):`);
	for (const f of failures) console.error(`  - ${f}`);
	process.exit(1);
}
// The sandbox stays behind when something failed — the argv dumps, logs and
// ledgers in it are the post-mortem — and goes when the answer was simply ok.
rmSync(SANDBOX, { recursive: true, force: true });
console.log(
	`check-drivers ok: ${tmuxHere ? "lens + plain lanes" : "plain lane (no tmux on host)"} — three drivers spawned, contracts collected, watchdog and cancel killed the tree, a driverless harness failed its own node`,
);
