/**
 * Provider setup and first-run onboarding for the RLP harness — the friendly
 * front end.
 *
 * The engine owns these files. `rlp provider …` validates every argument,
 * backs the file up, writes atomically, and keeps auth.json at 0600; this file
 * never touches models.json or auth.json itself and never puts a credential on
 * a command line (it goes down a pipe, because argv is visible in `ps`). That
 * split is the point: the tool's rules about credentials are testable in the
 * offline suite, and the terminal UI is a conversation on top of them.
 *
 * Commands:
 *   /provider                      endpoints, credentials, ladder reachability
 *   /provider connect [preset]     guided: preset → endpoint → key → models
 *   /provider add <id> <url> <model…>   scriptable attach (`--key-stdin`)
 *   /provider test [id]            one real round trip, failure classified
 *   /provider models <id>          ask the endpoint what it serves
 *   /provider key <id> [--drop]    set, replace, or remove a credential
 *   /provider remove <id> [--key]  detach an endpoint
 *   /setup                         guided first run: doctor → providers →
 *                                  brain → worker arms → roles → doctor
 *
 * What makes this friendlier than a JSON file, concretely:
 *   - presets, so a base URL is chosen rather than recalled;
 *   - `GET /models`, so a model id is picked from a list rather than typed;
 *   - a live round trip with a *classified* failure (bad key vs bad URL vs bad
 *     model id vs no network) and a fix line per class;
 *   - orphan arms named: a ladder arm whose provider has no credential is why
 *     "the model is in the list but nothing runs", and nothing else says so.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync, copyFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";

/** RLP's own agent dir — see the note in menus.ts; the rule is identical. */
const AGENT_DIR = process.env.RLP_CODING_AGENT_DIR || process.env.RPI_CODING_AGENT_DIR || join(homedir(), ".rlp", "agent");
const SETTINGS_FILE = join(AGENT_DIR, "settings.json");
/** Live checks talk to the network; a hung endpoint must not freeze the TUI. */
const PROBE_TIMEOUT_MS = 45_000;

// --- talking to the engine ----------------------------------------------------------

type Json = Record<string, unknown>;

/**
 * The interpreter that owns the provider store.
 *
 * Same walk-up as the other RLP extensions: `$RLP_ROOT`, then the checkout that
 * install.sh recorded next to the installed extensions, then up from the
 * session cwd — so `/provider` works in the repository you are working on, not
 * only inside the RLP checkout.
 */
function findPython(cwd: string): string | undefined {
	const roots: string[] = [];
	if (process.env.RLP_ROOT) roots.push(resolve(process.env.RLP_ROOT));
	for (const dir of [AGENT_DIR]) {
		if (!dir) continue;
		try {
			const root = (JSON.parse(readFileSync(join(dir, "rlp-location.json"), "utf8")) as { root?: string }).root;
			if (root) roots.push(resolve(root));
		} catch {
			/* no marker: fall through to the walk-up */
		}
	}
	let dir = resolve(cwd);
	for (let i = 0; i < 8; i++) {
		roots.push(dir);
		const parent = dirname(dir);
		if (parent === dir) break;
		dir = parent;
	}
	for (const root of roots) {
		const candidate = join(root, "rlp-svc", ".venv", "bin", "python");
		if (existsSync(candidate)) return candidate;
	}
	return undefined;
}

interface EngineRun {
	code: number;
	stdout: string;
	stderr: string;
}

/**
 * One engine call. `stdin` exists so a secret can travel over a pipe: a key on
 * argv is readable by every process on the host.
 */
function engine(python: string, args: string[], cwd: string, stdin?: string, timeoutMs = 120_000): Promise<EngineRun> {
	return new Promise((settle) => {
		const child = spawn(python, ["-m", "rlp_svc", ...args], { cwd, env: process.env });
		let stdout = "";
		let stderr = "";
		const timer = setTimeout(() => {
			child.kill("SIGKILL");
			settle({ code: 124, stdout, stderr: `${stderr}\n${args[0]} timed out after ${Math.round(timeoutMs / 1000)}s` });
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
		child.stdin.on("error", () => {
			/* a closed pipe is not an error worth reporting */
		});
		if (stdin !== undefined) child.stdin.end(stdin);
		else child.stdin.end();
	});
}

/** A parsed engine reply: whatever `--json` printed, plus the process status. */
interface EngineReply {
	ok: boolean;
	code: number;
	/** The envelope itself (or a flat payload for probe/discover/doctor). */
	payload: Json;
	/** `payload.result` when present — the shape most commands use. */
	data: Json;
	error?: string;
}

async function engineJson(
	ctx: ExtensionCommandContext,
	args: string[],
	stdin?: string,
	timeoutMs = 120_000,
): Promise<EngineReply | undefined> {
	const python = findPython(ctx.cwd);
	if (!python) return undefined;
	const run = await engine(python, args, ctx.cwd, stdin, timeoutMs);
	const text = run.stdout.trim();
	if (!text) {
		return {
			ok: false,
			code: run.code,
			payload: {},
			data: {},
			error: run.stderr.trim().slice(-300) || "the engine produced no output",
		};
	}
	let payload: Json;
	try {
		payload = JSON.parse(text.slice(text.indexOf("{"))) as Json;
	} catch {
		return { ok: false, code: run.code, payload: {}, data: {}, error: `unparseable engine output: ${text.slice(0, 200)}` };
	}
	const data = payload.result && typeof payload.result === "object" ? (payload.result as Json) : payload;
	return {
		ok: payload.ok !== false && run.code === 0,
		code: run.code,
		payload,
		data,
		error: typeof payload.error === "string" ? payload.error : undefined,
	};
}

/** One line saying the engine is not installed, with the fix. */
function missingEngine(ctx: ExtensionCommandContext): void {
	ctx.ui.notify(
		[
			"◈ the RLP decision engine is not installed, so provider config cannot be read or written.",
			"",
			"  Could not find rlp-svc/.venv/bin/python from this directory.",
			"",
			"  sh <RLP>/scripts/install.sh",
			"",
			"  or point RLP_ROOT at the RLP checkout.",
		].join("\n"),
		"error",
	);
}

/** Run something slow with a footer that admits it is slow. */
async function withWork<T>(ctx: ExtensionCommandContext, message: string, work: () => Promise<T>): Promise<T> {
	ctx.ui.setWorkingMessage(message);
	try {
		return await work();
	} finally {
		ctx.ui.setWorkingMessage();
	}
}

// --- reading the provider store ------------------------------------------------------

interface ProviderCard {
	id: string;
	name: string;
	baseUrl: string;
	models: string[];
	modelCount: number;
	credential: "oauth" | "key" | "none";
	credentialLabel: string;
	ladderArms: Array<{ model: string; worker: string; roles: string[]; available: boolean }>;
	runnable: boolean;
}

interface ProviderSummary {
	modelsPath: string;
	authPath: string;
	providers: ProviderCard[];
	credentialed: string[];
	withoutCredential: string[];
	orphanArms: Array<{ arm: string; worker: string; why: string }>;
	presets: Array<{ id: string; label: string; baseUrl: string; models: string[]; needsKey: boolean; note: string }>;
}

async function providerSummary(ctx: ExtensionCommandContext): Promise<ProviderSummary | undefined> {
	const reply = await engineJson(ctx, ["provider", "list", "--json"], undefined, PROBE_TIMEOUT_MS);
	if (!reply) return undefined;
	if (!reply.ok) {
		ctx.ui.notify(`◈ could not read the provider store: ${reply.error ?? "unknown error"}`, "warning");
		return undefined;
	}
	return reply.data as unknown as ProviderSummary;
}

function renderProviders(summary: ProviderSummary): string {
	const lines = [`◈ endpoints · ${summary.modelsPath}`, ""];
	if (summary.providers.length === 0) {
		lines.push("  no endpoints configured.");
		lines.push("");
		lines.push("  /provider connect     guided: pick a provider, paste a key, choose models");
		lines.push("  /setup                the whole first run in one go");
		lines.push("  /provider add <id> <baseUrl> <model>   attach one directly");
		return lines.join("\n");
	}
	for (const card of summary.providers) {
		const badge = { oauth: "◆", key: "●", none: "○" }[card.credential];
		lines.push(`${badge} ${card.id}  —  ${card.baseUrl}`);
		lines.push(`   ${card.modelCount} model(s) · ${card.credentialLabel}`);
		if (card.ladderArms.length > 0) {
			lines.push(`   RLP arms: ${card.ladderArms.map((a) => a.model).join(", ")}`);
		} else {
			lines.push("   RLP arms: none — this endpoint is usable by the harness but not by the orchestrator");
		}
		const preview = card.models.slice(0, 4).join(", ");
		if (preview) lines.push(`   ${preview}${card.modelCount > 4 ? `, +${card.modelCount - 4} more` : ""}`);
		lines.push("");
	}
	if (summary.orphanArms.length > 0) {
		lines.push("  ladder arms that cannot run:");
		for (const orphan of summary.orphanArms) lines.push(`    ${orphan.arm} — ${orphan.why}`);
		lines.push("");
	}
	lines.push("  ● api key   ◆ oauth   ○ no credential");
	lines.push("  /provider connect        attach an endpoint (guided)");
	lines.push("  /provider test <id>      one real round trip");
	lines.push("  /provider key <id>       set or replace a credential");
	lines.push("  /provider remove <id>    detach one");
	return lines.join("\n").trimEnd();
}

/** The lines a successful provision reports, so the user sees what was written. */
function reportApplied(kind: string, id: string, extra: string[]): string {
	return [`◈ ${kind} ${id}`, "", ...extra].join("\n");
}

// --- the model catalogue the harness resolves -----------------------------------------

interface ModelRow {
	provider: string;
	id: string;
	authenticated: boolean;
	current: boolean;
	isDefault: boolean;
}

/** Models the running harness knows, with this session's marks. */
function modelRows(ctx: ExtensionCommandContext): ModelRow[] {
	const current = ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : undefined;
	let savedDefault: string | undefined;
	try {
		const settings = JSON.parse(readFileSync(SETTINGS_FILE, "utf8")) as Json;
		if (typeof settings.defaultProvider === "string" && typeof settings.defaultModel === "string") {
			savedDefault = `${settings.defaultProvider}/${settings.defaultModel}`;
		}
	} catch {
		/* no settings yet */
	}
	const rows: ModelRow[] = [];
	for (const model of ctx.modelRegistry.getAll()) {
		const ref = `${model.provider}/${model.id}`;
		rows.push({
			provider: model.provider,
			id: model.id,
			authenticated: ctx.modelRegistry.hasConfiguredAuth(model),
			current: ref === current,
			isDefault: ref === savedDefault,
		});
	}
	return rows;
}

/** "1 3 5-7 all name" → the picked options, in the order typed. */
function parseSelection(raw: string, options: string[]): string[] {
	const picked: string[] = [];
	const add = (value?: string) => {
		if (value && !picked.includes(value)) picked.push(value);
	};
	for (const token of raw.split(/[,\s]+/).filter(Boolean)) {
		if (token.toLowerCase() === "all") return [...options];
		const range = token.match(/^(\d+)-(\d+)$/);
		if (range) {
			const a = Number(range[1]);
			const b = Number(range[2]);
			for (let i = Math.min(a, b); i <= Math.max(a, b); i++) add(options[i - 1]);
			continue;
		}
		if (/^\d+$/.test(token)) {
			add(options[Number(token) - 1]);
			continue;
		}
		const index = options.findIndex((o) => o === token || o.startsWith(token));
		if (index >= 0) add(options[index]);
	}
	return picked;
}

/**
 * A numbered list plus one prompt, because there is no multi-select dialog on
 * the command context. The list is printed first so the reply can be numbers.
 */
async function multiSelect(ctx: ExtensionCommandContext, title: string, options: string[]): Promise<string[]> {
	if (options.length === 0) return [];
	const cap = 100;
	ctx.ui.notify(
		[
			title,
			"",
			...options.slice(0, cap).map((o, i) => `  ${String(i + 1).padStart(3)}. ${o}`),
			options.length > cap ? `  …and ${options.length - cap} more` : "",
			"",
			'  reply with numbers or ranges (e.g. "1 3 5-7"), names, or "all"',
		]
			.filter(Boolean)
			.join("\n"),
	);
	const raw = await ctx.ui.input("Select (empty = none)", "1 3 5-7");
	if (!raw?.trim()) return [];
	return parseSelection(raw.trim(), options);
}

// --- the ladder, via the engine -------------------------------------------------------

interface LadderView {
	path: string;
	brain: string;
	roles: Record<string, string | string[]>;
	known_roles: string[];
	model_pool: string[];
	role_resolution: Record<string, { model: string; worker: string | null; binding: boolean; chain?: string[]; index?: number } | null>;
	workers: Array<{ id: string; harness?: string; available?: boolean; models: Array<{ model: string; roles: string[] }> }>;
}

async function ladderView(ctx: ExtensionCommandContext): Promise<LadderView | undefined> {
	const reply = await engineJson(ctx, ["ladder", "--json"]);
	if (!reply?.ok) {
		ctx.ui.notify(`◈ no usable orchestration ladder: ${reply?.error ?? "the engine is not installed"}`, "warning");
		return undefined;
	}
	return reply.data as unknown as LadderView;
}

/** Apply ladder ops through the engine's validated `config` command. */
async function applyOps(ctx: ExtensionCommandContext, ops: unknown[], kind: string, id: string): Promise<boolean> {
	const reply = await engineJson(ctx, ["config", JSON.stringify(ops), "--json"], undefined, 60_000);
	if (!reply?.ok) {
		ctx.ui.notify(`◈ ladder edit rejected: ${reply?.error ?? "unknown error"}`, "warning");
		return false;
	}
	const ladder = (reply.data.ladder ?? {}) as Json;
	const workers = (ladder.workers as Json[] | undefined) ?? [];
	ctx.ui.notify(
		reportApplied(kind, id, [
			`  ladder  ${String(reply.data.path ?? "")}`,
			reply.data.backup ? `  backup  ${String(reply.data.backup)}` : "",
			`  brain   ${String(ladder.brain ?? "?")}`,
			...workers.map((w) => {
				const arms = (w.models as Json[] | undefined) ?? [];
				return `  [${String(w.id)}]${w.available === false ? " [UNAVAILABLE]" : ""} ${arms.map((m) => String(m.model)).join(", ")}`;
			}),
		].filter(Boolean)),
	);
	return true;
}

// --- connecting a provider ------------------------------------------------------------

interface ConnectResult {
	provider: string;
	models: string[];
}

/**
 * The guided attach: preset → id → endpoint → key → live model discovery →
 * write. Every step can be cancelled, and cancelling writes nothing.
 *
 * Discovery is the step that matters. Typing a model id from memory is where
 * this used to go wrong, and the endpoint already knows the answer: one
 * unauthenticated `GET /models` turns the question into a list.
 */
async function connectWizard(ctx: ExtensionCommandContext, presetId?: string): Promise<ConnectResult | undefined> {
	if (!ctx.hasUI) return undefined;
	const summary = await providerSummary(ctx);
	if (summary === undefined) {
		missingEngine(ctx);
		return undefined;
	}
	const presets = summary.presets ?? [];

	// 1. preset
	let preset = presets.find((p) => p.id === presetId);
	if (!preset) {
		const label = await ctx.ui.select(
			"Connect a provider — which one?",
			presets.map((p) => `${p.label}${p.note ? `  ·  ${p.note}` : ""}`),
		);
		if (!label) return undefined;
		preset = presets.find((p) => `${p.label}${p.note ? `  ·  ${p.note}` : ""}` === label);
		if (!preset) return undefined;
	}

	// 2. id
	const typedId = await ctx.ui.input(
		`Provider id (used as the "provider" in provider/model)${preset.id === "custom" ? "" : ` — default: ${preset.id}`}`,
		preset.id === "custom" ? "my-provider" : preset.id,
	);
	if (typedId === undefined) return undefined;
	const id = (typedId.trim() || (preset.id === "custom" ? "" : preset.id)).trim();
	if (!id) {
		ctx.ui.notify("◈ a provider id is required — letters, digits, and . _ - only", "warning");
		return undefined;
	}
	if (!/^[a-zA-Z0-9._-]+$/.test(id)) {
		ctx.ui.notify(`◈ "${id}" is not a usable provider id (letters, digits, . _ -).`, "warning");
		return undefined;
	}
	const existing = summary.providers.find((p) => p.id === id);
	if (existing) {
		const ok = await ctx.ui.confirm(
			`Provider "${id}" already exists`,
			`${existing.baseUrl}\n${existing.modelCount} model(s), ${existing.credentialLabel}\n\nUpdate it? The old entry is backed up.`,
		);
		if (!ok) return undefined;
	}

	// 3. endpoint
	const typedUrl = await ctx.ui.input(
		`Base URL${preset.baseUrl ? ` (blank = ${preset.baseUrl})` : " — the API root, e.g. https://api.example.com/v1"}`,
		preset.baseUrl || "https://api.example.com/v1",
	);
	if (typedUrl === undefined) return undefined;
	const baseUrl = (typedUrl.trim() || preset.baseUrl).trim();
	if (!/^https?:\/\//.test(baseUrl)) {
		ctx.ui.notify(`◈ "${baseUrl}" is not an http(s) URL.`, "warning");
		return undefined;
	}

	// 4. key (optional, and never on argv)
	let apiKey: string | undefined;
	if (preset.needsKey) {
		const typedKey = await ctx.ui.input(
			`API key for ${id} (blank to add later with /provider key ${id})`,
			"sk-…",
		);
		if (typedKey?.trim()) apiKey = typedKey.trim();
	}

	// 5. what does the endpoint actually serve?
	let models: string[] = [];
	const discovered = await withWork(ctx, `rlp: asking ${baseUrl} which models it serves`, () =>
		engineJson(
			ctx,
			["provider", "discover", "--base-url", baseUrl, ...(apiKey ? ["--key-stdin"] : []), "--json"],
			apiKey ?? "",
			PROBE_TIMEOUT_MS,
		),
	);
	let discoveredFromUrl = false;
	if (discovered?.ok && Array.isArray(discovered.data.models)) {
		models = discovered.data.models as string[];
		discoveredFromUrl = true;
	} else if (discovered) {
		// Some endpoints list models for anyone, some need the key first: say
		// what happened, and let the user pick the fallback rather than guess.
		const why = String(discovered.payload.error ?? discovered.error ?? "unknown error");
		const fix = String(discovered.payload.fix ?? "");
		const choice = await ctx.ui.select(
			`Could not list models at ${baseUrl} (${String(discovered.payload.kind ?? "error")}: ${why.slice(0, 90)})`,
			[
				"Type model ids by hand",
				`Use the known ${preset.models.length} model(s) for ${preset.label}`,
				"Retry the request",
				"Cancel",
			],
		);
		if (!choice || choice === "Cancel") return undefined;
		if (choice.startsWith("Retry")) return connectWizard(ctx, preset.id);
		if (choice.startsWith("Use the known")) models = [...preset.models];
		else {
			const typed = await ctx.ui.input("Model ids (comma-separated)", "large, small");
			models = (typed ?? "").split(",").map((m) => m.trim()).filter(Boolean);
		}
		if (fix) ctx.ui.notify(`◈ ${fix}`, "info");
	}
	if (models.length === 0) {
		const typed = await ctx.ui.input("Model ids to attach (comma-separated)", preset.models.join(", "));
		models = (typed ?? "").split(",").map((m) => m.trim()).filter(Boolean);
		if (models.length === 0) {
			ctx.ui.notify("◈ no models given, so nothing was written.", "warning");
			return undefined;
		}
	}

	// 6. which of them? (keep the list short: an endpoint with 400 models is noise)
	let chosen = models;
	if (discoveredFromUrl && models.length > 1) {
		const capped = models.slice(0, 100);
		const picked = await multiSelect(
			ctx,
			`${baseUrl} serves ${models.length} model(s) — which should ${id} carry?`,
			capped,
		);
		if (picked.length > 0) chosen = picked;
		else {
			const ok = await ctx.ui.confirm("Attach none of them?", "The endpoint would be written with no models.");
			if (!ok) return undefined;
			chosen = [];
		}
	}

	// 7. write, through the engine (validated, backed up, atomic, 0600)
	const args = ["provider", "add", id, baseUrl, ...chosen, "--json"];
	if (apiKey) args.push("--key-stdin");
	const applied = await engineJson(ctx, args, apiKey ?? "", 60_000);
	if (!applied?.ok) {
		ctx.ui.notify(`◈ could not attach ${id}: ${applied?.error ?? "unknown error"}`, "error");
		return undefined;
	}
	const stored = (applied.data.models as string[] | undefined) ?? chosen;
	ctx.ui.notify(
		reportApplied(existing ? "updated" : "connected", id, [
			`  endpoint  ${baseUrl}`,
			`  models    ${stored.length > 0 ? stored.join(", ") : "(none yet)"}`,
			`  auth      ${apiKey ? "key written to auth.json (0600)" : `none yet — /provider key ${id}`}`,
			`  backup    ${String(applied.data.backup ?? "none (new file)")}`,
			"",
			"  /reload picks it up without restarting.",
		]),
	);
	return { provider: id, models: stored };
}

// --- putting an endpoint to work ------------------------------------------------------

/**
 * After an endpoint is attached, offer the things that make it matter: make it
 * the session model, the default, a worker arm, the brain, or a role binding.
 * Deliberately one question with a clear list — the ladder edits themselves
 * stay scriptable through /rlp-config.
 */
async function offerUsage(
	pi: ExtensionAPI,
	ctx: ExtensionCommandContext,
	connected: ConnectResult,
): Promise<void> {
	if (!ctx.hasUI || connected.models.length === 0) return;
	const ref = `${connected.provider}/${connected.models[0]}`;
	const action = await ctx.ui.select(
		`${connected.provider} is connected — put a model to work?`,
		[
			`Use ${ref} for this session`,
			`Set ${ref} as the default for new sessions`,
			`Add ${connected.models.length} model(s) as RLP worker arms`,
			`Set ${ref} as the RLP orchestrator (brain)`,
			`Bind one of them to an RLP role`,
			"Nothing else for now",
		],
	);
	if (!action || action.startsWith("Nothing")) return;

	// `Use` and `Set … default` are session-local; the rest edit the ladder.
	if (action.startsWith("Use ") || /^Set .* as the default/.test(action)) {
		const model = ctx.modelRegistry.find(connected.provider, connected.models[0]);
		if (!model) {
			ctx.ui.notify(
				`◈ ${ref} is not in this session's catalogue yet — /reload, then try again.`,
				"warning",
			);
			return;
		}
		if (action.startsWith("Use ")) {
			const ok = await pi.setModel(model as never);
			ctx.ui.notify(ok ? `◈ session model is now ${ref}` : `◈ could not switch to ${ref}`, ok ? "info" : "warning");
			return;
		}
		const settings = (() => {
			try {
				return JSON.parse(readFileSync(SETTINGS_FILE, "utf8")) as Json;
			} catch {
				return {} as Json;
			}
		})();
		mkdirSync(dirname(SETTINGS_FILE), { recursive: true });
		let backup = "";
		if (existsSync(SETTINGS_FILE)) {
			backup = `${SETTINGS_FILE}.bak.${Date.now()}`;
			copyFileSync(SETTINGS_FILE, backup);
		}
		settings.defaultProvider = connected.provider;
		settings.defaultModel = connected.models[0];
		writeFileSync(SETTINGS_FILE, `${JSON.stringify(settings, null, 2)}\n`);
		ctx.ui.notify(
			reportApplied("default set to", ref, [
				backup ? `  backup  ${backup}` : "  backup  none (new file)",
				"  Applies to new sessions. Orchestrated workers keep the ladder arms.",
			]),
		);
		return;
	}

	const view = await ladderView(ctx);
	if (!view) return;

	if (action.startsWith("Add ")) {
		const workers = view.workers.filter((w) => (w.harness ?? "pi") === "pi");
		const pool = workers.length > 0 ? workers : view.workers;
		const worker = pool.length === 1 || !ctx.hasUI ? pool[0]?.id : await ctx.ui.select("Which worker?", pool.map((w) => w.id));
		if (!worker) return;
		const roles = await ctx.ui.input(
			"Roles these models carry (comma-separated)",
			"code,review,docs",
		);
		const list = (roles ?? "code").split(",").map((r) => r.trim()).filter(Boolean);
		const ops = connected.models
			.filter((m) => !view.model_pool.includes(`${connected.provider}/${m}`))
			.map((m) => ({
				op: "add_arm",
				worker,
				model: `${connected.provider}/${m}`,
				roles: list,
				when: `added from /provider on ${new Date().toISOString().slice(0, 10)}`,
			}));
		if (ops.length === 0) {
			ctx.ui.notify(`◈ those models are already arms on the ladder.`, "info");
			return;
		}
		await applyOps(ctx, ops, `${ops.length} ladder arm(s) added to ${worker}:`, connected.provider);
		return;
	}

	if (/^Set .* as the RLP orchestrator/.test(action)) {
		await applyOps(ctx, [{ op: "set_brain", model: ref }], "brain is now", ref);
		return;
	}

	// role binding
	const role = await ctx.ui.select("Bind to which RLP role?", view.known_roles);
	if (!role) return;
	const which = connected.models.length === 1
		? connected.models
		: await multiSelect(ctx, `${connected.provider} — pick the models for the "${role}" role, in priority order`, connected.models);
	const refs = (which.length > 0 ? which : [connected.models[0]]).map((m) => `${connected.provider}/${m}`);
	const ops: unknown[] = [];
	const missing = refs.filter((r) => !view.model_pool.includes(r));
	if (missing.length > 0) {
		const piWorkers = view.workers.filter((w) => (w.harness ?? "pi") === "pi");
		const pool = piWorkers.length > 0 ? piWorkers : view.workers;
		if (pool.length === 0) {
			ctx.ui.notify("◈ the ladder has no worker to add the arm to.", "warning");
			return;
		}
		const worker = pool.length === 1 ? pool[0].id : await ctx.ui.select("Add the new arm(s) to which worker?", pool.map((w) => w.id));
		if (!worker) return;
		for (const r of missing) {
			ops.push({ op: "add_arm", worker, model: r, roles: [role], when: `bound to the ${role} role from /provider` });
		}
	}
	ops.push(refs.length > 1 ? { op: "set_role", role, models: refs } : { op: "set_role", role, model: refs[0] });
	await applyOps(ctx, ops, `role ${role} bound to`, refs.join(" > "));
}

// --- /provider ------------------------------------------------------------------------

async function providerHome(ctx: ExtensionCommandContext): Promise<void> {
	const summary = await providerSummary(ctx);
	if (!summary) return missingEngine(ctx);
	ctx.ui.notify(renderProviders(summary));
	if (!ctx.hasUI) return;

	const actions = [
		"＋ Connect a provider (guided)…",
		...summary.providers.map((p) => `Test ${p.id}`),
		"Test an endpoint before writing it…",
		...summary.providers.filter((p) => p.credential === "none").map((p) => `Set a key for ${p.id}`),
		...summary.providers.map((p) => `Add more models to ${p.id}`),
		...summary.providers.map((p) => `Remove ${p.id}`),
		"Done",
	];
	const choice = await ctx.ui.select("Provider actions", actions);
	if (!choice || choice === "Done") return;

	if (choice.startsWith("＋")) {
		const connected = await connectWizard(ctx);
		if (connected) return; // offerUsage is the wizard's own follow-up
		return;
	}
	if (choice.startsWith("Test an endpoint")) {
		const baseUrl = await ctx.ui.input("Base URL to test", "https://api.example.com/v1");
		if (!baseUrl?.trim()) return;
		const key = await ctx.ui.input("API key (blank if the endpoint needs none)", "sk-…");
		const model = await ctx.ui.input("Model id to call", "gpt-5.1");
		await probeAndReport(ctx, { baseUrl: baseUrl.trim(), key: key?.trim(), model: model?.trim() });
		return;
	}

	const testTarget = choice.match(/^Test (.+)$/);
	if (testTarget) return void (await probeAndReport(ctx, { provider: testTarget[1] }));

	const keyTarget = choice.match(/^Set a key for (.+)$/);
	if (keyTarget) return void (await setKeyInteractive(ctx, keyTarget[1]));

	const addTarget = choice.match(/^Add more models to (.+)$/);
	if (addTarget) return void (await addModelsInteractive(ctx, addTarget[1]));

	const removeTarget = choice.match(/^Remove (.+)$/);
	if (removeTarget) return void (await removeInteractive(ctx, removeTarget[1]));
}

/** One real round trip, reported as a verdict rather than a stack. */
async function probeAndReport(
	ctx: ExtensionCommandContext,
	target: { provider?: string; baseUrl?: string; key?: string; model?: string },
): Promise<boolean> {
	const args = ["provider", "probe", "--json"];
	if (target.provider) args.push(target.provider);
	if (target.baseUrl) args.push("--base-url", target.baseUrl);
	if (target.model) args.push("--model", target.model);
	// The credential travels on stdin, never on argv.
	if (target.key) args.push("--key-stdin");
	const shown = target.provider ?? target.baseUrl ?? "endpoint";
	const reply = await withWork(ctx, `rlp: calling ${shown}`, () =>
		engineJson(ctx, args, target.key ?? "", PROBE_TIMEOUT_MS),
	);
	if (!reply) {
		missingEngine(ctx);
		return false;
	}
	if (reply.ok) {
		ctx.ui.notify(
			[
				`◈ ${shown} answered in ${String(reply.data.latency ?? "?")}`,
				`  model  ${String(reply.data.model ?? "?")}`,
				`  url    ${String(reply.data.baseUrl ?? "?")}`,
			].join("\n"),
		);
		return true;
	}
	const available = (reply.payload.availableModels as string[] | undefined) ?? [];
	ctx.ui.notify(
		[
			`◈ ${shown} did not answer`,
			"",
			`  what   ${String(reply.payload.kind ?? "error")}: ${String(reply.payload.error ?? reply.error ?? "").slice(0, 200)}`,
			`  fix    ${String(reply.payload.fix ?? "")}`,
			available.length > 0 ? `\n  it does serve: ${available.slice(0, 12).join(", ")}` : "",
		]
			.filter(Boolean)
			.join("\n"),
		"warning",
	);
	return false;
}

/** Set or replace a credential, without ever echoing it. */
async function setKeyInteractive(ctx: ExtensionCommandContext, provider: string): Promise<void> {
	if (!ctx.hasUI) return;
	const key = await ctx.ui.input(`API key for ${provider} (stored in auth.json, 0600)`, "sk-…");
	if (!key?.trim()) return;
	const reply = await engineJson(ctx, ["provider", "key", provider, "--key-stdin", "--json"], key.trim(), 30_000);
	if (!reply?.ok) {
		ctx.ui.notify(`◈ could not store the key: ${reply?.error ?? "unknown error"}`, "error");
		return;
	}
	const verified = await probeAndReport(ctx, { provider });
	ctx.ui.notify(
		verified
			? `◈ credential stored for ${provider}, and the endpoint answered.`
			: `◈ credential stored for ${provider} (auth.json, 0600). The test above says whether it works.`,
		verified ? "info" : "warning",
	);
}

/** Discover and merge more models into an endpoint that already exists. */
async function addModelsInteractive(ctx: ExtensionCommandContext, provider: string): Promise<void> {
	const summary = await providerSummary(ctx);
	const card = summary?.providers.find((p) => p.id === provider);
	if (!card) return void ctx.ui.notify(`◈ no endpoint "${provider}".`, "warning");
	const discovered = await withWork(ctx, `rlp: asking ${card.baseUrl} which models it serves`, () =>
		engineJson(ctx, ["provider", "discover", provider, "--json"], undefined, PROBE_TIMEOUT_MS),
	);
	let candidates: string[] = [];
	if (discovered?.ok) candidates = ((discovered.data.models as string[]) ?? []).filter((m) => !card.models.includes(m));
	if (candidates.length === 0) {
		const typed = await ctx.ui.input(
			discovered?.ok ? "No new models were reported. Model ids to add (comma-separated)" : `Could not list models (${discovered?.error ?? "error"}). Model ids to add`,
			"model-id",
		);
		candidates = (typed ?? "").split(",").map((m) => m.trim()).filter(Boolean);
		if (candidates.length === 0) return;
	} else {
		const picked = await multiSelect(ctx, `${card.baseUrl} — ${candidates.length} model(s) not attached yet`, candidates);
		if (picked.length === 0) return;
		candidates = picked;
	}
	const reply = await engineJson(ctx, ["provider", "add", provider, card.baseUrl, ...candidates, "--json"], undefined, 60_000);
	if (!reply?.ok) {
		ctx.ui.notify(`◈ could not update ${provider}: ${reply?.error ?? "unknown error"}`, "error");
		return;
	}
	ctx.ui.notify(
		reportApplied("models added to", provider, [
			`  now  ${(((reply.data.models as string[]) ?? []) || []).join(", ")}`,
			`  backup  ${String(reply.data.backup ?? "none")}`,
		]),
	);
}

async function removeInteractive(ctx: ExtensionCommandContext, provider: string): Promise<void> {
	const ok = await ctx.ui.confirm(`Remove provider "${provider}"?`, "Its credential is kept unless you say otherwise.");
	if (!ok) return;
	const drop = await ctx.ui.confirm("Also remove its credential?", "auth.json entry for this provider — yes to delete it.");
	const reply = await engineJson(ctx, ["provider", "remove", provider, ...(drop ? ["--drop-key"] : []), "--json"], undefined, 30_000);
	if (!reply?.ok) {
		ctx.ui.notify(`◈ could not remove ${provider}: ${reply?.error ?? "unknown error"}`, "error");
		return;
	}
	ctx.ui.notify(
		reportApplied("removed", provider, [
			reply.data.keyRemoved ? "  credential also removed" : "  credential kept in auth.json",
			`  backup  ${String(reply.data.backup ?? "none")}`,
			"  /reload to apply.",
		]),
	);
}

// --- /setup: the guided first run ------------------------------------------------------

/** Doctor, as a summary a person reads rather than a table of 30 rows. */
async function doctorSummary(ctx: ExtensionCommandContext): Promise<{ ok: boolean; problems: string[]; text: string }> {
	const reply = await engineJson(ctx, ["doctor", "--json"], undefined, 120_000);
	if (!reply) return { ok: false, problems: ["the decision engine is not installed"], text: "" };
	const checks = (reply.payload.checks as Array<{ status: string; name: string; detail: string; hint: string }>) ?? [];
	const problems = checks
		.filter((c) => c.status !== "ok")
		.map((c) => `${c.status === "fail" ? "FAIL" : "warn"}  ${c.name}: ${c.detail}${c.hint ? `\n        → ${c.hint}` : ""}`);
	const summary = (reply.payload.summary as { pass: number; warn: number; fail: number }) ?? { pass: 0, warn: 0, fail: 0 };
	return {
		ok: reply.payload.ok === true,
		problems,
		text: `${summary.pass} passed, ${summary.warn} warning(s), ${summary.fail} failure(s)`,
	};
}

/**
 * The whole first run, guided: is this host runnable → which endpoints have no
 * credential → which model orchestrates → which models work → which roles they
 * cover (with the cross-vendor rule stated) → re-check.
 *
 * Every step is skippable, and each one reports what it changed. The point is
 * that the *decisions* are asked for in order, with the defaults stated, rather
 * than left as four files the user has to know about.
 */
async function setupWizard(pi: ExtensionAPI, ctx: ExtensionCommandContext): Promise<void> {
	const health = await withWork(ctx, "rlp: checking this host", () => doctorSummary(ctx));
	const banner = [
		"◈ RLP setup",
		"",
		`  doctor  ${health.ok ? "runnable" : "problems found"} (${health.text})`,
		...(health.problems.length > 0 ? ["", ...health.problems.map((p) => `  ${p}`)] : []),
	];
	ctx.ui.notify(banner.join("\n"), health.ok ? "info" : "warning");

	// 1. endpoints and credentials
	let summary = await providerSummary(ctx);
	if (!summary) return missingEngine(ctx);
	if (summary.orphanArms.length > 0) {
		ctx.ui.notify(
			[
				"◈ these ladder arms cannot run as configured:",
				"",
				...summary.orphanArms.map((o) => `  ${o.arm} — ${o.why}`),
			].join("\n"),
			"warning",
		);
	}
	for (;;) {
		const keyless = summary.withoutCredential;
		const options = [
			...(keyless.length > 0 ? keyless.map((id) => `Set the key for ${id}`) : []),
			"＋ Connect a provider (guided)…",
			"Skip — endpoints are fine",
		];
		const choice = await ctx.ui.select("Step 1 of 4 — endpoints", options);
		if (!choice || choice.startsWith("Skip")) break;
		if (choice.startsWith("＋")) {
			const connected = await connectWizard(ctx);
			summary = (await providerSummary(ctx)) ?? summary;
			if (connected) await offerUsage(pi, ctx, connected);
			continue;
		}
		const target = choice.replace(/^Set the key for /, "");
		await setKeyInteractive(ctx, target);
		summary = (await providerSummary(ctx)) ?? summary;
	}

	// 2. the orchestrator model
	const authenticated = modelRows(ctx).filter((r) => r.authenticated);
	if (authenticated.length === 0) {
		ctx.ui.notify(
			[
				"◈ no provider has a credential, so there is no model to orchestrate with.",
				"",
				"  Run /setup again after connecting one, or /provider connect now.",
			].join("\n"),
			"warning",
		);
		return;
	}
	const view = await ladderView(ctx);
	if (!view) return;
	const brainRef = await chooseModel(ctx, `Step 2 of 4 — the orchestrator (brain). Currently: ${view.brain}`, authenticated);
	if (brainRef) {
		await applyOps(ctx, [{ op: "set_brain", model: brainRef }], "brain is now", brainRef);
		const model = ctx.modelRegistry.find(brainRef.split("/")[0], brainRef.slice(brainRef.indexOf("/") + 1));
		if (model && (await pi.setModel(model as never))) {
			ctx.ui.notify(`◈ this session now runs on ${brainRef}`, "info");
		}
	}

	// 3. the worker arms
	const picked = await multiSelect(
		ctx,
		[
			"Step 3 of 4 — worker arms. Which models may RLP dispatch to?",
			"",
			"  Arms are priority-ordered: the first is where the bulk of the spend goes.",
			"  Two or more provider FAMILIES are what make independent review possible:",
			"  a review node is re-picked onto a different family than the code it reviews.",
		].join("\n"),
		authenticated.map((r) => `${r.provider}/${r.id}${r.current ? "   (this session)" : ""}`),
	);
	const armRefs = picked.map((p) => p.replace(/\s+\(this session\)$/, "").trim());
	if (armRefs.length > 0) {
		const piWorker = view.workers.find((w) => (w.harness ?? "pi") === "pi") ?? view.workers[0];
		if (!piWorker) {
			ctx.ui.notify("◈ the ladder has no worker to add arms to.", "warning");
		} else {
			const roles = await ctx.ui.input("Roles for these arms (comma-separated)", "code,review,docs");
			const list = (roles ?? "code").split(",").map((r) => r.trim()).filter(Boolean);
			const ops = armRefs
				.filter((r) => !view.model_pool.includes(r))
				.map((r) => ({
					op: "add_arm",
					worker: piWorker.id,
					model: r,
					roles: list,
					// Appended, not positioned: `model_pool` is the union across
					// workers, so its length is not this worker's arm count.
					when: `arm chosen in /setup on ${new Date().toISOString().slice(0, 10)}`,
				}));
			if (ops.length > 0) await applyOps(ctx, ops, `${ops.length} worker arm(s) added to ${piWorker.id}:`, armRefs.join(", "));
		}
		const families = new Set(armRefs.map((r) => r.split("/")[0]));
		if (families.size < 2) {
			ctx.ui.notify(
				[
					`◈ every arm is from one family (${[...families].join(", ")}).`,
					"",
					"  Cross-vendor review cannot be satisfied, so review nodes will be reported as",
					"  violations. One more provider family — even a small one — fixes it:",
					"  /provider connect (OpenRouter is the cheapest way to get several at once).",
				].join("\n"),
				"warning",
			);
		}
	}

	// 4. role bindings
	const after = await ladderView(ctx);
	if (after) {
		const bind = await ctx.ui.select(
			"Step 4 of 4 — role bindings. Bind a role to a specific model?",
			["Skip — use arm priority order", "Set the planner-side roles (plan, critique, verify)", "Back to /rlp-roles later"],
		);
		if (bind?.startsWith("Set the planner")) {
			for (const role of ["plan", "critique", "verify"]) {
				const ref = await chooseModel(ctx, `Which model should the "${role}" role use?`, authenticated);
				if (!ref) continue;
				const ops: unknown[] = [];
				if (!(after.model_pool ?? []).includes(ref)) {
					const worker = after.workers.find((w) => (w.harness ?? "pi") === "pi") ?? after.workers[0];
					if (!worker) continue;
					ops.push({ op: "add_arm", worker: worker.id, model: ref, roles: [role], when: `bound to the ${role} role from /setup` });
				}
				ops.push({ op: "set_role", role, model: ref });
				await applyOps(ctx, ops, `role ${role} bound to`, ref);
			}
		}
	}

	const final = await withWork(ctx, "rlp: re-checking this host", () => doctorSummary(ctx));
	ctx.ui.notify(
		[
			"◈ setup done",
			"",
			`  doctor  ${final.ok ? "runnable" : "still has problems"} (${final.text})`,
			...(final.problems.length > 0 ? ["", ...final.problems.map((p) => `  ${p}`)] : []),
			"",
			"  /reload                pick up new endpoints and models in this session",
			"  /rlp-ladder            what the orchestrator will actually do",
			"  /rlp-plan \"<request>\"   see the gate, the DAG and the routing before running",
		].join("\n"),
		final.ok ? "info" : "warning",
	);
}

/** Pick one `provider/model` from the authenticated catalogue, with marks. */
async function chooseModel(ctx: ExtensionCommandContext, title: string, rows: ModelRow[]): Promise<string | undefined> {
	const providers = [...new Set(rows.map((r) => r.provider))].sort();
	const provider = await ctx.ui.select(`${title}\n\nProvider`, providers);
	if (!provider) return undefined;
	const inProvider = rows.filter((r) => r.provider === provider);
	const chosen = await ctx.ui.select(
		`${provider} — which model?`,
		inProvider.map((r) => `${r.current ? "●" : " "}${r.isDefault ? "★" : " "} ${r.id}`),
	);
	if (!chosen) return undefined;
	const id = chosen.replace(/^[●★\s]+/, "").trim();
	return `${provider}/${id}`;
}

// --- registration -----------------------------------------------------------------------

export default function rlpProvider(pi: ExtensionAPI): void {
	pi.registerCommand("provider", {
		description:
			"Model providers: list them, connect one (guided), test a connection, set a key, remove an endpoint",
		handler: async (args, ctx) => {
			const raw = args.trim();
			if (!raw) return providerHome(ctx);
			const [verb, ...rest] = raw.split(/\s+/);
			switch (verb) {
				case "list":
				case "show": {
					const summary = await providerSummary(ctx);
					if (!summary) return missingEngine(ctx);
					ctx.ui.notify(renderProviders(summary));
					return;
				}
				case "connect": {
					const connected = await connectWizard(ctx, rest[0]);
					if (connected) await offerUsage(pi, ctx, connected);
					return;
				}
				case "add": {
					// Scriptable when it has arguments; guided when it does not.
					if (rest.length < 2) return void (await connectWizard(ctx));
					const [id, baseUrl, ...models] = rest;
					const key = await ctx.ui.input(`API key for ${id} (blank if none)`, "sk-…");
					const reply = await engineJson(
						ctx,
						["provider", "add", id, baseUrl, ...models, "--key-stdin", "--json"],
						key?.trim() ?? "",
						60_000,
					);
					if (!reply?.ok) {
						ctx.ui.notify(`◈ could not attach ${id}: ${reply?.error ?? "unknown error"}`, "error");
						return;
					}
					ctx.ui.notify(
						reportApplied("connected", id, [
							`  endpoint  ${baseUrl}`,
							`  models    ${(reply.data.models as string[] | undefined)?.join(", ") ?? models.join(", ")}`,
							`  auth      ${key?.trim() ? "key written to auth.json (0600)" : `none yet — /provider key ${id}`}`,
							`  backup    ${String(reply.data.backup ?? "none (new file)")}`,
							"",
							"  /reload picks it up, then /provider test <id> proves it.",
						]),
					);
					return;
				}
				case "test":
				case "probe": {
					if (rest[0]) return void (await probeAndReport(ctx, { provider: rest[0] }));
					const summary = await providerSummary(ctx);
					const ids = summary?.providers.map((p) => p.id) ?? [];
					const chosen = ids.length === 1 ? ids[0] : await ctx.ui.select("Test which endpoint?", ids);
					if (chosen) await probeAndReport(ctx, { provider: chosen });
					return;
				}
				case "models":
				case "discover": {
					if (rest[0]) return void (await addModelsInteractive(ctx, rest[0]));
					const summary = await providerSummary(ctx);
					const chosen = await ctx.ui.select("Ask which endpoint?", summary?.providers.map((p) => p.id) ?? []);
					if (chosen) await addModelsInteractive(ctx, chosen);
					return;
				}
				case "key":
				case "login": {
					if (!rest[0]) {
						ctx.ui.notify("usage: /provider key <id> [--drop]", "warning");
						return;
					}
					if (rest.includes("--drop")) {
						const reply = await engineJson(ctx, ["provider", "key", rest[0], "--drop", "--json"], undefined, 30_000);
						ctx.ui.notify(
							reply?.ok
								? `◈ credential removed for ${rest[0]}`
								: `◈ could not remove it: ${reply?.error ?? "unknown error"}`,
							reply?.ok ? "info" : "warning",
						);
						return;
					}
					return void (await setKeyInteractive(ctx, rest[0]));
				}
				case "remove":
				case "rm": {
					if (!rest[0]) return void ctx.ui.notify("usage: /provider remove <id> [--key]", "warning");
					if (rest.includes("--key")) {
						const reply = await engineJson(ctx, ["provider", "remove", rest[0], "--drop-key", "--json"], undefined, 30_000);
						ctx.ui.notify(
							reply?.ok ? `◈ removed ${rest[0]} and its credential` : `◈ could not remove it: ${reply?.error ?? "unknown error"}`,
							reply?.ok ? "info" : "warning",
						);
						return;
					}
					return void (await removeInteractive(ctx, rest[0]));
				}
				default: {
					ctx.ui.notify(
						`◈ unknown provider verb "${verb}". Try: connect · test · models · key · remove · list`,
						"warning",
					);
					return providerHome(ctx);
				}
			}
		},
	});

	pi.registerCommand("setup", {
		description: "Guided first run: check this host, connect providers, choose the brain and the worker arms",
		handler: async (_args, ctx) => {
			if (!ctx.hasUI) {
				ctx.ui.notify(
					"◈ /setup is interactive. Without a UI: rlp doctor, then rlp provider add <id> <baseUrl> <model>.",
					"warning",
				);
				return;
			}
			await setupWizard(pi, ctx);
		},
	});
}