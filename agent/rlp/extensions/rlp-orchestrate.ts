/**
 * Local orchestration for the RLP harness.
 *
 * This is the piece that used to come from omnigent. RLP decides *what* to do
 * with a request using the laya gate, the RLM decomposer and the laya router —
 * all of which already exist as the decision engine — and then, instead of
 * handing the plan to an external orchestration plane, executes it here: one
 * worktree per node, one headless `rpi` worker per node, waves dispatched
 * together, results collected back for synthesis.
 *
 * Why in-process at all: `rlp` is meant to be a single command. Every step
 * that needs a second program (a server, a daemon, a runner zygote) is a step
 * that can be down, stale, or on a different code version than the one the user
 * just updated. Dispatch is a `spawn` and collection is reading a file; neither
 * needs a plane.
 *
 * What this costs, stated plainly: omnigent's session database, web UI,
 * worktree lifecycle manager and guardrail policies do not come with it. The
 * guardrail that mattered most — a cap on dispatches per turn — is enforced
 * here directly from the ladder's `maxDispatchesPerTurn`, which is the same
 * number the old plane used.
 *
 * Registered tools (the brain calls these):
 *   rlp_plan      gate -> DAG -> routing -> waves, for a request
 *   rlp_dispatch  start the workers for one wave, each in its own worktree
 *   rlp_collect   block until results arrive (or `waitMs` elapses), then report
 *   rlp_state     the ledger: every node, its model, worktree, branch and status
 *   rlp_cancel    stop running workers for the run
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
	for (const dir of [process.env.RPI_CODING_AGENT_DIR, join(homedir(), ".pi", "agent")]) {
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

/** The decision engine's interpreter: the same one `rlp plan` uses. */
function findPython(cwd: string): string | undefined {
	for (const root of candidateRoots(cwd)) {
		const candidate = join(root, "rlp-svc", ".venv", "bin", "python");
		if (existsSync(candidate)) return candidate;
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
	/** Harness the ladder says this node runs on. Only `pi` is dispatchable locally. */
	harness?: string;
	/** Present|missing|unknown for the arm's provider, from the planner's preflight. */
	credential?: string;
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

/** The dispatch guardrails omnigent enforced as policies, now local. */
const ALLOWED_PURPOSES = new Set(["implement", "review", "explore", "search"]);
/** Harnesses local orchestration can actually spawn. Anything else is a plan error. */
const DISPATCHABLE_HARNESSES = new Set(["pi"]);
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

/** Map an engine op and its arguments onto the equivalent CLI invocation. */
function cliArgsFor(op: string, args: Record<string, unknown>): string[] {
	switch (op) {
		case "plan": {
			const out = ["plan", String(args.request ?? ""), "--json"];
			if (args.context) out.push("--context", String(args.context));
			if (args.mode && args.mode !== "auto") {
				out.push("--mode", String(args.mode));
				if (args.because) out.push("--because", String(args.because));
			}
			return out;
		}
		case "triage":
			return ["triage", String(args.request ?? ""), "--json"];
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
	const dir = process.env.RPI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
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
			},
			{ additionalProperties: false },
		),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const args: Record<string, unknown> = { request: params.request, context: params.context ?? "" };
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
			"Start the workers for one dispatch wave. Each node gets its own git worktree, branch and headless rpi " +
			"process on the model arm rlp_plan chose. Returns immediately with one handle per node; collect with " +
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
			const rpi = findRpi(ctx.cwd);
			if (!rpi) return fail("The rpi worker binary was not found. Run: sh scripts/install.sh");

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

				// Preflight, the local analogue of omnigent's harness-readiness check:
				// an arm with no credential, or a harness this plane cannot spawn, is a
				// plan error. Fail the node loudly rather than burn a dispatch on a
				// worker that is guaranteed to die.
				if (node.credential === "missing") {
					node.status = "failed";
					node.error = `no credential for ${node.arm} — run /login ${node.arm.split("/")[0]}, or /rlp-config to change the arm`;
					lines.push(`${id}: NOT dispatched — ${node.error}`);
					continue;
				}
				if (node.harness && !DISPATCHABLE_HARNESSES.has(node.harness)) {
					node.status = "failed";
					node.error = `local orchestration only spawns pi workers; '${node.harness}' is not reachable here — /rlp-config to disable it`;
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

				child.on("close", (code) => {
					// The worker's stdout is its report. Promote the log into the
					// result file, because that is what rlp_collect shows and what a
					// dependent node receives as context — the log also keeps the full
					// transcript for post-mortem when a worker went wrong.
					try {
						const raw = existsSync(logFile) ? readFileSync(logFile, "utf8") : "";
						const clean = stripAnsi(raw);
						writeFileSync(resultFile, clean);
						node.verdict = extractVerdict(clean);
					} catch {
						/* a missing result is reported as such by rlp_collect */
					}
					node.exitCode = code ?? 1;
					node.finishedAt = new Date().toISOString();
					node.status = code === 0 ? "done" : "failed";
					if (code !== 0) {
						node.error = `worker exited ${code}; full output in ${node.logFile}`;
					}
					save(run);
				});

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
			// forever. Kill it, mark it failed, and let the brain re-dispatch —
			// the local equivalent of omnigent's headless-subagent timer.
			const watchdog = () => {
				if (!run.workerTimeoutMs) return;
				for (const node of Object.values(run.nodes)) {
					if (node.status !== "running" || !node.startedAt) continue;
					if (Date.now() - Date.parse(node.startedAt) <= run.workerTimeoutMs) continue;
					try {
						if (node.pid) process.kill(node.pid, "SIGTERM");
					} catch {
						/* already gone */
					}
					node.status = "failed";
					node.finishedAt = new Date().toISOString();
					node.error = `worker exceeded the ${run.workerTimeoutMs} ms watchdog and was killed`;
				}
			};

			while (pending() && Date.now() < deadline) {
				if (signal?.aborted) return fail("cancelled");
				await new Promise((r) => setTimeout(r, 1000));
				watchdog();
			}
			watchdog();

			const out: string[] = [`Run ${run.runId}`];
			for (const node of Object.values(run.nodes)) {
				if (!wanted(node.id)) continue;
				if (node.status === "running") {
					const ran = node.startedAt ? Math.round((Date.now() - Date.parse(node.startedAt)) / 1000) : 0;
					out.push(`\n### ${node.id}: ${node.title}\n  status: still running (pid ${node.pid}, ${ran}s)`);
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
			const lines = [
				`Run ${run.runId}  (${run.cwd})`,
				`Request: ${run.request}`,
				`Dispatch budget this turn: ${run.dispatchBudgetUsed}/${run.maxDispatchesPerTurn}`,
				``,
				`Waves: ${run.waves.map((w, i) => `${i + 1}) ${w.join(" ")}`).join("   ")}`,
				``,
			];
			for (const node of Object.values(run.nodes)) {
				lines.push(
					`${node.id}  ${node.status.toUpperCase().padEnd(9)} ${node.title}`,
				);
				lines.push(`      arm      ${node.arm}  (${node.purpose}, ${node.modelFamily || "n/a"})`);
				lines.push(`      harness  ${node.harness ?? "pi"}  credential=${node.credential ?? "unknown"}`);
				lines.push(`      deps     ${node.dependsOn.join(", ") || "-"}`);
				if (node.worktree) lines.push(`      worktree ${node.worktree}`);
				if (node.branch) lines.push(`      branch   ${node.branch}`);
				if (node.verdict) lines.push(`      verdict  ${node.verdict}`);
				if (node.error) lines.push(`      error    ${node.error}`);
			}
			return ok(lines.join("\n"));
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
				if (node.status !== "running" || !node.pid) continue;
				try {
					process.kill(node.pid, "SIGTERM");
					node.status = "cancelled";
					node.finishedAt = new Date().toISOString();
					stopped += 1;
				} catch {
					/* already gone */
				}
			}
			save(run);
			return ok(`Cancelled ${stopped} worker(s) in run ${run.runId}.`);
		},
	});

	// --- the contract ---------------------------------------------------------------	// The system prompt already carries <rlp_orchestration> (the ladder, rendered
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
depend on those nodes.

**4. RE-DISPATCH ON FAILURE.** A node that failed, or whose result misses its
acceptance sentence, may be re-dispatched **once** to the other pi model with
the gap stated explicitly. On a second failure, say so and report the DAG
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
}
