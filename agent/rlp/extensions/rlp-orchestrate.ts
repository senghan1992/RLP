/**
 * Orchestration for the RLP harness.
 *
 * RLP decides *what* to do with a request using the laya gate, the RLM
 * decomposer and the laya router — all of which live in the decision engine —
 * and then executes the plan right here: one worktree per node, one headless
 * worker per node — the bundled `rpi`, or the external coding tool the route's
 * driver names — waves dispatched together, results collected back for
 * synthesis. Workers behind the tmux lens (`tmux -L rlp`) are watchable; how
 * they are collected is the same either way.
 *
 * Why in-process: `rlp` is meant to be a single command. Every step that needs
 * a second program (a server, a daemon, a runner zygote) is a step that can be
 * down, stale, or on a different code version than the one the user just
 * updated. Dispatch is a `spawn` and collection is reading a file; neither
 * needs a plane, so there is not one.
 *
 * What is given up by having no plane, stated plainly: there is no cross-session
 * run database and no web UI. What replaces them is a run directory
 * (`~/.rlp/runs/<id>/`) holding the ledger, every worker's log and every
 * worker's report — greppable, and still there after the session ends.
 *
 * The guardrails are enforced here rather than by a policy engine: a per-turn
 * dispatch cap from the ladder's `maxDispatchesPerTurn`, a worker watchdog from
 * `workerTimeoutMs`, an allow-list of dispatch purposes, and a preflight that
 * refuses to spend a dispatch on an arm with no credential.
 *
 * Registered tools (the brain calls these):
 *   rlp_plan      gate -> DAG -> routing -> waves, for a request
 *   rlp_dispatch  start the workers for one wave, each in its own worktree
 *   rlp_collect   block until results arrive (or `waitMs` elapses), then report
 *   rlp_state     the ledger: every node, its model, worktree, branch and status
 *   rlp_watch     the run's tmux windows and how to attach to each (read-only)
 *   rlp_cancel    stop running workers for the run
 *
 * Registered commands (the person types these; all are brain-only, like the tools):
 *   /rlp-run      put a request into the session so the contract runs it
 *   /rlp-state    read the ledger without going through the model
 *   /rlp-watch    the attach commands for this run's worker windows
 *
 * Plus a per-turn system-prompt section, because the orchestration contract is
 * what makes the model use these tools correctly — and the laya gate decides
 * whether to use them at all.
 */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, openSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import type {
	AgentToolResult,
	ExtensionAPI,
	ExtensionCommandContext,
	ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

// --- locating the pieces we drive ----------------------------------------------

/**
 * Where this RLP checkout is, when the session cwd is somewhere else entirely.
 *
 * Walking up from cwd only works inside this project, and the normal case is the
 * opposite: you run `rlp` in the repository you are working on. install.sh
 * therefore records the checkout next to the installed extensions, and that is
 * the fallback. `$RLP_ROOT` still wins for a relocated or multi-checkout setup.
 */
function installedRoot(): string | undefined {
	for (const dir of [process.env.RLP_CODING_AGENT_DIR, process.env.RPI_CODING_AGENT_DIR, join(homedir(), ".rlp", "agent")]) {
		if (!dir) continue;
		try {
			const marker = readFileSync(join(dir, "rlp-location.json"), "utf8");
			const root = (JSON.parse(marker) as { root?: string }).root;
			if (root && existsSync(root)) return resolve(root);
		} catch {
			/* no marker, or an unreadable one */
		}
	}
	return undefined;
}

/** Candidate checkouts: env, the install marker, then a walk up from cwd. */
function candidateRoots(cwd: string): string[] {
	const roots: string[] = [];
	if (process.env.RLP_ROOT) roots.push(resolve(process.env.RLP_ROOT));
	const marker = installedRoot();
	if (marker) roots.push(marker);
	let dir = resolve(cwd);
	for (let i = 0; i < 8; i++) {
		roots.push(dir);
		const parent = dirname(dir);
		if (parent === dir) break;
		dir = parent;
	}
	return roots;
}

/** The rpi binary: this same checkout's fork. */
function findRpi(cwd: string): string | undefined {
	if (process.env.RLP_RPI) {
		const explicit = resolve(process.env.RLP_RPI);
		if (existsSync(explicit)) return explicit;
	}
	for (const root of candidateRoots(cwd)) {
		const candidate = join(root, "scripts", "rpi-bin");
		if (existsSync(candidate)) return candidate;
	}
	return undefined;
}

/**
 * The decision engine's interpreter: the same walk `scripts/svc-py` follows.
 *
 * A venv-less install — which is the default — creates no `.venv` at all, so
 * stopping at that probe reported "the decision engine is not installed" on a
 * host where `rlp plan` worked from the shell. The install marker records the
 * interpreter actually used; a PATH python that can import rlp_svc is the last
 * resort. This walk is kept in step with rlp-provider.ts and rlp-commands.ts by
 * hand: the loader makes every extension file independent, so triplicating the
 * helper is cheaper than inventing a shared module nobody can import.
 */
function findPython(cwd: string): string | undefined {
	for (const root of candidateRoots(cwd)) {
		const candidate = join(root, "rlp-svc", ".venv", "bin", "python");
		if (existsSync(candidate)) return candidate;
	}
	for (const dir of [process.env.RLP_CODING_AGENT_DIR, process.env.RPI_CODING_AGENT_DIR, join(homedir(), ".rlp", "agent")]) {
		if (!dir) continue;
		try {
			const python = (JSON.parse(readFileSync(join(dir, "rlp-location.json"), "utf8")) as { python?: string }).python;
			if (python && existsSync(python)) return resolve(python);
		} catch {
			/* no marker, or no python recorded */
		}
	}
	return pathPython();
}

let probedPython: string | undefined;

/**
 * A PATH python that can actually import the engine, resolved once.
 *
 * The probe prints `sys.executable`, so the answer is the absolute interpreter
 * and not whatever `python3` means in this shell — the same rule svc-py follows.
 */
function pathPython(): string | undefined {
	if (probedPython !== undefined) return probedPython;
	probedPython = null as unknown as undefined;
	for (const candidate of ["python3", "python"]) {
		try {
			const probe = spawnSync(candidate, ["-c", "import rlp_svc, sys; print(sys.executable)"], {
				encoding: "utf8",
				timeout: 20_000,
			});
			if (probe.status === 0) {
				const resolved = (probe.stdout || "").trim().split("\n").pop();
				if (resolved && existsSync(resolved)) {
					probedPython = resolved;
					return probedPython;
				}
			}
		} catch {
			/* not on PATH, or broken: try the next name */
		}
	}
	return undefined;
}

// --- the engine client ---------------------------------------------------------------

/**
 * One resident engine process per session.
 *
 * Calling the engine as a subprocess per decision is what made this tool feel
 * broken: laya takes ~150 s to load on CPU, so every `rlp_plan` paid it, and a
 * single orchestration paid it twice because dispatch re-ran the plan. The
 * engine now loads once per session (started in the background at session
 * start) and answers over a JSON-line pipe, which is what turns the gate into
 * the ~5 s decision it is on this host instead of a two-and-a-half minute one.
 *
 * Failure is a fallback, not a crash: if the resident process cannot start or
 * dies, the CLI is used instead, which still works and is still slow.
 */
class EngineClient {
	private child: ReturnType<typeof spawn> | null = null;
	private nextId = 1;
	private pending = new Map<number, { resolve: (v: EngineReply) => void; reject: (e: Error) => void }>();
	private buffer = "";
	private warming = false;
	private ready = false;
	private starting: Promise<void> | null = null;

	private readonly python: string;
	private readonly cwd: string;
	private readonly onStatus?: (text: string | undefined) => void;

	// Assigned rather than declared as constructor parameter properties: those
	// need a full TypeScript transform, and an extension should also load under a
	// strip-only runtime such as `node --experimental-strip-types`.
	constructor(python: string, cwd: string, onStatus?: (text: string | undefined) => void) {
		this.python = python;
		this.cwd = cwd;
		this.onStatus = onStatus;
	}

	/** Start the process and wait only for it to exist, not for it to be warm. */
	async start(): Promise<void> {
		if (this.child) return;
		if (this.starting) return this.starting;
		this.starting = new Promise<void>((resolve, reject) => {
			const child = spawn(this.python, ["-m", "rlp_svc", "engine"], {
				cwd: this.cwd,
				env: process.env,
				stdio: ["pipe", "pipe", "pipe"],
			});
			this.child = child;
			child.stdout.on("data", (chunk) => this.onData(String(chunk)));
			child.stderr.on("data", () => {
				/* the engine reports failures in-band; stderr is noise */
			});
			child.on("error", (e) => {
				this.child = null;
				reject(e);
			});
			child.on("close", () => {
				// A dead engine must not leave callers hanging.
				for (const [, p] of this.pending) p.reject(new Error("engine process exited"));
				this.pending.clear();
				this.child = null;
				this.ready = false;
			});
			resolve();
		});
		return this.starting;
	}

	private onData(chunk: string): void {
		this.buffer += chunk;
		const lines = this.buffer.split("\n");
		this.buffer = lines.pop() ?? "";
		for (const line of lines) {
			if (!line.trim()) continue;
			let msg: Record<string, unknown>;
			try {
				msg = JSON.parse(line);
			} catch {
				continue;
			}
			if (msg.event === "warming") {
				this.warming = true;
				this.onStatus?.("rlp: loading the laya decision model (once per session, ~150 s on CPU)");
				continue;
			}
			if (msg.event === "ready") {
				this.warming = false;
				this.ready = true;
				this.onStatus?.(undefined);
				continue;
			}
			const id = msg.id as number | null;
			const waiter = id == null ? undefined : this.pending.get(id);
			if (!waiter) continue;
			this.pending.delete(id as number);
			waiter.resolve(msg as unknown as EngineReply);
		}
	}

	get isWarm(): boolean {
		return this.ready;
	}

	/** The interpreter this client drives, so a caller can tell when it changed. */
	get pythonPath(): string {
		return this.python;
	}

	get isLoading(): boolean {
		return this.warming;
	}

	/** A reply is the envelope `plan.py`/`triage.py` already return, or an error. */
	async call(op: string, args: Record<string, unknown>, timeoutMs = 20 * 60 * 1000): Promise<EngineReply> {
		await this.start();
		const id = this.nextId++;
		const reply = new Promise<EngineReply>((resolve, reject) => {
			this.pending.set(id, { resolve, reject });
			setTimeout(() => {
				if (this.pending.delete(id)) reject(new Error(`${op} timed out after ${Math.round(timeoutMs / 1000)}s`));
			}, timeoutMs);
		});
		const stdin = this.child?.stdin;
		if (!stdin) {
			this.pending.delete(id);
			throw new Error("engine stdin is not writable");
		}
		stdin.write(`${JSON.stringify({ id, op, args })}\n`);
		return reply;
	}

	dispose(): void {
		try {
			this.child?.kill("SIGTERM");
		} catch {
			/* already gone */
		}
		this.child = null;
	}
}

interface EngineReply {
	id: number;
	ok: boolean;
	result?: Record<string, unknown>;
	error?: string;
}

// --- run state -------------------------------------------------------------------

const RLP_HOME = process.env.RLP_HOME || join(homedir(), ".rlp");

export type NodeStatus = "pending" | "running" | "done" | "failed" | "cancelled" | "replanned";

/**
 * An external harness's command, sent along the route record as data (design
 * decision D1): the catalog in `rlp_svc/harnesses.py` assembles the argv
 * *template*, and `{prompt}` / `{prompt_file}` stay placeholders for whatever
 * dispatches. This side never learns a tool's command line by heart — adding
 * a harness is one entry in that catalog, not an edit here. pi nodes carry no
 * driver: pi's dispatch is internal and predates this.
 */
export interface DriverRecord {
	kind: string;
	harness: string;
	binary: string;
	argv: string[];
	/** "file" (the prompt rides a 0600 file) or "argv" (positional message). */
	promptVia: string;
	tmux: boolean;
	interactive: boolean;
	/** Env-var NAMES the tool may read — values are never carried on a route. */
	envNames: string[];
	vendor: string;
}

export interface NodeRecord {
	id: string;
	title: string;
	brief: string;
	acceptance: string;
	domain: string;
	agent: string;
	arm: string;
	modelFamily: string;
	purpose: string;
	/** Harness the ladder says this node runs on — "pi" (the internal lane) or
	 *  an external tool whose command rides the route as a `driver`. */
	harness?: string;
	/** Present|missing|unknown for the arm's provider, from the planner's preflight. */
	credential?: string;
	/**
	 * The external harness's argv template, riding the route record as data
	 * (D1). Absent for pi and for harnesses the catalog does not carry — the
	 * dispatcher fails those nodes with the catalog's own hint, never with a
	 * silent fallback. Optional so ledgers written before it load unchanged.
	 */
	driver?: DriverRecord;
	/** tmux lens state (D4): session on the `-L rlp` socket, the exit-code file,
	 *  and the 0600 prompt file, when the node runs behind the lens. */
	tmuxSession?: string;
	exitFile?: string;
	promptFile?: string;
	dependsOn: string[];
	worktree?: string;
	branch?: string;
	status: NodeStatus;
	pid?: number;
	startedAt?: string;
	finishedAt?: string;
	resultFile?: string;
	logFile?: string;
	/** Where the worker writes its machine-readable report.json. */
	reportFile?: string;
	exitCode?: number;
	error?: string;
	/** The worker's self-reported ACCEPTANCE/VERDICT line, when it emitted one. */
	verdict?: string;
	/** The independent best-of-N verifier's verdict, once rlp_verify has run. */
	verify?: Record<string, unknown>;
}

export interface RunLedger {
	runId: string;
	createdAt: string;
	request: string;
	cwd: string;
	waves: string[][];
	nodes: Record<string, NodeRecord>;
	dispatchBudgetUsed: number;
	maxDispatchesPerTurn: number;
	/** Watchdog: a worker older than this is killed and marked failed. */
	workerTimeoutMs?: number;
	/** Ladder `routing.tmux` at plan time: "auto" | "on" | "off" (D4). */
	tmux?: string;
	/** Planner policy: pass dependency results as file paths + digest, not inline text. */
	artifactPassing?: boolean;
	/** How deep a failed node may be recursively re-planned. */
	recursiveDepth?: number;
	/** How many recursive re-plans this run has already performed. */
	replans?: number;
	/** Best-of-N samples for rlp_verify. */
	verifySamples?: number;
}

let currentRun: RunLedger | null = null;

function runsDir(): string {
	return join(RLP_HOME, "runs");
}

function newRunId(): string {
	// Sortable and filesystem-safe; the run directory is also the human handle
	// for "where did that go?".
	const now = new Date();
	const pad = (n: number) => String(n).padStart(2, "0");
	return [
		now.getFullYear(),
		pad(now.getMonth() + 1),
		pad(now.getDate()),
		"-",
		pad(now.getHours()),
		pad(now.getMinutes()),
		pad(now.getSeconds()),
	].join("");
}

function runDir(runId: string): string {
	return join(runsDir(), runId);
}

function save(ledger: RunLedger): void {
	const dir = runDir(ledger.runId);
	mkdirSync(dir, { recursive: true });
	writeFileSync(join(dir, "ledger.json"), `${JSON.stringify(ledger, null, 2)}\n`);
}

function load(runId: string): RunLedger | null {
	const path = join(runDir(runId), "ledger.json");
	if (!existsSync(path)) return null;
	try {
		return JSON.parse(readFileSync(path, "utf8")) as RunLedger;
	} catch {
		return null;
	}
}

/** The newest run, so a restarted session can pick up what it left running. */
function latestRun(): RunLedger | null {
	try {
		const ids = readdirSync(runsDir())
			.filter((d) => existsSync(join(runsDir(), d, "ledger.json")))
			.sort();
		const last = ids[ids.length - 1];
		return last ? load(last) : null;
	} catch {
		return null;
	}
}

/**
 * The ledger rendered to lines — one renderer for the `rlp_state` tool and for
 * the `/rlp-state` command, because a second writer of the same view is a
 * second thing that can disagree with the first.
 */
function renderLedger(run: RunLedger): string {
	const lines = [
		`Run ${run.runId}  (${run.cwd})`,
		`Request: ${run.request}`,
		`Dispatch budget this turn: ${run.dispatchBudgetUsed}/${run.maxDispatchesPerTurn}`,
		``,
		`Waves: ${run.waves.map((w, i) => `${i + 1}) ${w.join(" ")}`).join("   ")}`,
		``,
	];
	for (const node of Object.values(run.nodes)) {
		lines.push(`${node.id}  ${node.status.toUpperCase().padEnd(9)} ${node.title}`);
		lines.push(`      arm      ${node.arm}  (${node.purpose}, ${node.modelFamily || "n/a"})`);
		lines.push(`      harness  ${node.harness ?? "pi"}  credential=${node.credential ?? "unknown"}`);
		if (node.driver) lines.push(`      driver   ${node.driver.binary} ${node.driver.argv.join(" ")}`);
		if (node.tmuxSession) lines.push(`      watch    tmux -L rlp attach -t ${node.tmuxSession}`);
		lines.push(`      deps     ${node.dependsOn.join(", ") || "-"}`);
		if (node.worktree) lines.push(`      worktree ${node.worktree}`);
		if (node.branch) lines.push(`      branch   ${node.branch}`);
		if (node.verdict) lines.push(`      verdict  ${node.verdict}`);
		if (node.error) lines.push(`      error    ${node.error}`);
	}
	return lines.join("\n");
}

/**
 * The watch view of a run (D4): tmux is a lens, and attaching through it is
 * something a *person* does in their own terminal — this names where to look,
 * never attaches or sends keys on anyone's behalf.
 */
function renderWatch(run: RunLedger): string {
	const mode = run.tmux ?? "auto";
	const lens = tmuxAvailable();
	const running = Object.values(run.nodes).filter((n) => n.status === "running");
	const lines = [`Run ${run.runId} — tmux lens: ${mode}${lens ? "" : " (no tmux on this host; workers run plainly)"}`];
	const windows = running.filter((n) => n.tmuxSession);
	if (windows.length) {
		lines.push("", "Live windows (attach to watch; Ctrl-b d detaches, and detaching changes nothing):");
		for (const n of windows) lines.push(`  ${n.id}  ${n.title}  →  tmux -L rlp attach -t ${n.tmuxSession}`);
	}
	const plain = running.filter((n) => !n.tmuxSession);
	if (plain.length) {
		lines.push("", `Plain spawns: ${plain.map((n) => `${n.id} (pid ${n.pid ?? "?"})`).join(", ")} — logs in ${runDir(run.runId)}`);
	}
	if (!running.length) lines.push("", "No workers running — everything has finished, failed or been cancelled.");
	return lines.join("\n");
}

/**
 * Recompute dispatch waves from the live node graph.
 *
 * Needed after `rlp_replan` injects sub-nodes: the original `waves` array is
 * stale, but the dependency edges are the truth. Nodes whose deps are absent
 * from the graph count as satisfied.
 */
function computeWaves(run: RunLedger): string[][] {
	const nodes = Object.values(run.nodes).filter((n) => n.status !== "replanned");
	const byId = new Map(nodes.map((n) => [n.id, n]));
	const done = new Set<string>();
	const waves: string[][] = [];
	let remaining = nodes.map((n) => n.id);
	let guard = 0;
	while (remaining.length > 0 && guard++ < 500) {
		const ready = remaining.filter((id) =>
			(byId.get(id)?.dependsOn ?? []).every((d) => done.has(d) || !byId.has(d)),
		);
		if (ready.length === 0) break;
		waves.push(ready);
		for (const id of ready) done.add(id);
		remaining = remaining.filter((id) => !done.has(id));
	}
	return waves;
}

// --- git worktrees ----------------------------------------------------------------

interface WorktreeResult {
	worktree: string;
	branch: string;
	/** Set when isolation was not possible and the node shares the main tree. */
	shared?: string;
}

function git(args: string[], cwd: string): { code: number; out: string } {
	const result = spawnSync("git", args, { cwd, encoding: "utf8" });
	return { code: result.status ?? 1, out: `${result.stdout ?? ""}${result.stderr ?? ""}`.trim() };
}

/**
 * Give a node its own checkout so two workers cannot fight over the same files.
 *
 * A detached worktree is preferred; when the project is not a git repository
 * (or git is missing) the node runs in the main tree and says so, because
 * silently serialising a fan-out would be worse than the race it avoids.
 */
function makeWorktree(repoRoot: string, node: NodeRecord, runId: string): WorktreeResult {
	const branch = `rlp/${runId}/${node.id}`;
	const safeRepo = (git(["rev-parse", "--show-toplevel"], repoRoot).code === 0);
	if (!safeRepo) {
		return { worktree: repoRoot, branch: "(no git — shared working tree)", shared: repoRoot };
	}
	const target = join(dirname(repoRoot), `.rlp-worktrees`, runId, node.id);
	mkdirSync(dirname(target), { recursive: true });
	if (existsSync(target)) {
		return { worktree: target, branch };
	}
	const created = git(["worktree", "add", "-b", branch, target, "HEAD"], repoRoot);
	if (created.code !== 0) {
		// The branch may exist from an earlier run; retry detached.
		const retry = git(["worktree", "add", "--detach", target, "HEAD"], repoRoot);
		if (retry.code !== 0) {
			return { worktree: repoRoot, branch: "(worktree failed — shared working tree)", shared: repoRoot };
		}
		return { worktree: target, branch: `${branch} (detached)` };
	}
	return { worktree: target, branch };
}

// --- the engine bridge ---------------------------------------------------------------

interface EngineResult {
	code: number;
	stdout: string;
	stderr: string;
}

function callEngine(
	python: string,
	args: string[],
	cwd: string,
	timeoutMs = 15 * 60 * 1000,
): Promise<EngineResult> {
	return new Promise((settle) => {
		const child = spawn(python, ["-m", "rlp_svc", ...args], { cwd, env: process.env });
		let stdout = "";
		let stderr = "";
		const timer = setTimeout(() => {
			child.kill("SIGKILL");
			settle({ code: 124, stdout, stderr: `${stderr}\ntimed out after ${Math.round(timeoutMs / 1000)}s` });
		}, timeoutMs);
		child.stdout.on("data", (d) => {
			stdout += String(d);
		});
		child.stderr.on("data", (d) => {
			stderr += String(d);
		});
		child.on("error", (e) => {
			clearTimeout(timer);
			settle({ code: 1, stdout, stderr: `${stderr}\n${e.message}` });
		});
		child.on("close", (code) => {
			clearTimeout(timer);
			settle({ code: code ?? 1, stdout, stderr });
		});
	});
}

// --- prompt-text helpers -----------------------------------------------------------------

/** Purposes a dispatch may carry. Anything else is a plan error, not a new mode. */
const ALLOWED_PURPOSES = new Set(["implement", "review", "explore", "search"]);
/** Worker watchdog default when the ladder sets none: 20 minutes. */
const DEFAULT_WORKER_TIMEOUT_MS = 20 * 60 * 1000;
/** How much of one dependency's report a dependent worker receives. */
const DEP_RESULT_LIMIT = 12_000;

function purposeOf(value: unknown): string {
	const purpose = String(value ?? "implement");
	return ALLOWED_PURPOSES.has(purpose) ? purpose : "implement";
}

/** Cut a report down to a context-safe size, saying so, so a worker never gets a wall. */
function truncate(text: string, limit = DEP_RESULT_LIMIT): string {
	if (text.length <= limit) return text;
	return `${text.slice(0, limit)}\n\n…[${text.length - limit} more characters truncated]`;
}

/** The worker's own pass/fail line, promoted into the collect report. */
function extractVerdict(body: string): string | undefined {
	const match = body.match(/^\s*(?:ACCEPTANCE|VERDICT):\s*(.+)$/im);
	return match ? match[1].trim().slice(0, 240) : undefined;
}

// --- external harness drivers (design decisions D1 and D4) ----------------------

/**
 * A prompt under this size rides argv inline; a bigger one rides a `$(cat …)`
 * expansion in the same shell command, so ARG_MAX is never the reason a node
 * failed. The 0600 prompt file is written either way — it is what `{prompt_file}`
 * harnesses (muse) read, and it is the post-mortem for the rest.
 */
const INLINE_PROMPT_MAX = 96_000;

/** Single-quoted for /bin/sh. Every token assembled into a worker command goes
 *  through this; nothing else reaches the shell unquoted. */
function shquote(text: string): string {
	return `'${text.replace(/'/g, `'\\''`)}'`;
}

/** tmux is asked once per session — the lens is a host fact, not a per-node guess.
 *  A missing tmux is not an error: `routing.tmux` auto/off spawn plainly. */
let tmuxProbed: boolean | undefined;
function tmuxAvailable(): boolean {
	if (tmuxProbed === undefined) {
		try {
			tmuxProbed = spawnSync("tmux", ["-V"], { encoding: "utf8" }).status === 0;
		} catch {
			tmuxProbed = false;
		}
	}
	return tmuxProbed;
}

/** RLP's own socket (D4): a private server that a stray `tmux ls` never shows
 *  and that a user's own tmux sessions can neither collide with nor be killed by. */
function runTmux(args: string[]): { code: number; out: string } {
	const r = spawnSync("tmux", ["-L", "rlp", ...args], { encoding: "utf8" });
	return { code: r.status ?? 1, out: `${r.stdout ?? ""}${r.stderr ?? ""}`.trim() };
}

function tmuxSessionName(runId: string, nodeId: string): string {
	return `rlp-${runId}-${nodeId}`;
}

/**
 * The worker's command line, assembled from the driver's argv *template*.
 * The catalog leaves `{prompt}` / `{prompt_file}` as placeholders precisely
 * because only dispatch knows where the prompt landed; substituting them here
 * is the whole of what this side knows about any external tool (D1).
 */
function externalWorkerCommand(driver: DriverRecord, prompt: string, promptFile: string): string {
	const parts: string[] = [shquote(driver.binary)];
	for (const token of driver.argv) {
		if (token === "{prompt_file}") parts.push(shquote(promptFile));
		else if (token === "{prompt}")
			parts.push(prompt.length <= INLINE_PROMPT_MAX ? shquote(prompt) : `"$(cat ${shquote(promptFile)})"`);
		else parts.push(shquote(token));
	}
	return parts.join(" ");
}

/** The one shell script both lanes run: the worker's output is its log, and its
 *  exit code is a file — that is how a tmux session with no parent process still
 *  reports how it ended. */
function wrapWorkerShell(workerCmd: string, logFile: string, exitFile: string): string {
	return `${workerCmd} > ${shquote(logFile)} 2>&1; echo $? > ${shquote(exitFile)}`;
}

/**
 * Finish a node, whichever lane it ran in: promote the log into the result file
 * (collect shows that, dependents read that), strip ANSI, find the ACCEPTANCE
 * line, record status. The close handler and the tmux poll share this so no
 * lens can collect the same worker slightly differently.
 */
function finalizeNode(run: RunLedger, node: NodeRecord, code: number): void {
	const live = node.status === "running";
	// The worker's stdout is its report. Promote the log into the result file,
	// because that is what rlp_collect shows and what a dependent node receives
	// as context — the log also keeps the full transcript for post-mortem.
	try {
		const raw = node.logFile && existsSync(node.logFile) ? readFileSync(node.logFile, "utf8") : "";
		const clean = stripAnsi(raw);
		if (node.resultFile) writeFileSync(node.resultFile, clean);
		node.verdict = extractVerdict(clean);
	} catch {
		/* a missing result is reported as such by rlp_collect */
	}
	node.exitCode = code;
	// A node the watchdog already killed or the user cancelled keeps *that*
	// error — the late process-exit is evidence, not a new verdict.
	if (live) {
		node.finishedAt = new Date().toISOString();
		node.status = code === 0 ? "done" : "failed";
		if (code !== 0) node.error = `worker exited ${code}; full output in ${node.logFile}`;
	}
	save(run);
}

/**
 * tmux completion = the session is gone (D4). When it is, the exit file says how
 * it ended; a missing exit file means the session died before the shell could
 * write it, which is a failure like any other.
 */
function pollTmuxNodes(run: RunLedger): void {
	for (const node of Object.values(run.nodes)) {
		if (node.status !== "running" || !node.tmuxSession) continue;
		if (runTmux(["has-session", "-t", node.tmuxSession]).code === 0) continue;
		const raw = node.exitFile && existsSync(node.exitFile) ? readFileSync(node.exitFile, "utf8").trim() : "";
		finalizeNode(run, node, /^\d+$/.test(raw) ? parseInt(raw, 10) : 1);
	}
}

/** Stop a running node's process tree: the window is the tree on the tmux lane;
 *  plain external spawns are detached group leaders, so the negative pid kills
 *  the tool and its children, never just the shell in front of it. */
function stopNode(node: NodeRecord): void {
	if (node.tmuxSession) {
		runTmux(["kill-session", "-t", node.tmuxSession]);
		return;
	}
	if (!node.pid) return;
	try {
		process.kill(node.driver ? -node.pid : node.pid, "SIGTERM");
	} catch {
		/* already gone */
	}
}

/** The brief a worker receives. Self-contained on purpose: it has no context
 *  beyond this file, the node brief, and the acceptance sentence. */
function workerPrompt(node: NodeRecord, run: RunLedger, wave: number, total: number): string {
	const artifact = run.artifactPassing !== false;
	const deps = node.dependsOn.length
		? `\n\n## Results of the nodes you depend on\n\n${
				artifact
					? "Their full reports are files on disk — read them with read/grep/bash rather than asking for them inline.\n\n"
					: ""
			}${node.dependsOn
				.map((id) => {
					const dep = run.nodes[id];
					const body = dep?.resultFile && existsSync(dep.resultFile)
						? readFileSync(dep.resultFile, "utf8")
						: "";
					const digest = truncate(body.trim(), artifact ? 900 : DEP_RESULT_LIMIT);
					const where = artifact && dep?.resultFile ? `\nFull result file: ${dep.resultFile}` : "";
					return `### ${id}: ${dep?.title ?? "?"}${where}\n\n${digest || "(not available)"}`;
				})
				.join("\n\n")}`
		: "";

	return [
		`You are a RLP worker, dispatched for exactly one node of a larger task.`,
		``,
		`## Your purpose`,
		``,
		node.purpose === "review"
			? `REVIEW — read-only. Judge the work against its acceptance contract. Do not edit anything. Report blocking issues, non-blocking issues and suggestions, with file:line evidence.`
			: node.purpose === "explore"
				? `EXPLORE / SEARCH — read-only. Answer with file:line evidence.`
				: `IMPLEMENT — make the change. Stay inside the scope below. Drive it to green: run the tests or checks for what you touched and report the exact commands.`,
		``,
		`## Working directory`,
		``,
		`Work ONLY inside this directory. It is your own worktree; other workers are using their own.`,
		node.worktree ?? run.cwd,
		``,
		`## The node`,
		``,
		`Title: ${node.title}`,
		`Domain: ${node.domain}`,
		`Branch: ${node.branch ?? "(none)"}`,
		`Wave: ${wave + 1} of ${total}`,
		``,
		`Brief:`,
		node.brief,
		``,
		`Acceptance (one observable pass/fail sentence):`,
		node.acceptance,
		deps,
		``,
		`## How to report`,
		``,
		node.purpose === "review"
			? `End with exactly one line: \`VERDICT: approved\` or \`VERDICT: changes requested — <what must change>\`, then the evidence above it.`
			: `Report what you changed (file:line) and how you verified it, with the exact commands and their results. If you could not run at all, say so in one line naming the failure.`,
		`Finish with exactly one line stating the acceptance result, so the orchestrator can read it without guessing:`,
		`\`ACCEPTANCE: pass — <one line of evidence>\` or \`ACCEPTANCE: fail — <what is missing>\`.`,
		``,
		`## Machine-readable report (write it before you finish)`,
		``,
		`Write a JSON file to exactly this path: ${node.reportFile ?? "(no path given)"}`,
		`Shape: {"status":"done"|"failed","acceptance":"pass"|"fail","acceptance_note":"one line","files":["path:line", …],"commands":["cmd → result", …],"summary":"2-3 sentences"}`,
		`This is how the orchestrator reads your result without parsing prose; the text ACCEPTANCE line above is still required.`,
		``,
		`Commit your work on the branch above with a message describing the change. Do not push, and never force-push. The human merges.`,
	]
		.filter((line) => line !== undefined)
		.join("\n");
}

/** Workers normally run headless and emit plain text, but a harness that ever
 *  colours its output would otherwise leak escape codes into a node's report
 *  and into the next node's context. */
function stripAnsi(text: string): string {
	// biome-ignore lint/suspicious/noControlCharactersInRegex: stripping escapes is the point
	return text.replace(/\[[0-9;?]*[a-zA-Z]/g, "");
}

/** Worker exited with this code, so callers can tell a failure from an abort. */

/**
 * The plan most recently produced, in memory and on disk.
 *
 * Kept because dispatch must execute the plan that was shown, and kept on disk
 * because a session can be restarted between the plan and the dispatch.
 */
let lastPlan: Record<string, unknown> | null = null;

/** How long a forced plan stays a licence to dispatch in direct-only mode. */
const FORCED_PLAN_WINDOW_MS = 15 * 60_000;

function planPath(): string {
	return join(RLP_HOME, "last-plan.json");
}

function rememberPlan(plan: Record<string, unknown>): void {
	lastPlan = plan;
	try {
		mkdirSync(RLP_HOME, { recursive: true });
		writeFileSync(planPath(), `${JSON.stringify(plan, null, 2)}\n`);
	} catch {
		/* in-memory copy still serves this session */
	}
}

function latestPlan(): Record<string, unknown> | null {
	if (lastPlan) return lastPlan;
	try {
		return JSON.parse(readFileSync(planPath(), "utf8")) as Record<string, unknown>;
	} catch {
		return null;
	}
}

/**
 * Was this plan produced under `force`, i.e. on the user's instruction rather
 * than by the gate? Dispatch asks it before spending a fan-out in direct-only
 * mode, so the escape hatch is bounded to the run it was used on.
 */
function plannedForced(plan: Record<string, unknown> | null): boolean {
	if (plan?.force !== true) return false;
	// The engine stamps a forced plan, and the stamp is what bounds the claim:
	// `last-plan.json` outlives the turn, and a fan-out authorised an hour ago in
	// a mode that says never is not an authorisation, it is a stale file.
	const at = Date.parse(String(plan.forced_at ?? ""));
	return Number.isFinite(at) && Date.now() - at < FORCED_PLAN_WINDOW_MS;
}

/** Map an engine op and its arguments onto the equivalent CLI invocation. */
function cliArgsFor(op: string, args: Record<string, unknown>): string[] {
	switch (op) {
		case "plan": {
			const out = ["plan", String(args.request ?? ""), "--json"];
			if (args.force) out.push("--force");
			if (args.context) out.push("--context", String(args.context));
			if (args.mode && args.mode !== "auto") {
				out.push("--mode", String(args.mode));
				if (args.because) out.push("--because", String(args.because));
			}
			return out;
		}
		case "triage":
			return ["triage", String(args.request ?? ""), ...(args.force ? ["--force"] : []), "--json"];
		case "decompose":
			return ["decompose", String(args.request ?? ""), "--json"];
		case "ladder":
			return ["ladder", "--json"];
		case "config":
			return ["config", JSON.stringify(args.ops ?? []), "--json"];
		case "replan":
			return ["replan", String(args.focus ?? ""), "--request", String(args.request ?? ""), "--json"];
		case "verify":
			return [
				"verify",
				"--title",
				String(args.title ?? ""),
				"--acceptance",
				String(args.acceptance ?? ""),
				"--report",
				String(args.report ?? ""),
				"--evidence",
				String(args.evidence ?? ""),
				"--avoid-family",
				String(args.avoidFamily ?? ""),
				"--samples",
				String(args.samples ?? 3),
				"--json",
			];
		case "memory":
			return ["memory", "--limit", String(args.limit ?? 40), "--json"];
		case "remember":
			return [
				"remember",
				String(args.text ?? ""),
				"--kind",
				String(args.kind ?? "note"),
				"--node",
				String(args.node ?? ""),
				"--run",
				String(args.run ?? ""),
				"--json",
			];
		default:
			return [op, "--json"];
	}
}

// --- the tools --------------------------------------------------------------------------------
/**
 * A tool result is `{content, details}` — returning a bare `{output}` type-checks
 * as any but delivers nothing to the model, so both shapes are built here.
 * An error is still a result the model must read, so it goes back as content
 * with a leading marker rather than being thrown.
 */
function ok(text: string): AgentToolResult {
	return { content: [{ type: "text", text }], details: undefined };
}

function fail(text: string): AgentToolResult {
	return { content: [{ type: "text", text: `rlp: ${text}` }], details: undefined };
}

/**
 * The operator's role -> model bindings, rendered for the brain's prompt.
 *
 * The fork renders <rlp_orchestration> but predates role bindings, and adding
 * them there would need a fork rebuild; the extension already owns the local
 * orchestration contract, so it appends this section instead. Empty (no
 * section) when the ladder carries no bindings, so a plain ladder stays plain.
 */
function roleBindingsSection(): string {
	const dir = process.env.RLP_CODING_AGENT_DIR || process.env.RPI_CODING_AGENT_DIR || join(homedir(), ".rlp", "agent");
	const path = process.env.RLP_ORCHESTRATION
		? resolve(process.env.RLP_ORCHESTRATION)
		: join(dir, "orchestration.json");
	try {
		const doc = JSON.parse(readFileSync(path, "utf8")) as { roles?: Record<string, string> };
		const entries = Object.entries(doc.roles ?? {});
		if (entries.length === 0) return "";
		return [
			"",
			"## RLP role bindings (role -> model)",
			"",
			...entries.map(([role, model]) => `- ${role}: ${model}`),
			"",
			"The planner binds each of these roles to exactly this model; do not pick a different arm for it.",
			"`rlp_plan` already applies them, so the gate table it returns is the authority.",
		].join("\n");
	} catch {
		return "";
	}
}

/**
 * Is this session direct-only — the mode in which RLP is a plain coding agent?
 *
 * Read from the file rather than asked of the engine, because the two places
 * that need the answer are the two places that must not wait on a subprocess:
 * the session-start hook (do not load a 421M model in a mode that will never
 * consult it) and the per-turn prompt (which contract the brain runs under).
 * The engine reads the same file, so the two cannot disagree.
 *
 * `$RLP_DIRECT` counts too, and outranks the ladder: `rlp --direct` is a
 * session-level instruction, and a session launched that way must not advertise
 * an orchestration surface the launcher just switched off.
 */
function directOnly(): boolean {
	if (process.env.RLP_DIRECT) return true;
	const dir = process.env.RLP_CODING_AGENT_DIR || process.env.RPI_CODING_AGENT_DIR || join(homedir(), ".rlp", "agent");
	const path = process.env.RLP_ORCHESTRATION
		? resolve(process.env.RLP_ORCHESTRATION)
		: join(dir, "orchestration.json");
	try {
		const doc = JSON.parse(readFileSync(path, "utf8")) as { routing?: { gate?: string } };
		return doc.routing?.gate === "direct";
	} catch {
		return false; // an unreadable ladder is the engine's problem to report, not a mode
	}
}

export default function rlpOrchestrate(pi: ExtensionAPI): void {
	// One binary, two identities (set by scripts/rlp and by rlp_dispatch).
	//
	// `rlp` (RLP_IDENTITY=rlp) is the brain: it gets the orchestration contract,
	// the five rlp_* tools and the resident laya engine. `rpi` — and every worker
	// rlp_dispatch spawns — is the bare harness: the same extensions load, but
	// none of the brain surface is registered. A worker must not re-triage its own
	// node through rlp_plan, and must not start a ~150 s laya load it will never
	// use. Identity is the seam that keeps the brain and the leaf workers apart.
	const IS_BRAIN = process.env.RLP_IDENTITY === "rlp";
	// One engine per session. Started in the background at session start, so the
	// ~150 s laya load happens while the user is still typing rather than on the
	// first decision.
	let engine: EngineClient | null = null;
	let engineRequested = false;

	const getEngine = (ctx: ExtensionContext): EngineClient | null => {
		const python = findPython(ctx.cwd);
		if (!python) return null;
		if (!engine || engine.pythonPath !== python) {
			engine = new EngineClient(python, ctx.cwd, (text) => {
				try {
					ctx.ui.setStatus("rlp-engine", text);
				} catch {
					/* headless */
				}
			});
		}
		return engine;
	};

	pi.on("session_start", (_event, ctx: ExtensionContext) => {
		if (!IS_BRAIN) return; // the bare harness has no engine to warm
		// Direct-only mode never asks the gate a question, so warming laya would
		// spend ~150 s of CPU and a few hundred MB of RSS on a model that is never
		// consulted. This is the line that makes `/direct on` a real mode change
		// rather than a label.
		if (directOnly()) {
			try {
				ctx.ui.setStatus("rlp-mode", "◈ direct mode");
			} catch {
				/* headless */
			}
			return;
		}
		const client = getEngine(ctx);
		if (!client) return;
		// Fire and forget: warming is the engine's job, and a failure here is
		// reported by the first tool that needs it.
		void client.start().catch(() => {});
	});

	pi.on("session_shutdown", () => {
		engine?.dispose();
		engine = null;
	});

	/** Call an engine op, preferring the resident process and falling back to the CLI. */
	const ask = async (ctx: ExtensionContext, op: string, args: Record<string, unknown>): Promise<EngineReply> => {
		const client = getEngine(ctx);
		if (client) {
			try {
				// The first call may arrive while the model is still loading; the
				// engine queues it, so this waits for the answer rather than failing.
				return await client.call(op, args);
			} catch {
				engine = null;
			}
		}
		const python = findPython(ctx.cwd);
		if (!python) {
			return { id: 0, ok: false, error: "the RLP decision engine is not installed (no rlp-svc/.venv)" };
		}
		const result = await callEngine(python, cliArgsFor(op, args), ctx.cwd);
		const text = result.stdout.trim();
		if (!text) return { id: 0, ok: false, error: result.stderr.slice(-400) || "engine produced no output" };
		try {
			const parsed = JSON.parse(text.slice(text.indexOf("{")));
			return { id: 0, ok: parsed.ok !== false, result: parsed.result ?? parsed, error: parsed.error };
		} catch {
			return { id: 0, ok: false, error: `unparseable engine output: ${text.slice(0, 200)}` };
		}
	};

	// --- rlp_plan -----------------------------------------------------------------
	if (IS_BRAIN)
		pi.registerTool({
			name: "rlp_plan",
		label: "RLP plan",
		description:
			"Decide what to do with a request: run the laya triage gate, and when it earns it, decompose into a " +
			"DAG, route every node to a worker and model arm, and compute dispatch waves. Returns mode=direct for " +
			"work that should be done inline with no workers at all, or the full plan for orchestrate. Call this " +
			"once per new user request, before doing anything else with it. A cold laya load takes ~170s.",
		promptSnippet: "rlp_plan(request) — triage, decompose, route and wave a request before dispatching",
		parameters: Type.Object(
			{
				request: Type.String({ description: "The user's request, verbatim" }),
				context: Type.Optional(
					Type.String({ description: "Repository context you already have, if any" }),
				),
				mode: Type.Optional(
					Type.Union([Type.Literal("auto"), Type.Literal("direct"), Type.Literal("orchestrate")], {
						description:
							"auto = let the gate decide. Use orchestrate ONLY when you can already name two or " +
							"more independent deliverables, and say which in `because`.",
					}),
				),
				because: Type.Optional(
					Type.String({
						description: "The two or more independent deliverables that justify overriding the gate",
					}),
				),
				force: Type.Optional(
					Type.Boolean({
						description:
							"Set true only when the user explicitly asked to orchestrate while RLP is in " +
							"direct-only mode (/direct on, or launched with --direct). It asks the engine " +
							"instead of taking the mode's answer, and it does not change the mode.",
					}),
				),
			},
			{ additionalProperties: false },
		),
		async execute(_id, params, signal, _onUpdate, ctx) {
			// Direct-only mode answers here rather than through the engine. The
			// resident engine is deliberately never warmed in that mode, so an `ask`
			// would either queue behind a 170 s model load or start one — to be told
			// the thing the mode already says. `force` is the user's own instruction,
			// and outranks a default.
			if (directOnly() && !params.force) {
				return ok(
					JSON.stringify(
						{
							mode: "direct",
							direct_only: true,
							triage: { engine: "direct-mode", confidence: 1.0, escalate: false },
							recommended:
								"RLP is in direct-only mode: do the work inline with your own tools — no DAG, " +
								"no workers, no worktrees. Only if the user explicitly asked to orchestrate, " +
								"call this again with force=true (that asks the engine; it does not change the mode).",
						},
						null,
						2,
					),
				);
			}
			const args: Record<string, unknown> = { request: params.request, context: params.context ?? "" };
			// Forwarded, not just honoured here: the resident engine and the CLI
			// fallback both need to know the mode was outranked on purpose.
			if (params.force) args.force = true;
			if (params.mode && params.mode !== "auto") {
				args.mode = params.mode;
				if (params.because) args.because = params.because;
			}
			const reply = await ask(ctx, "plan", args);
			if (signal?.aborted) return fail("cancelled");
			if (!reply.ok) return fail(reply.error ?? "plan failed");
			if (!reply.result) return fail("plan returned no result");
			// Remember the plan: dispatch must execute the plan that was shown and
			// approved, not a fresh one. Re-planning there could pick different arms
			// or a different DAG and silently diverge from the gate table.
			rememberPlan(reply.result);
			return ok(JSON.stringify(reply.result, null, 2));
		},
	});

	// --- rlp_dispatch -------------------------------------------------------------
	if (IS_BRAIN)
		pi.registerTool({
			name: "rlp_dispatch",
		label: "RLP dispatch",
		description:
			"Start the workers for one dispatch wave. Each node gets its own git worktree, branch and headless " +
			"worker — the bundled pi, or the external coding tool whose driver rlp_plan put on the route — on the " +
			"model arm rlp_plan chose. Returns immediately with one handle per node; collect with " +
			"rlp_collect. Never dispatch a node whose dependencies are not yet done, and never split one wave " +
			"across two calls — the whole point of a wave is that independent work runs at once.",
		promptSnippet: "rlp_dispatch(nodes) — start one wave of workers, each in its own worktree",
		parameters: Type.Object(
			{
				nodes: Type.Array(Type.String(), {
					description: "The node ids to dispatch, exactly as rlp_plan returned them",
				}),
				request: Type.String({
					description: "The original request, required the first time so the run can be created",
				}),
				waves: Type.Optional(
					Type.Array(Type.Array(Type.String()), {
						description: "The full wave list from rlp_plan, required the first time",
					}),
				),
			},
			{ additionalProperties: false },
		),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			// The mode is the answer to "may I fan out", so a brain that forgot to ask
			// the gate still cannot spend a fan-out in direct-only mode — unless the
			// plan this run was built from was produced under `force`, which is the
			// user's own instruction. The escape hatch opens one run, not the mode:
			// the next plan without force answers `direct` again.
			const plannedNow = lastPlan ?? latestPlan();
			if (directOnly() && !plannedForced(plannedNow)) {
				return fail(
					"RLP is in direct-only mode, so nothing is dispatched. Do the work inline, or " +
						"plan with force=true when the *user* asked for a fan-out, or have them turn " +
						"the mode off (/direct off) and plan again.",
				);
			}
			// Nullable on purpose: an external-harness plan does not need the pi
			// binary at all, and failing the whole dispatch for it was a way for
			// one lane's missing piece to stall another's.
			const rpi = findRpi(ctx.cwd);

			// Build the run from the plan rlp_plan already produced. Re-running the
			// plan here was both a doubled ~150 s cost and a correctness hole: the
			// gate table shown to the human could differ from what got dispatched.
			if (!currentRun || currentRun.request !== params.request) {
				const planned = lastPlan ?? latestPlan();
				if (!planned || planned.mode !== "orchestrate") {
					return fail(
						"No orchestrate plan for this request. Call rlp_plan first (and pass " +
							"mode=\"orchestrate\" with a `because` if the gate defaulted to direct).",
					);
				}
				const tasks = (planned.tasks ?? []) as Array<Record<string, unknown>>;
				const routes = (planned.routes ?? {}) as Record<string, Record<string, unknown>>;
				const waves = (params.waves ?? (planned.waves ?? [])) as string[][];
				const cap = (planned.max_dispatches_per_turn as number) ?? 4;
				if (tasks.length === 0) return fail("The plan has no tasks; nothing to dispatch.");
				const runId = newRunId();
				const nodes: Record<string, NodeRecord> = {};
				for (const task of tasks) {
					const id = String(task.id);
					const route = routes[id] ?? {};
					nodes[id] = {
						id,
						title: String(task.title ?? id),
						brief: String(task.brief ?? ""),
						acceptance: String(task.acceptance ?? ""),
						domain: String(task.domain ?? "code"),
						agent: String(route.agent ?? "pi"),
						arm: String(route.arm ?? ""),
						modelFamily: String(route.model_family ?? ""),
						purpose: purposeOf(route.purpose),
						harness: route.harness ? String(route.harness) : "pi",
						credential: route.credential ? String(route.credential) : "unknown",
						// The external harness's command template, taken from the
						// route as delivered. pi routes carry none: null or absent.
						driver: route.driver ? (route.driver as DriverRecord) : undefined,
						dependsOn: (task.depends_on ?? []) as string[],
						status: "pending",
					};
				}
				currentRun = {
					runId,
					createdAt: new Date().toISOString(),
					request: params.request,
					cwd: ctx.cwd,
					waves,
					nodes,
					dispatchBudgetUsed: 0,
					maxDispatchesPerTurn: cap,
					workerTimeoutMs: (planned.worker_timeout_ms as number) ?? DEFAULT_WORKER_TIMEOUT_MS,
					tmux: planned.tmux ? String(planned.tmux) : "auto",
					artifactPassing:
						(planned.planning as { artifactPassing?: boolean } | undefined)?.artifactPassing !== false,
					recursiveDepth:
						(planned.planning as { recursiveDepth?: number } | undefined)?.recursiveDepth ?? 1,
					verifySamples:
						(planned.planning as { verifySamples?: number } | undefined)?.verifySamples ?? 3,
				};
				// The run directory holds the ledgers and per-node logs, and the logs
				// are opened below before anything else has written it.
				mkdirSync(runDir(runId), { recursive: true });
				save(currentRun);
			}

			const run = currentRun;
			const cap = run.maxDispatchesPerTurn;
			// The guardrail that mattered most: a turn's dispatches are bounded
			// by the ladder, exactly as the old plane bounded them.
			if (run.dispatchBudgetUsed + params.nodes.length > cap) {
				return fail(
					`This turn would dispatch ${run.dispatchBudgetUsed + params.nodes.length} node(s) but the ` +
						`ladder caps a turn at ${cap}. Already dispatched ${run.dispatchBudgetUsed}. ` +
						`Dispatch the next wave in a later turn, after collecting.`,
				);
			}

			const lines: string[] = [];
			for (const id of params.nodes) {
				const node = run.nodes[id];
				if (!node) {
					lines.push(`${id}: not in the plan — skipped`);
					continue;
				}
				if (node.status === "running") {
					lines.push(`${id}: already running (pid ${node.pid})`);
					continue;
				}
				if (node.status === "done") {
					lines.push(`${id}: already done`);
					continue;
				}
				if (node.status === "replanned") {
					lines.push(`${id}: replanned into sub-nodes — dispatch those instead`);
					continue;
				}
				const unmet = node.dependsOn.filter((d) => run.nodes[d]?.status !== "done");
				if (unmet.length > 0) {
					lines.push(`${id}: waiting on ${unmet.join(", ")} — not dispatched`);
					continue;
				}

				// Preflight: an arm with no credential, no model at all, or a harness
				// this plane cannot spawn is a plan error. Fail the node loudly rather
				// than burn a dispatch on a worker that is guaranteed to die.
				//
				// "no model at all" is the state RLP ships in — an unconfigured ladder
				// still routes a node, so `arm` is empty — and without this the worker
				// spawned with `--model ""` and failed somewhere three steps from the
				// cause, which is exactly the silence this tool keeps getting judged on.
				if (!node.arm.includes("/")) {
					node.status = "failed";
					node.error = `${node.arm || "(no model arm)"} is not a provider/model arm — run /setup to give the ladder one`;
					lines.push(`${id}: NOT dispatched — ${node.error}`);
					continue;
				}
				if (node.credential === "missing") {
					node.status = "failed";
					node.error = `no credential for ${node.arm} — run /login ${node.arm.split("/")[0]}, or /rlp-config to change the arm`;
					lines.push(`${id}: NOT dispatched — ${node.error}`);
					continue;
				}
				// What is dispatchable is driver presence, not a name list (D1/D5):
				// pi nodes carry no driver and run the internal lane that predates
				// the catalog; external nodes carry the command itself; a harness the
				// catalog could not give a driver to fails with the catalog's own
				// word — never a silent fallback to pi.
				if (node.driver && run.tmux === "on" && !tmuxAvailable()) {
					node.status = "failed";
					node.error = `routing.tmux is "on" and this host has no tmux to put ${node.driver.harness} behind — install tmux, or set the lens to auto/off`;
					lines.push(`${id}: NOT dispatched — ${node.error}`);
					continue;
				}
				if (!node.driver && node.harness && node.harness !== "pi") {
					node.status = "failed";
					node.error = `no RLP driver for harness '${node.harness}' — \`rlp harness list\` names what this build can run; /rlp-config to move the node to pi`;
					lines.push(`${id}: NOT dispatched — ${node.error}`);
					continue;
				}

				const wt = makeWorktree(ctx.cwd, node, run.runId);
				node.worktree = wt.worktree;
				node.branch = wt.branch;

				const dir = runDir(run.runId);
				const resultFile = join(dir, `${id}.result.txt`);
				const logFile = join(dir, `${id}.log`);
				const reportFile = join(dir, `${id}.report.json`);
				node.resultFile = resultFile;
				node.logFile = logFile;
				node.reportFile = reportFile;

				const waveIndex = run.waves.findIndex((w) => w.includes(id));
				const prompt = workerPrompt(node, run, Math.max(0, waveIndex), run.waves.length);
				const out = existsSync(resultFile) ? readFileSync(resultFile, "utf8") : "";

				if (node.driver) {
					// External lane. The prompt rides a 0600 file either way — the
					// command inlines it when it fits ARG_MAX comfort, `{prompt_file}`
					// tools read it directly — and the lens follows routing.tmux.
					const full = `${prompt}\n${out}`;
					const promptFile = join(dir, `${id}.prompt.txt`);
					writeFileSync(promptFile, full, { mode: 0o600 });
					node.promptFile = promptFile;
					node.exitFile = join(dir, `${id}.exit`);
					const script = wrapWorkerShell(
						externalWorkerCommand(node.driver, full, promptFile),
						logFile,
						node.exitFile,
					);
					if (node.driver.tmux && (run.tmux === "on" || (run.tmux !== "off" && tmuxAvailable()))) {
						const sess = tmuxSessionName(run.runId, id);
						// tmux concatenates trailing args and runs them through the
						// default shell, so the script must ride as ONE quoted argument.
						const started = runTmux([
							"new-session", "-d", "-s", sess, "-c", node.worktree ?? ctx.cwd,
							`sh -c ${shquote(script)}`,
						]);
						if (started.code !== 0) {
							node.status = "failed";
							node.error = `tmux could not start ${node.driver.binary}: ${started.out.slice(-300)}`;
							lines.push(`${id}: NOT dispatched — ${node.error}`);
							continue;
						}
						node.tmuxSession = sess;
					} else {
						const child = spawn("sh", ["-c", script], {
							cwd: node.worktree ?? ctx.cwd,
							env: { ...process.env, RLP_IDENTITY: "worker" },
							// Its own process group: the watchdog and cancel must be
							// able to kill the tool, not just the shell in front of it.
							detached: true,
							stdio: ["ignore", "ignore", "ignore"],
						});
						node.pid = child.pid;
						child.on("close", (code) => finalizeNode(run, node, code ?? 1));
					}
					node.status = "running";
					node.startedAt = new Date().toISOString();
					run.dispatchBudgetUsed += 1;
					lines.push(
						`${id}: started ${node.driver.harness} on ${node.arm} in ${node.worktree} (branch ${node.branch}${
							node.tmuxSession
								? `, window ${node.tmuxSession} — attach: tmux -L rlp attach -t ${node.tmuxSession}`
								: `, pid ${node.pid}`
						})` + (wt.shared ? "  [shared working tree — not a git repo]" : ""),
					);
					continue;
				}

				// pi lane — the internal path, byte-identically what it always was.
				if (!rpi) {
					node.status = "failed";
					node.error = "The rpi worker binary was not found. Run: sh scripts/install.sh";
					lines.push(`${id}: NOT dispatched — ${node.error}`);
					continue;
				}
				const logFd = openSync(logFile, "w");
				const child = spawn(rpi, ["-p", `${prompt}\n${out}`, "--model", node.arm], {
					cwd: node.worktree,
					// RLP_IDENTITY=worker: the child is a leaf, not a brain. Without
					// this it inherits the brain's RLP_IDENTITY=rlp and would load the
					// orchestration contract and start its own laya engine.
					env: { ...process.env, RPI_DEFAULT_MODEL: node.arm, RLP_IDENTITY: "worker" },
					detached: false,
					stdio: ["ignore", logFd, logFd],
				});
				node.pid = child.pid;
				node.status = "running";
				node.startedAt = new Date().toISOString();
				run.dispatchBudgetUsed += 1;
				child.on("close", (code) => finalizeNode(run, node, code ?? 1));
				lines.push(
					`${id}: started on ${node.arm} in ${node.worktree} (branch ${node.branch}, pid ${child.pid})` +
						(wt.shared ? "  [shared working tree — not a git repo]" : ""),
				);
			}
			save(run);
			return ok(
				[`Run ${run.runId} — ${run.dispatchBudgetUsed}/${cap} dispatches used this turn.`, "", ...lines].join("\n"),
			);
		},
	});

	// --- rlp_collect ---------------------------------------------------------------
	if (IS_BRAIN)
		pi.registerTool({
			name: "rlp_collect",
		label: "RLP collect",
		description:
			"Wait for worker results and return them. Blocks up to waitMs (default 120s) and returns as soon as " +
			"anything has finished. Returns each finished node's full output plus the status of everything still " +
			"running. Call this instead of polling yourself; when nothing is ready, use the time to do work that " +
			"does not depend on those nodes.",
		promptSnippet: "rlp_collect() — block for worker results, up to waitMs",
		parameters: Type.Object(
			{
				waitMs: Type.Optional(
					Type.Number({
						description: "How long to wait for results, in ms (default 120000, max 600000)",
					}),
				),
				nodes: Type.Optional(
					Type.Array(Type.String(), {
						description: "Only wait for these node ids; omit to consider all running nodes",
					}),
				),
			},
			{ additionalProperties: false },
		),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const run = currentRun ?? latestRun();
			if (!run) return fail("No run to collect from. Dispatch a wave first.");
			currentRun = run;
			const waitMs = Math.min(600_000, Math.max(0, params.waitMs ?? 120_000));
			const deadline = Date.now() + waitMs;

			const wanted = (id: string) => !params.nodes || params.nodes.includes(id);
			const pending = () =>
				Object.values(run.nodes).some((n) => wanted(n.id) && n.status === "running");

			// Watchdog: a worker that never exits would otherwise hang collect
			// forever. Kill it, mark it failed, and let the brain re-dispatch. The
			// ceiling is the ladder's routing.workerTimeoutMs. On the tmux lane the
			// kill is `kill-session` — the whole tree, stronger than signalling the
			// pid that merely fronts it.
			const watchdog = () => {
				if (!run.workerTimeoutMs) return;
				for (const node of Object.values(run.nodes)) {
					if (node.status !== "running" || !node.startedAt) continue;
					if (Date.now() - Date.parse(node.startedAt) <= run.workerTimeoutMs) continue;
					stopNode(node);
					node.status = "failed";
					node.finishedAt = new Date().toISOString();
					node.error = `worker exceeded the ${run.workerTimeoutMs} ms watchdog and was killed`;
				}
			};

			while (pending() && Date.now() < deadline) {
				if (signal?.aborted) return fail("cancelled");
				await new Promise((r) => setTimeout(r, 1000));
				// A tmux node has no parent process to notify us: its completion is
				// the session going away, which the poll turns into a finalized node.
				pollTmuxNodes(run);
				watchdog();
			}
			pollTmuxNodes(run);
			watchdog();

			const out: string[] = [`Run ${run.runId}`];
			for (const node of Object.values(run.nodes)) {
				if (!wanted(node.id)) continue;
				if (node.status === "running") {
					const ran = node.startedAt ? Math.round((Date.now() - Date.parse(node.startedAt)) / 1000) : 0;
					const where = node.tmuxSession
						? `window ${node.tmuxSession} — attach: tmux -L rlp attach -t ${node.tmuxSession}`
						: `pid ${node.pid}`;
					out.push(`\n### ${node.id}: ${node.title}\n  status: still running (${where}, ${ran}s)`);
					continue;
				}
				if (node.status === "pending") {
					out.push(`\n### ${node.id}: ${node.title}\n  status: not dispatched yet`);
					continue;
				}
				if (node.status === "cancelled") {
					out.push(`\n### ${node.id}: ${node.title}\n  status: cancelled`);
					continue;
				}
				const body =
					node.resultFile && existsSync(node.resultFile)
						? readFileSync(node.resultFile, "utf8").trim()
						: "";
				// Prefer the worker's machine-readable report over its prose.
				let report: Record<string, unknown> | undefined;
				if (node.reportFile && existsSync(node.reportFile)) {
					try {
						report = JSON.parse(readFileSync(node.reportFile, "utf8")) as Record<string, unknown>;
					} catch {
						/* an unparseable report falls back to the ACCEPTANCE line */
					}
				}
				const verdict =
					(typeof report?.acceptance === "string" ? report.acceptance : undefined) ?? node.verdict;
				const files = Array.isArray(report?.files) ? (report.files as unknown[]) : [];
				const commands = Array.isArray(report?.commands) ? (report.commands as unknown[]) : [];
				const reported = report
					? [
							`\n  report: status=${String(report.status ?? "?")} acceptance=${String(report.acceptance ?? "?")}` +
								(report.acceptance_note ? ` — ${String(report.acceptance_note).slice(0, 200)}` : ""),
							files.length ? `\n  files: ${files.slice(0, 12).map(String).join(", ")}` : "",
							commands.length ? `\n  commands: ${commands.slice(0, 8).map(String).join(" | ")}` : "",
							report.summary ? `\n  summary: ${String(report.summary).slice(0, 400)}` : "",
						].join("")
					: "";
				out.push(
					`\n### ${node.id}: ${node.title}\n  status: ${node.status}${
						node.status === "failed" ? ` (exit ${node.exitCode})` : ""
					}${verdict ? `\n  verdict: ${verdict}` : ""}\n  acceptance: ${node.acceptance}${reported}\n\n${body || node.error || "(no output)"}`,
				);
			}
			save(run);
			return ok(out.join("\n"));
		},
	});

	// --- rlp_state ------------------------------------------------------------------
	if (IS_BRAIN)
		pi.registerTool({
			name: "rlp_state",
		label: "RLP state",
		description:
			"The run ledger: every node with its model, worktree, branch, dependencies and status, plus the wave " +
			"order and how much of this turn's dispatch budget is spent. Use this to decide what is dispatchable now.",
		promptSnippet: "rlp_state() — every node, its arm, worktree, deps and status",
		parameters: Type.Object({}, { additionalProperties: false }),
		async execute(_id, _params, _signal, _onUpdate, ctx) {
			const run = currentRun ?? latestRun();
			if (!run) return ok("No run yet. Call rlp_plan, then rlp_dispatch.");
			currentRun = run;
			return ok(renderLedger(run));
		},
	});

	// --- rlp_watch --------------------------------------------------------------------
	// The lens, not the protocol (D4): a brain that is asked "what is it doing right
	// now" answers with the attach command instead of guessing from log tails, and a
	// user gets the same words from /rlp-watch. It never attaches for anybody.
	if (IS_BRAIN)
		pi.registerTool({
			name: "rlp_watch",
			label: "RLP watch",
			description:
				"Which workers are running now and how to watch them: the tmux window and attach command for each " +
				"(on RLP's own `-L rlp` socket), pids and log paths for plain spawns. Read-only — it never " +
				"attaches, never sends keys; attach is the human's own terminal move.",
			promptSnippet: "rlp_watch() — where each running worker can be watched from",
			parameters: Type.Object({}, { additionalProperties: false }),
			async execute(_id, _params, _signal, _onUpdate, _ctx) {
				const run = currentRun ?? latestRun();
				if (!run) return fail("No run yet. Call rlp_plan, then rlp_dispatch.");
				currentRun = run;
				return ok(renderWatch(run));
			},
		});

	// --- rlp_replan -------------------------------------------------------------------
	if (IS_BRAIN)
		pi.registerTool({
			name: "rlp_replan",
			label: "RLP replan",
			description:
				"Recursively re-decompose ONE failed node into a sub-DAG and inject it into the run: the " +
				"node's dependents are re-parented onto the sub-DAG's leaves, and the sub-nodes become " +
				"dispatchable. Use it when a node failed twice, or its report says acceptance=fail, and the " +
				"node is clearly several things. Bounded by the ladder's planning.recursiveDepth.",
			promptSnippet: "rlp_replan(node, why) — split a failed node into a sub-DAG and dispatch that",
			parameters: Type.Object(
				{
					node: Type.String({ description: "The failed node id to re-decompose" }),
					why: Type.Optional(Type.String({ description: "Why it failed — passed to the re-planner as context" })),
					request: Type.Optional(Type.String({ description: "Original request, if different from the run's" })),
				},
				{ additionalProperties: false },
			),
			async execute(_id, params, _signal, _onUpdate, ctx) {
				const run = currentRun ?? latestRun();
				if (!run) return fail("No run to replan. Call rlp_plan and rlp_dispatch first.");
				currentRun = run;
				const node = run.nodes[params.node];
				if (!node) return fail(`No node ${params.node} in run ${run.runId}.`);
				if (node.status === "replanned") return fail(`${params.node} is already replanned.`);
				const allowed = run.recursiveDepth ?? 1;
				if ((run.replans ?? 0) >= allowed) {
					return fail(
						`This run has used its ${allowed} recursive re-plan(s) (planning.recursiveDepth). ` +
							"Report the DAG short of this node instead of looping, or raise recursiveDepth in /rlp-config.",
					);
				}
				const focus = [
					node.title,
					node.brief,
					`Acceptance: ${node.acceptance}`,
					params.why ? `Why it failed: ${params.why}` : "",
				]
					.filter(Boolean)
					.join("\n");
				const reply = await ask(ctx, "replan", { focus, request: params.request ?? run.request });
				if (!reply.ok || !reply.result) return fail(reply.error ?? "replan failed");
				const tasks = (reply.result.tasks ?? []) as Array<Record<string, unknown>>;
				const routes = (reply.result.routes ?? {}) as Record<string, Record<string, unknown>>;
				if (tasks.length === 0) return fail("replan returned no sub-tasks");

				const ns = (subId: string) => `${node.id}/${subId}`;
				const subIds = tasks.map((t) => String(t.id));
				for (const task of tasks) {
					const id = ns(String(task.id));
					const route = routes[String(task.id)] ?? {};
					const rawDeps = (task.depends_on ?? []) as string[];
					// Roots of the sub-DAG inherit the failed node's dependencies;
					// internal edges are namespaced so they cannot collide.
					const deps = rawDeps.length > 0 ? rawDeps.map(ns) : node.dependsOn.slice();
					run.nodes[id] = {
						id,
						title: `${node.title} · ${String(task.title ?? task.id)}`,
						brief: String(task.brief ?? ""),
						acceptance: String(task.acceptance ?? ""),
						domain: String(task.domain ?? "code"),
						agent: String(route.agent ?? node.agent),
						arm: String(route.arm ?? node.arm),
						modelFamily: String(route.model_family ?? ""),
						purpose: purposeOf(route.purpose),
						harness: route.harness ? String(route.harness) : node.harness,
						credential: route.credential ? String(route.credential) : "unknown",
						driver: route.driver ? (route.driver as DriverRecord) : node.driver,
						dependsOn: deps,
						status: "pending",
					};
				}
				// Leaves: sub-tasks nothing else in the sub-DAG depends on.
				const dependedOn = new Set<string>();
				for (const task of tasks) for (const d of (task.depends_on ?? []) as string[]) dependedOn.add(String(d));
				const leaves = subIds.filter((s) => !dependedOn.has(s)).map(ns);
				// Re-parent whoever depended on the failed node onto the sub-DAG's leaves.
				for (const other of Object.values(run.nodes)) {
					if (other.id.startsWith(`${node.id}/`)) continue;
					if (other.dependsOn.includes(node.id)) {
						other.dependsOn = other.dependsOn.flatMap((d) => (d === node.id ? leaves : [d]));
					}
				}
				node.status = "replanned";
				run.replans = (run.replans ?? 0) + 1;
				run.waves = computeWaves(run);
				save(run);
				return ok(
					[
						`Replanned ${node.id} into ${tasks.length} sub-node(s) — run ${run.runId}.`,
						"",
						`  sub-nodes: ${subIds.map(ns).join(", ")}`,
						`  dependents now wait on: ${leaves.join(", ") || "-"}`,
						`  recursive re-plans used: ${run.replans}/${allowed}`,
						"",
						"Dispatch them with rlp_dispatch (those whose deps are done are ready now).",
					].join("\n"),
				);
			},
		});

	// --- rlp_verify -------------------------------------------------------------------
	if (IS_BRAIN)
		pi.registerTool({
			name: "rlp_verify",
			label: "RLP verify",
			description:
				"Independently verify a node's acceptance with best-of-N votes from a different vendor. Reads the " +
				"node's report and result, and asks the engine — which prefers a verifier on another provider " +
				"family than the implementer — to judge pass/fail several times. Use it before trusting a " +
				"code/debug node that no review node covers. A fail means the node is not done.",
			promptSnippet: "rlp_verify(node) — independent cross-vendor best-of-N verdict on a node's acceptance",
			parameters: Type.Object(
				{
					node: Type.String({ description: "The node id to verify" }),
					samples: Type.Optional(Type.Number({ description: "Best-of-N samples (default: the ladder's verifySamples)" })),
				},
				{ additionalProperties: false },
			),
			async execute(_id, params, _signal, _onUpdate, ctx) {
				const run = currentRun ?? latestRun();
				if (!run) return fail("No run to verify. Call rlp_plan and rlp_dispatch first.");
				currentRun = run;
				const node = run.nodes[params.node];
				if (!node) return fail(`No node ${params.node} in run ${run.runId}.`);
				const acceptance = node.acceptance || node.title;
				if (!acceptance) return fail(`${params.node} has no acceptance sentence to verify against.`);
				const report = node.reportFile && existsSync(node.reportFile) ? readFileSync(node.reportFile, "utf8") : "";
				const evidence = node.resultFile && existsSync(node.resultFile)
					? truncate(readFileSync(node.resultFile, "utf8"), 12_000)
					: "";
				const avoidFamily = node.arm.includes("/") ? node.arm.slice(0, node.arm.indexOf("/")) : "";
				const samples = params.samples ?? run.verifySamples ?? 3;
				const reply = await ask(ctx, "verify", {
					title: node.title,
					acceptance,
					report,
					evidence,
					avoidFamily,
					samples,
				});
				if (!reply.ok || !reply.result) return fail(reply.error ?? "verify failed");
				node.verify = reply.result;
				save(run);
				const r = reply.result as {
					pass?: boolean;
					verifier?: string;
					cross_vendor?: boolean;
					pass_count?: number;
					samples?: number;
					agreement?: number;
					reason?: string;
				};
				return ok(
					[
						`${node.id}: ${r.pass ? "VERIFIED PASS" : "VERIFIED FAIL"}  (${r.pass_count}/${r.samples} votes, agreement ${r.agreement})`,
						`  verifier: ${r.verifier}${r.cross_vendor ? " (cross-vendor)" : " (SAME vendor — no other available)"}`,
						`  reason:   ${r.reason ?? "-"}`,
						"",
						r.pass
							? "Treat the node as done."
							: "Not done: re-dispatch it, or rlp_replan it if it is several things.",
					].join("\n"),
				);
			},
		});

	// --- rlp_memory / rlp_remember -----------------------------------------------------
	// The project's append-only knowledge log, shared across runs. rlp_plan already
	// folds its brief into the decomposer's context; these are how the brain reads
	// it directly and how it records what a run learned.
	if (IS_BRAIN)
		pi.registerTool({
			name: "rlp_memory",
			label: "RLP memory",
			description:
				"The per-project knowledge log from earlier RLP runs (pitfalls, decisions, rejected " +
				"acceptances, artifacts). rlp_plan already folds a brief of this into planning context; " +
				"call this to read it directly before deciding how to approach work here.",
			promptSnippet: "rlp_memory(limit) — what earlier runs in this project learned",
			parameters: Type.Object(
				{ limit: Type.Optional(Type.Number({ description: "How many recent entries (default 40)" })) },
				{ additionalProperties: false },
			),
			async execute(_id, params, _signal, _onUpdate, ctx) {
				const reply = await ask(ctx, "memory", { limit: params.limit ?? 40 });
				if (!reply.ok || !reply.result) return fail(reply.error ?? "memory read failed");
				const r = reply.result as { path?: string; count?: number; brief?: string };
				if (!r.count) return ok(`No memory for this project yet (${r.path}).`);
				return ok([`${r.count} entr${r.count === 1 ? "y" : "ies"} in ${r.path}`, "", r.brief ?? ""].join("\n"));
			},
		});

	if (IS_BRAIN)
		pi.registerTool({
			name: "rlp_remember",
			label: "RLP remember",
			description:
				"Append one durable fact to this project's knowledge log so the next run starts smarter: a " +
				"pitfall, a decision, a rejected acceptance, or where an artifact landed. Use it at the end " +
				"of a run for what is worth recalling, not for narration.",
			promptSnippet: "rlp_remember(text, kind, node) — record what this run learned",
			parameters: Type.Object(
				{
					text: Type.String({ description: "One or two sentences worth recalling next run" }),
					kind: Type.Optional(
						Type.Union(
							[
								Type.Literal("note"),
								Type.Literal("decision"),
								Type.Literal("pitfall"),
								Type.Literal("artifact"),
								Type.Literal("blocked"),
							],
							{ description: "Defaults to note" },
						),
					),
					node: Type.Optional(Type.String({ description: "The node id this came from, if any" })),
					run: Type.Optional(Type.String({ description: "The run id, if any" })),
				},
				{ additionalProperties: false },
			),
			async execute(_id, params, _signal, _onUpdate, ctx) {
				const reply = await ask(ctx, "remember", {
					text: params.text,
					kind: params.kind ?? "note",
					node: params.node ?? "",
					run: params.run ?? "",
				});
				if (!reply.ok) return fail(reply.error ?? "remember failed");
				return ok(`remembered [${params.kind ?? "note"}] ${params.text.slice(0, 140)}`);
			},
		});

	// --- rlp_cancel -------------------------------------------------------------------
	if (IS_BRAIN)
		pi.registerTool({
			name: "rlp_cancel",
		label: "RLP cancel",
		description: "Stop the running workers of a run. Their worktrees and branches are left in place.",
		promptSnippet: "rlp_cancel() — stop this run's running workers",
		parameters: Type.Object({}, { additionalProperties: false }),
		async execute(_id, _params, _signal, _onUpdate, _ctx) {
			const run = currentRun ?? latestRun();
			if (!run) return ok("Nothing to cancel.");
			let stopped = 0;
			for (const node of Object.values(run.nodes)) {
				if (node.status !== "running") continue;
				// tmux lanes have no live pid in this process — kill-session takes the
				// whole tree; plain external spawns are killed as process groups.
				stopNode(node);
				node.status = "cancelled";
				node.finishedAt = new Date().toISOString();
				stopped += 1;
			}
			save(run);
			return ok(`Cancelled ${stopped} worker(s) in run ${run.runId}.`);
		},
	});

	/**
	 * Direct-only mode's whole contract: one agent, working inline.
	 *
	 * Short on purpose. The orchestration contract is long because driving a DAG,
	 * waves, collection and verification is the part a model gets wrong unaided;
	 * in this mode there is nothing to get wrong, and leaving the fan-out rules in
	 * the prompt would keep advertising tools the mode has just switched off.
	 */
	const DIRECT_CONTRACT = `
## RLP — direct mode

This session runs in RLP's direct-only mode: one agent, working inline. There is
no triage gate, no decomposition, no worker fan-out, and no decision model to
load. That is a choice, not a broken install, so do not go looking for the
orchestration surface or report it as missing.

Work the request yourself with your normal tools: read, change, run the relevant
test or check, and report what you did and how you verified it. Take a big task
in order, in one line of work at a time, rather than trying to delegate it.

The one exception is the user's own instruction. If *they* ask for a fan-out, or
for parallel workers, you may call \`rlp_plan\` with \`force: true\`: that asks the
gate for this one request and licenses this one run to dispatch — it does not
change the mode, and the stamp expires. Do not reach for it on your own
judgement; the mode exists so that judgement is not made per request.

To change the mode itself, say so and let the user do it: \`/direct off\`, or
\`rlp mode full\` outside a session.

Keep the guardrails: commit on a branch, never merge, never force-push, never
touch a protected branch.
`.trim();

	// --- the contract ---------------------------------------------------------------
	// The system prompt already carries <rlp_orchestration> (the ladder, rendered
	// by the fork). What it does not carry is the rule for *when* to orchestrate
	// and how to drive these tools, which is the part a model will get wrong
	// without being told.
	const CONTRACT = `
## RLP orchestration

You are running on the RLP harness, which decides whether a request deserves
orchestration at all. Ceremony is a bug: never fan out a one-line fix.

**1. TRIAGE FIRST.** Every new user request starts with \`rlp_plan\`.

- \`mode: "direct"\` — do the work yourself with your own tools. No DAG, no
  workers, no worktrees, no gate table. Make the change, run the relevant test
  or check, and report what you did and how you verified it. One line:
  "triage: direct (<confidence>) — handling inline".
- \`mode: "orchestrate"\` — run the pipeline below.
- \`escalate: true\` — the gate was unsure. Default to direct. Escalate only
  when you can already name two or more independent deliverables, and pass them
  as \`because\`. Do not escalate on a hunch.
- \`orchestration_unavailable: "<reason>"\` — this host cannot dispatch at all
  (usually a ladder with no model arms yet). Do the work inline and, **once**,
  tell the user the reason verbatim and that \`/setup\` fixes it — or \`/direct on\`
  if they would rather never orchestrate here. Do not call
  \`rlp_dispatch\`, and do not retry \`rlp_plan\` with \`mode: "orchestrate"\`:
  an override cannot conjure an arm, and the answer will not change.

If work turns out bigger mid-flight — a second independent deliverable appears —
stop, say "upgrading to orchestrate", and run the pipeline. The converse is also
true: if a plan turns out to be one thing, cancel it and just do the work.

**2. DISPATCH IN WAVES.** \`rlp_dispatch\` starts a whole wave at once; that is
the point, so never split one wave across calls and never serialise independent
nodes. A node is ready only when every id in its \`depends_on\` is done —
\`rlp_state\` tells you which are ready. The ladder's max dispatches per turn is
enforced for you; the budget is per turn, so a later wave is a later call.

Each worker gets its own git worktree and branch, and its own headless session.
You never edit a worker's files; you route, verify and assemble.

**3. COLLECT, DON'T POLL.** \`rlp_collect\` blocks until results arrive and
hands you each node's full output. Do not sleep, do not poll the filesystem.
If nothing is ready within the wait, spend that turn on work that does not
depend on those nodes. When a user asks what a worker is doing *right now*,
answer with \`rlp_watch\`: it names each running node's tmux window and attach
command. Do not attach or send keys yourself — the lens is for watching, and
attaching is the human's own move.

**4. RE-DISPATCH ON FAILURE.** A node that failed, or whose result misses its
acceptance sentence, may be re-dispatched **once** to the other arm — another
model, or another tool the ladder carries — with the gap stated explicitly. On a second failure, say so and report the DAG
short of that node rather than looping. A worker that reports
\`ACCEPTANCE: fail\`, or whose \`verdict\` line contradicts its acceptance,
has not met its contract even if it exited 0 — treat it as failed. A worker
that never exits is killed by the ladder's watchdog and comes back as failed;
do not wait on it forever. If \`rlp_plan\` reports a node under \`preflight\`,
its arm has no credential: fix it with \`/login <provider>\` or
\`/rlp-config\`, do not dispatch it.

A worker's machine-readable report (its \`report.json\`) and its \`verdict\` are
the contract, not its prose. When a node fails a second time, or its report says
\`acceptance: fail\`, and the node is clearly several things, do not keep
re-dispatching it: call \`rlp_replan(node, why)\` to split that node into a
sub-DAG. Replanning re-parents its dependents onto the sub-DAG's leaves and makes
the sub-nodes dispatchable; it is bounded by the ladder's
\`planning.recursiveDepth\`. Once that budget is spent, report the DAG short of
the node rather than looping.

**Independent verification.** A worker's own \`ACCEPTANCE: pass\` is not proof.
Before you report a code or debug node as done, if no review node covers it,
call \`rlp_verify(node)\`: it asks a *different vendor* for a best-of-N verdict
on the acceptance. A fail, or weak agreement, means the node is not done —
re-dispatch it, or \`rlp_replan\` it if it is several things. Review and
integration nodes are already independent; do not double-verify those.

**Remember what the run learned.** At the end of a run, call \`rlp_remember\` for
anything durable: a pitfall the next run would hit, a decision and why, a node
whose acceptance was rejected and what it really needed, where a produced
artifact lives. One or two sentences, never narration. \`rlp_plan\` folds this
project's memory into the next plan's context automatically, so what you write
here makes the following run cheaper and more accurate; \`rlp_memory\` reads it
back.

**5. SYNTHESIZE, DO NOT MERGE.** When the DAG is done, write the final answer
yourself from the collected results: what was done, where (branches, files),
what was verified and how, what is left. **The human merges. Never merge, never
push, never force-push.**

**6. THE LADDER IS EDITABLE.** Which models orchestrate is configuration, and
the user can change it live: \`/rlp-config\` (interactive, or
\`/rlp-config brain|add-arm|set-arm|worker|gate ...\`) and the ladder actions
in \`/models --pick\`. If a request needs a model the ladder does not carry, edit it
rather than inventing an arm; \`rlp_plan\` only ever names arms the ladder holds.

Cross-vendor rule: a review node must run on a different provider family than
the implementation it reviews. \`rlp_plan\` already applies this when it picks
arms; do not override it back to the same family.

Before the first dispatch, post the gate table (id | title | agent | model |
deps | acceptance) and the wave order, then dispatch **in the same turn**. The
gate is a report, not a question — unless the request was genuinely ambiguous
about scope, in which case ask first.

Act in the same turn you announce. A turn that ends after only announcing
intent is a bug. You may end a turn only when workers are in flight and you are
waiting on \`rlp_collect\`, or when the deliverable is done.
`.trim();

	pi.on("before_agent_start", (_event, ctx: ExtensionContext) => {
		// The bare harness gets plain pi: no orchestration contract, no role
		// bindings. Only the brain is told how to drive rlp_plan/rlp_dispatch.
		if (!IS_BRAIN) return;
		// Which contract depends on the mode, and the mode is read every turn
		// because `/direct` can change it mid-session and the next prompt must not
		// still be telling the model it may fan out. Handing a direct-only session
		// the fan-out contract is how a mode becomes a label: the tools answer, so
		// the model obeys the instructions instead.
		if (directOnly()) {
			return { systemPrompt: `${ctx.getSystemPrompt()}\n\n${DIRECT_CONTRACT}` };
		}
		return {
			systemPrompt: `${ctx.getSystemPrompt()}\n\n${CONTRACT}${roleBindingsSection()}`,
		};
	});

	// --- /rlp-run now means something concrete -----------------------------------------
	// Previously it composed a shell command. With local orchestration there is no
	// external plane to launch, so it runs the request through the same pipeline a
	// user would drive by hand — which is also the honest way to show it working.
	if (IS_BRAIN)
		pi.registerCommand("rlp-run", {
		description: "Run a request through RLP locally: gate, then waves of workers in this session",
		handler: async (args, ctx: ExtensionCommandContext) => {
			const request = args.trim();
			if (!request) {
				if (!ctx.hasUI) return;
				const typed = await ctx.ui.input("Orchestrate which request?", "e.g. add a --wc flag, test it, document it");
				if (!typed?.trim()) return;
				// Prefill the editor so the request goes through the normal turn
				// loop, where the contract and the tools are live.
				ctx.ui.pasteToEditor(typed.trim());
				return;
			}
			ctx.ui.pasteToEditor(request);
		},
	});

	// --- /rlp-state: the ledger a person can read --------------------------------------
	// The brain tool `rlp_state` renders this for the model; onboarding copy and
	// `rlp skills` have been pointing at `/rlp-state` for a run the user never
	// started yet. A read-only view costs nothing to exist, and it belongs in this
	// file because the ledger loader and the renderer are already here — moving it
	// to rlp-commands.ts would mean shipping a second copy of both. Same IS_BRAIN
	// gate as the tool, so the bare harness stays surface-free.
	if (IS_BRAIN)
		pi.registerCommand("rlp-state", {
			description: "Show the run ledger: every node, its arm, worktree, branch and status (read-only)",
			handler: async (_args, ctx: ExtensionCommandContext) => {
				const run = currentRun ?? latestRun();
				ctx.ui.notify(
					run ? renderLedger(run) : "No run yet. /rlp-run <request> plans one, and rlp_dispatch fills the ledger.",
				);
			},
		});

	// --- /rlp-watch: the human's own look inside the lens ------------------------------
	// Same renderer the tool uses, so the model and the person can never be looking
	// at two descriptions of one run. Attaching is still the person's move in their
	// own terminal — this hands over the command and nothing else.
	if (IS_BRAIN)
		pi.registerCommand("rlp-watch", {
			description: "List this run's tmux windows and their attach commands (read-only; RLP never attaches for you)",
			handler: async (_args, ctx: ExtensionCommandContext) => {
				const run = currentRun ?? latestRun();
				ctx.ui.notify(run ? renderWatch(run) : "No run yet. /rlp-run <request> plans one, and rlp_dispatch fills the lens.");
			},
		});
}
