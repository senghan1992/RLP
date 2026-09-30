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
 *   /rlp-run is registered by rlp-orchestrate.ts, which actually runs it.
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
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
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
 * Locate the RLP checkout by walking up from the session cwd, so the commands
 * work in any project directory and survive a relocated checkout. `$RLP_ROOT`
 * wins, and the PATH-installed `rlp-svc` is the last resort.
 */
function findPython(cwd: string): string | null {
	const candidates: string[] = [];
	const push = (dir: string | null | undefined) => {
		if (dir) candidates.push(join(resolve(dir), "rlp-svc", ".venv", "bin", "python"));
	};
	push(process.env.RLP_ROOT);

	let dir = resolve(cwd);
	for (let i = 0; i < 8; i++) {
		push(dir);
		const parent = dirname(dir);
		if (parent === dir) break;
		dir = parent;
	}

	for (const candidate of candidates) {
		if (existsSync(candidate)) return candidate;
	}
	return null;
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
			"Could not find rlp-svc/.venv/bin/python from this directory.",
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
