/**
 * RLP commands for the pi/rpi harness.
 *
 * The decision engine lives behind a shell entry point (`rlp plan`, `rlp doctor`,
 * …) which means it is invisible to the person actually using the tool: the
 * slash menu advertises the harness and nothing about the orchestrator it is
 * part of. This extension closes that gap — it puts the tool's own capabilities
 * where every other tool puts them, in the `/` menu, and it needs no fork
 * rebuild: it is a dropped-in file under `<agent dir>/extensions/`.
 *
 * Registered commands:
 *   /rlp            status card + action menu (no argument = status only)
 *   /rlp-plan       <request>   headless plan: gate -> DAG -> routes -> waves
 *   /rlp-triage     <request>   the gate verdict alone, one forward pass
 *   /rlp-doctor                 is this host runnable, with a fix per failure
 *   /rlp-ladder                the resolved model ladder and the router roster
 *   /rlp-run and /rlp-state are registered by rlp-orchestrate.ts, which owns
 *   the run and the ledger.
 *   /provider and /setup are registered by rlp-provider.ts, which owns the
 *   endpoint/credential conversation.
 *
 * Three rules the commands keep:
 *   - never raise into the TUI: every failure is a rendered line, not a stack;
 *   - never mutate: all of them are read-only, so a wrong `/rlp-plan` costs
 *     time but cannot cost state. The one exception is that the status menu can
 *     *put a mutating command in the editor* (`/setup`, `/direct`) — the user
 *     still presses enter, and the write itself belongs to the extension that
 *     owns that config path.
 */
import { execFile, spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";

/** Longest engine call we will wait for; a cold laya load is ~170 s. */
const TIMEOUT_MS = 15 * 60 * 1000;

interface RunResult {
	code: number;
	stdout: string;
	stderr: string;
}

type Handler = (args: string, ctx: ExtensionCommandContext) => Promise<void>;

/**
 * Where this RLP checkout is, when the session cwd is somewhere else entirely.
 *
 * install.sh records the checkout next to the installed extensions, and a
 * project directory contains no checkout at all — the marker is how `/rlp-plan`
 * works from anywhere. `$RLP_ROOT` still wins for a relocated setup.
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

/**
 * The decision engine's interpreter: the same walk `scripts/svc-py` follows.
 *
 * A venv-less install — which is the default — creates no `.venv` at all, so
 * stopping at that probe reported "the decision engine is not installed" here
 * on a host where `rlp plan` worked from the shell. The install marker records
 * the interpreter actually used; a PATH python that can import rlp_svc is the
 * last resort. This walk is kept in step with rlp-provider.ts and
 * rlp-orchestrate.ts by hand: the loader makes every extension file
 * independent, so triplicating the helper is cheaper than inventing a shared
 * module nobody can import.
 */
function findPython(cwd: string): string | null {
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
	return pathPython() ?? null;
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
	// Negative results cache too: a host with no engine on PATH should pay the
	// 20 s probe once per session, not once per command.
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

function run(python: string, args: string[], cwd: string): Promise<RunResult> {
	return new Promise((settle) => {
		execFile(
			python,
			["-m", "rlp_svc", ...args],
			{ cwd, timeout: TIMEOUT_MS, maxBuffer: 16 * 1024 * 1024, env: process.env },
			(error, stdout, stderr) => {
				// `execFile` reports *any* non-zero exit as an error, even when the
				// process wrote a complete report to stdout — `doctor` exits 3 when a
				// check fails, and still prints the whole table. Collapsing that to 0
				// is how the status card came to say "runnable" on a host doctor had
				// just failed on, and how `/rlp-doctor` never showed a warning.
				const code =
					error && typeof (error as { code?: unknown }).code === "number"
						? ((error as { code: number }).code as number)
						: error
							? 1
							: 0;
				if (error && !stdout) {
					settle({ code, stdout: "", stderr: String(stderr || error.message) });
					return;
				}
				settle({ code, stdout: String(stdout ?? ""), stderr: String(stderr ?? "") });
			},
		);
	});
}

/** Ask for a request when the user gave none; "" means cancelled. */
async function requestArg(ctx: ExtensionCommandContext, args: string, prompt: string): Promise<string> {
	const trimmed = args.trim();
	if (trimmed) return trimmed;
	if (!ctx.hasUI) return "";
	const typed = await ctx.ui.input(prompt, "e.g. add a --wc flag, test it, document it");
	return (typed ?? "").trim();
}

function report(ctx: ExtensionCommandContext, body: string, type?: "info" | "warning" | "error"): void {
	ctx.ui.notify(body, type);
}

/** The un-run case, stated as a line rather than a stack trace. */
function missingEngine(ctx: ExtensionCommandContext): void {
	report(
		ctx,
		[
			"rlp: the decision engine is not installed.",
			"",
			"Could not find an engine interpreter: no rlp-svc/.venv/bin/python above this",
			"directory, no install marker recording one, and no PATH python that can",
			"`import rlp_svc`.",
			"Install or repair it with:",
			"",
			"    sh <RLP>/scripts/install.sh",
			"",
			"or point RLP_ROOT at the RLP checkout.",
		].join("\n"),
		"error",
	);
}

/** Long-running commands share this: show progress, then clear the footer. */
async function withWork<T>(ctx: ExtensionCommandContext, message: string, work: () => Promise<T>): Promise<T> {
	ctx.ui.setWorkingMessage(message);
	let elapsed = 0;
	const tick = setInterval(() => {
		elapsed += 10;
		ctx.ui.setWorkingMessage(`${message} (${elapsed}s — a cold laya load is ~170 s)`);
	}, 10_000);
	try {
		return await work();
	} finally {
		clearInterval(tick);
		ctx.ui.setWorkingMessage();
	}
}

// --- handlers ------------------------------------------------------------------

const plan: Handler = async (args, ctx) => {
	const request = await requestArg(ctx, args, "Plan which request?");
	if (!request) return;
	const python = findPython(ctx.cwd);
	if (!python) return missingEngine(ctx);

	const result = await withWork(ctx, "rlp: planning (gate → DAG → routing → waves)", () =>
		run(python, ["plan", request], ctx.cwd),
	);
	if (!result.stdout.trim()) {
		report(ctx, `rlp plan failed:\n${result.stderr.trim() || "no output"}`, "error");
		return;
	}
	report(ctx, result.stdout.trim());
};

const triage: Handler = async (args, ctx) => {
	const request = await requestArg(ctx, args, "Triage which request?");
	if (!request) return;
	const python = findPython(ctx.cwd);
	if (!python) return missingEngine(ctx);

	const result = await withWork(ctx, "rlp: triaging (one laya forward pass)", () =>
		run(python, ["triage", request], ctx.cwd),
	);
	const body = result.stdout.trim() || result.stderr.trim() || "rlp triage returned nothing";
	report(ctx, body, result.code ? "error" : "info");
};

const doctor: Handler = async (_args, ctx) => {
	const python = findPython(ctx.cwd);
	if (!python) return missingEngine(ctx);
	const result = await run(python, ["doctor"], ctx.cwd);
	const body = result.stdout.trim() || result.stderr.trim() || "rlp doctor returned nothing";
	report(ctx, body, result.code === 0 ? "info" : "warning");
};

const ladder: Handler = async (_args, ctx) => {
	const python = findPython(ctx.cwd);
	if (!python) return missingEngine(ctx);
	const result = await run(python, ["ladder"], ctx.cwd);
	report(ctx, result.stdout.trim() || result.stderr.trim() || "rlp ladder returned nothing");
};

const orchestrate: Handler = async (args, ctx) => {
	const request = await requestArg(ctx, args, "Orchestrate which request?");
	if (!request) return;
	const command = `rlp -p ${JSON.stringify(request)}`;
	report(
		ctx,
		[
			"◈ rlp run",
			"",
			"Orchestration needs a real session plane, so this command is composed rather than",
			"executed here. It is now in the editor — press enter to run it in this directory.",
			"",
			`    ${command}`,
			"",
			"Decompose → route → dispatch happen there. The human merges.",
		].join("\n"),
	);
	if (ctx.hasUI) ctx.ui.pasteToEditor(command);
};

/**
 * `/setup` lives in rlp-provider.ts (with /provider), and registering it in two
 * files would show up as /setup:1 and /setup:2. The menu entry therefore puts
 * the real command in the editor, where it runs as the command the user typed.
 */
const setup: Handler = async (_args, ctx) => {
	report(
		ctx,
		[
			"◈ RLP setup",
			"",
			"The guided first run: mode → connect providers → the model RLP works on →",
			"worker arms → per-role models. Every step is skippable, and it runs by",
			"itself the first time you start `rlp` on a host that cannot work yet.",
			"",
			"It is in the editor now — press enter to start it.",
		].join("\n"),
	);
	if (ctx.hasUI) ctx.ui.pasteToEditor("/setup");
};

const directMode: Handler = async (_args, ctx) => {
	report(
		ctx,
		[
			"◈ RLP mode",
			"",
			"direct-only  every request handled inline: no gate, no DAG, no workers,",
			"             and no decision model loaded. RLP as a plain coding agent.",
			"full         the laya gate decides per request and fans out when the work",
			"             earns it. This is RLP's default.",
			"",
			"The command is in the editor now — press enter for the current state,",
			"or type \`/direct on\` / \`/direct off\`.",
		].join("\n"),
	);
	if (ctx.hasUI) ctx.ui.pasteToEditor("/direct");
};

const MENU: Array<{ label: string; run: Handler; prompt?: string }> = [
	{ label: "Setup (connect providers, choose models)", run: setup },
	{ label: "Mode (direct-only, or let the gate decide)", run: directMode },
	{ label: "Plan a request (gate → DAG → waves)", run: plan, prompt: "Plan which request?" },
	{ label: "Triage a request (gate only)", run: triage, prompt: "Triage which request?" },
	{ label: "Doctor (is this host runnable?)", run: doctor },
	{ label: "Ladder (models and roster)", run: ladder },
	// "Run the whole pipeline" is not here: /rlp-run is registered by
	// rlp-orchestrate.ts, which actually dispatches workers. Registering it in
	// two files makes the loader disambiguate it as /rlp-run:1 and /rlp-run:2,
	// and neither invocation then looks like the command you typed.
];

const status: Handler = async (args, ctx) => {
	const sub = args.trim();
	if (sub) {
		const entry = MENU.find((item) => item.label.toLowerCase().startsWith(sub.split(/\s+/)[0].toLowerCase()));
		const verb = sub.split(/\s+/)[0].toLowerCase();
		if (entry && verb !== "rlp") {
			await entry.run(sub.slice(verb.length).trim(), ctx);
			return;
		}
	}

	const python = findPython(ctx.cwd);
	if (!python) return missingEngine(ctx);

	const [health, ladderJson, providerJson] = await Promise.all([
		run(python, ["doctor", "--json"], ctx.cwd),
		run(python, ["ladder", "--json"], ctx.cwd),
		run(python, ["provider", "list", "--json"], ctx.cwd),
	]);

	const lines = [`◈ RLP · ${health.code === 0 ? "runnable" : "NOT runnable — /rlp-doctor for fixes"}`];
	try {
		const config = JSON.parse(ladderJson.stdout)?.result;
		if (config) {
			lines.push(`  ladder    ${config.path}`);
			// The effective mode, from the engine rather than from a guess here: it
			// is the first thing a person reading a status card needs, and the one
			// thing they cannot infer from the brain or the arm count.
			lines.push(
				`  mode      ${config.direct_only
					? `direct-only — nothing is orchestrated (from ${config.direct_source})`
					: "full — the gate decides per request"}`,
			);
			lines.push(`  brain     ${config.brain ?? "(not chosen yet — /setup)"}`);
			lines.push(`  workers   ${(config.roster ?? []).map((c: { id: string }) => c.id).join(", ") || "none"}`);
			for (const excluded of config.excluded_workers ?? []) {
				lines.push(`  excluded  ${excluded.id} — ${excluded.reason}`);
			}
		}
	} catch {
		lines.push("  ladder    unavailable — run /rlp-doctor");
	}
	try {
		const providers = JSON.parse(providerJson.stdout)?.result;
		if (providers) {
			const withKey = providers.credentialed ?? [];
			lines.push(`  endpoints ${providers.providers.length} (${withKey.length} with a credential)`);
			for (const orphan of (providers.orphanArms ?? []).slice(0, 4)) {
				lines.push(`  UNUSABLE  ${orphan.arm} — ${orphan.why}`);
			}
		}
	} catch {
		/* an older engine without `provider`: the ladder line still stands */
	}
	lines.push(`  here      ${ctx.cwd}`);
	lines.push("");
	lines.push("  /setup                 guided first run: mode, providers, models per role");
	lines.push("  /direct on|off         work inline and never orchestrate (or the reverse)");
	lines.push("  /provider              endpoints, credentials, a live connection test");
	lines.push("  /rlp-plan <request>    decide what to do, without dispatching");
	lines.push("  /rlp-triage <request>  the gate alone, one forward pass");
	lines.push("  /rlp-doctor            why something would not run");
	lines.push("  /rlp-ladder            the model ladder in full");
	lines.push("  /rlp-config            show or edit the ladder (brain, arms, gate, mode)");
	lines.push("  /rlp-run <request>     run a request through the whole pipeline");
	lines.push("  /rlp-state             read the ledger of the run in this session");
	report(ctx, lines.join("\n"), health.code === 0 ? "info" : "warning");

	if (!ctx.hasUI) return;
	const choice = await ctx.ui.select("RLP", MENU.map((item) => item.label));
	if (!choice) return;
	const entry = MENU.find((item) => item.label === choice);
	if (!entry) return;
	if (entry.prompt && ctx.hasUI) {
		const typed = await ctx.ui.input(entry.prompt, "e.g. add a --wc flag, test it, document it");
		if (!typed?.trim()) return;
		await entry.run(typed.trim(), ctx);
		return;
	}
	await entry.run("", ctx);
};

// --- registration ----------------------------------------------------------------

export default function rlpCommands(pi: ExtensionAPI): void {
	pi.registerCommand("rlp-plan", {
		description: "Plan a request headlessly: gate -> DAG -> routing -> dispatch waves",
		handler: plan,
	});
	pi.registerCommand("rlp-triage", {
		description: "Ask the gate only: direct or orchestrate (one laya forward pass)",
		handler: triage,
	});
	pi.registerCommand("rlp-doctor", {
		description: "Check whether this host can actually run RLP (one fix per failure)",
		handler: doctor,
	});
	pi.registerCommand("rlp-ladder", {
		description: "Show the resolved model ladder and the derived router roster",
		handler: ladder,
	});
	pi.registerCommand("rlp", {
		description: "RLP: status, and the action menu (plan, triage, doctor, ladder, run)",
		handler: status,
	});
}
