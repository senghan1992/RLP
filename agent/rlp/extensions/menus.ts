/**
 * Harness menus for RLP: a command index, a model browser, and provider
 * management.
 *
 * Why this exists as a dropped-in extension rather than fork patches: the
 * built-in `/` menu is long, flat, and silent about the harness's own
 * configuration. Finding `/scoped-models` in 25 rows, or working out which of
 * 487 models belong to *you*, is friction every session. And "attach another
 * model" currently means hand-editing two JSON files that hold credentials.
 *
 * Commands:
 *   /commands              everything the slash menu offers, grouped, live
 *   /models [filter]       models grouped by provider, marked, optionally pick
 *   /models --pick         interactive: provider -> model -> use / set default
 *   /provider              list providers: endpoint, models, credential state
 *   /provider add <id> <baseUrl> <modelId> [name]
 *   /provider remove <id>
 *
 * Safety rules this file keeps, because it writes to a credential store:
 *   - never print a secret: keys are reported only as present/absent;
 *   - always back up before writing models.json / auth.json / settings.json;
 *   - merge, never replace: unrelated keys in those files are preserved;
 *   - a bad argument is a printed line, never a stack and never a partial write.
 */
import { execFile } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";

const AGENT_DIR = process.env.RPI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
const MODELS_FILE = process.env.RLP_PI_MODELS || join(AGENT_DIR, "models.json");
const AUTH_FILE = process.env.RLP_PI_AUTH || join(AGENT_DIR, "auth.json");
const SETTINGS_FILE = join(AGENT_DIR, "settings.json");

type Json = Record<string, unknown>;

function readJson(path: string): Json | undefined {
	try {
		return JSON.parse(readFileSync(path, "utf8"));
	} catch {
		return undefined;
	}
}

/** Write JSON through a timestamped backup, so a bad write is always undoable. */
function writeJson(path: string, data: Json): string {
	mkdirSync(dirname(path), { recursive: true });
	let backup = "";
	if (existsSync(path)) {
		backup = `${path}.bak.${Date.now()}`;
		copyFileSync(path, backup);
	}
	writeFileSync(path, `${JSON.stringify(data, null, 2)}\n`);
	return backup;
}

// --- providers ------------------------------------------------------------------

interface ProviderEntry {
	name: string;
	baseUrl: string;
	api?: string;
	apiKey?: string;
	models?: Array<{ id: string; name?: string }>;
}

function loadProviders(): Record<string, ProviderEntry> {
	const doc = readJson(MODELS_FILE);
	const providers = doc?.providers;
	return providers && typeof providers === "object" ? (providers as Record<string, ProviderEntry>) : {};
}

/** Auth state without ever exposing the secret itself. */
function authState(provider: string): "oauth" | "key" | "none" {
	const auth = readJson(AUTH_FILE);
	const entry = auth?.[provider];
	if (!entry || typeof entry !== "object") return "none";
	const e = entry as Json;
	if (typeof e.access === "string" && e.access) return "oauth";
	if (typeof e.key === "string" && e.key) return "key";
	return "none";
}

function credentialsHint(provider: string): string {
	return authState(provider) === "none"
		? `no credentials — run /login ${provider}`
		: "credential present";
}

// --- the RLP ladder (optional decoration) -----------------------------------------

interface LadderArm {
	provider: string;
	model: string;
	worker: string;
	available: boolean;
	when: string;
}

function loadLadderArms(): LadderArm[] {
	const override = process.env.RLP_ORCHESTRATION;
	const path = override
		? resolve(process.cwd(), override)
		: join(AGENT_DIR, "orchestration.json");
	const doc = readJson(path);
	const workers = doc?.workers;
	if (!Array.isArray(workers)) return [];
	const arms: LadderArm[] = [];
	for (const worker of workers as Array<Json>) {
		if (!worker || typeof worker !== "object") continue;
		const models = Array.isArray(worker.models) ? (worker.models as Array<Json>) : [];
		for (const arm of models) {
			const model = typeof arm?.model === "string" ? arm.model : "";
			if (!model.includes("/")) continue;
			arms.push({
				provider: model.split("/")[0],
				model: model.slice(model.indexOf("/") + 1),
				worker: typeof worker.id === "string" ? worker.id : "?",
				available: worker.available !== false,
				when: typeof arm.when === "string" ? arm.when : "",
			});
		}
	}
	return arms;
}

// --- editing the RLP ladder --------------------------------------------------------
//
// The ladder is the one file that decides which models orchestrate and which
// models work. Until now it was visible (/orchestration, /models, /rlp-ladder)
// but editable only by hand. These commands let a session change it in place,
// through the engine's validated `config` op: validate first, timestamped
// backup, atomic write, and the running harness picks the edit up on the next
// prompt rebuild because it caches on the file's mtime.

function ladderPath(): string {
	const override = process.env.RLP_ORCHESTRATION;
	return override ? resolve(process.cwd(), override) : join(AGENT_DIR, "orchestration.json");
}

/** The same walk-up the RLP commands use, so the editor works in any project. */
function findPython(cwd: string): string | undefined {
	const roots: string[] = [];
	if (process.env.RLP_ROOT) roots.push(resolve(process.env.RLP_ROOT));
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

interface ConfigReply {
	ok: boolean;
	lines: string[];
}

/** Apply ops through the engine. Never raises; a failure is rendered lines. */
async function applyConfig(ctx: ExtensionCommandContext, ops: unknown[], dryRun = false): Promise<ConfigReply> {
	const python = findPython(ctx.cwd);
	if (!python) {
		return {
			ok: false,
			lines: ["Ⓡ the decision engine is not installed (no rlp-svc/.venv/bin/python)", "  sh <RLP>/scripts/install.sh"],
		};
	}
	const args = ["-m", "rlp_svc", "config", JSON.stringify(ops), "--json"];
	if (dryRun) args.push("--dry-run");
	const { stdout, stderr } = await new Promise<{ stdout: string; stderr: string }>((settle) => {
		execFile(
			python,
			args,
			{ cwd: ctx.cwd, env: process.env, maxBuffer: 8 * 1024 * 1024, timeout: 60_000 },
			(_error, out, serr) => settle({ stdout: String(out ?? ""), stderr: String(serr ?? "") }),
		);
	});
	if (!stdout.trim()) return { ok: false, lines: [`Ⓡ ladder edit failed: ${stderr.trim().slice(-300) || "no output"}`] };
	try {
		const payload = JSON.parse(stdout.slice(stdout.indexOf("{"))) as {
			ok?: boolean;
			error?: string;
			result?: { path: string; backup?: string; dry_run?: boolean; ladder: Json };
		};
		if (!payload.ok || !payload.result) return { ok: false, lines: [`Ⓡ ladder edit rejected: ${payload.error ?? "unknown error"}`] };
		const r = payload.result;
		const workers = (r.ladder.workers as Json[]) ?? [];
		return {
			ok: true,
			lines: [
				`Ⓡ ladder updated${r.dry_run ? " (dry run)" : ""} — ${r.path}`,
				r.backup ? `  backup  ${r.backup}` : "",
				`  brain   ${r.ladder.brain}`,
				...workers.map((w) => {
					const arms = (w.models as Json[] | undefined) ?? [];
					return `  [${w.id}]${w.available === false ? " [UNAVAILABLE]" : ""} ${arms.map((m) => m.model).join(", ")}`;
				}),
			].filter(Boolean),
		};
	} catch {
		return { ok: false, lines: [`Ⓡ unparseable config output: ${stdout.slice(0, 200)}`] };
	}
}

function ladderRaw(): Json | undefined {
	return readJson(ladderPath());
}

/** Render the ladder as the editor sees it. */
function renderLadder(): string {
	const doc = ladderRaw();
	if (!doc) return [`Ⓡ no orchestration ladder at ${ladderPath()}`, "  sh <RLP>/scripts/install.sh installs one"].join("\n");
	const routing = (doc.routing as Json) ?? {};
	const lines = [
		`Ⓡ RLP ladder · ${ladderPath()}`,
		`  brain  ${doc.brain}`,
		`  gate   ${routing.gate ?? "hybrid"} · escalateBelow=${routing.escalateBelow ?? "-"} · cap=${routing.maxDispatchesPerTurn ?? "-"} · timeout=${routing.workerTimeoutMs ?? "-"}ms`,
		`  review crossVendor=${((doc.review as Json) ?? {}).crossVendor ?? false}`,
		`  plan   critique=${((doc.planning as Json) ?? {}).critique ?? true} · maxRefines=${((doc.planning as Json) ?? {}).maxRefines ?? 1} · recursion=${((doc.planning as Json) ?? {}).recursiveDepth ?? 1} · artifacts=${((doc.planning as Json) ?? {}).artifactPassing ?? true} · verify=${((doc.planning as Json) ?? {}).verifySamples ?? 3}`,
		`  rlm    depth=${((doc.rlm as Json) ?? {}).maxDepth ?? "-"} · iterations=${((doc.rlm as Json) ?? {}).maxIterations ?? "-"} · budget=${((doc.rlm as Json) ?? {}).maxBudget ?? "-"} · timeout=${((doc.rlm as Json) ?? {}).maxTimeout ?? "-"}`,
		"",
	];
	for (const worker of (doc.workers as Json[]) ?? []) {
		lines.push(`[${worker.id}]${worker.available === false ? "  UNAVAILABLE" : ""}${worker.harness ? `  on ${worker.harness}` : ""}`);
		if (worker.available === false && worker.availabilityNote) lines.push(`    reason: ${worker.availabilityNote}`);
		((worker.models as Json[]) ?? []).forEach((arm, i) => {
			lines.push(`  ${i === 0 ? "DEFAULT" : `arm ${i + 1}`}  ${arm.model}  roles=${((arm.roles as string[]) ?? []).join(",")}`);
			lines.push(`           when: ${collapse(String(arm.when ?? ""), 90)}`);
		});
	}
	lines.push("");
	lines.push("  /rlp-config brain <provider/model>        make it the orchestrator model");
	lines.push("  /rlp-roles --pick                         set the model for each role");
	lines.push("  /rlp-config add-arm <worker> <ref> [roles] append a worker arm");
	lines.push("  /rlp-config set-arm <worker> <i|ref> <ref> replace one arm");
	lines.push("  /rlp-config rm-arm <worker> <i|ref>        remove one arm");
	lines.push("  /rlp-config worker <id> on|off [note]      enable/disable a worker");
	lines.push("  /rlp-config gate <laya|hybrid>             how triage decides when unsure");
	lines.push("  /rlp-config critique on|off · refines <n> · recursion <n>   plan quality loop");
	lines.push("  /rlp-config rlm-depth <n> · rlm-iterations <n> · rlm-budget <n|off>   RLM knobs");
	lines.push("  /rlp-config escalate <0..1> · cap <n> · timeout <ms> · cross-vendor on|off");
	return lines.join("\n");
}

/** Parse a `/rlp-config ...` line into engine ops. Returns undefined on a bad verb. */
function opsFromArgs(args: string): unknown[] | string {
	const [verb, ...rest] = args.trim().split(/\s+/);
	switch (verb) {
		case "brain":
			return rest[0] ? [{ op: "set_brain", model: rest[0] }] : "usage: /rlp-config brain <provider/model>";
		case "add-arm": {
			const [worker, model, roles, ...when] = rest;
			if (!worker || !model) return "usage: /rlp-config add-arm <worker> <provider/model> [roles] [when]";
			return [
				{
					op: "add_arm",
					worker,
					model,
					roles: (roles || "code").split(",").map((r) => r.trim()).filter(Boolean),
					when: when.join(" ") || "added from /rlp-config",
				},
			];
		}
		case "set-arm": {
			const [worker, match, model, ...when] = rest;
			if (!worker || !match || !model) return "usage: /rlp-config set-arm <worker> <index|provider/model> <provider/model> [when]";
			return [{ op: "set_arm", worker, match: /^\d+$/.test(match) ? Number(match) : match, model, when: when.join(" ") || undefined }];
		}
		case "rm-arm": {
			const [worker, match] = rest;
			if (!worker || !match) return "usage: /rlp-config rm-arm <worker> <index|provider/model>";
			return [{ op: "remove_arm", worker, match: /^\d+$/.test(match) ? Number(match) : match }];
		}
		case "move-arm": {
			const [worker, from, to] = rest;
			if (!worker || !from || !to) return "usage: /rlp-config move-arm <worker> <from> <to>";
			return [{ op: "move_arm", worker, from: Number(from), to: Number(to) }];
		}
		case "worker": {
			const [id, toggle, ...note] = rest;
			if (!id || !["on", "off"].includes(toggle ?? "")) return "usage: /rlp-config worker <id> on|off [note]";
			return [{ op: "set_worker_available", worker: id, available: toggle === "on", note: note.join(" ") || undefined }];
		}
		case "gate":
			if (!["laya", "hybrid"].includes(rest[0] ?? "")) return "usage: /rlp-config gate <laya|hybrid>";
			return [{ op: "set_routing", key: "gate", value: rest[0] }];
		case "escalate":
			return rest[0] === undefined ? "usage: /rlp-config escalate <0..1>" : [{ op: "set_routing", key: "escalateBelow", value: Number(rest[0]) }];
		case "cap":
			return rest[0] === undefined ? "usage: /rlp-config cap <n>" : [{ op: "set_routing", key: "maxDispatchesPerTurn", value: Number(rest[0]) }];
		case "timeout":
			return rest[0] === undefined ? "usage: /rlp-config timeout <ms>" : [{ op: "set_routing", key: "workerTimeoutMs", value: Number(rest[0]) }];
		case "cross-vendor":
			if (!["on", "off"].includes(rest[0] ?? "")) return "usage: /rlp-config cross-vendor on|off";
			return [{ op: "set_review", crossVendor: rest[0] === "on" }];
		case "critique":
			if (!["on", "off"].includes(rest[0] ?? "")) return "usage: /rlp-config critique on|off";
			return [{ op: "set_planning", key: "critique", value: rest[0] === "on" }];
		case "artifacts":
			if (!["on", "off"].includes(rest[0] ?? "")) return "usage: /rlp-config artifacts on|off";
			return [{ op: "set_planning", key: "artifactPassing", value: rest[0] === "on" }];
		case "refines":
			return rest[0] === undefined
				? "usage: /rlp-config refines <0..3>"
				: [{ op: "set_planning", key: "maxRefines", value: Number(rest[0]) }];
		case "recursion":
			return rest[0] === undefined
				? "usage: /rlp-config recursion <0..3>"
				: [{ op: "set_planning", key: "recursiveDepth", value: Number(rest[0]) }];
		case "verify-samples":
			return rest[0] === undefined
				? "usage: /rlp-config verify-samples <1..7>"
				: [{ op: "set_planning", key: "verifySamples", value: Number(rest[0]) }];
		case "rlm-depth":
			return rest[0] === undefined
				? "usage: /rlp-config rlm-depth <n>"
				: [{ op: "set_rlm", key: "maxDepth", value: Number(rest[0]) }];
		case "rlm-iterations":
			return rest[0] === undefined
				? "usage: /rlp-config rlm-iterations <n>"
				: [{ op: "set_rlm", key: "maxIterations", value: Number(rest[0]) }];
		case "rlm-budget":
			if (rest[0] === "off") return [{ op: "set_rlm", key: "maxBudget", value: null }];
			return rest[0] === undefined
				? "usage: /rlp-config rlm-budget <n|off>"
				: [{ op: "set_rlm", key: "maxBudget", value: Number(rest[0]) }];
		case "rlm-timeout":
			if (rest[0] === "off") return [{ op: "set_rlm", key: "maxTimeout", value: null }];
			return rest[0] === undefined
				? "usage: /rlp-config rlm-timeout <seconds|off>"
				: [{ op: "set_rlm", key: "maxTimeout", value: Number(rest[0]) }];
		default:
			return `unknown verb ${JSON.stringify(verb)}`;
	}
}

/** Pick `provider/model` from the authenticated catalogue, or undefined. */
async function pickModelRef(ctx: ExtensionCommandContext): Promise<string | undefined> {
	const rows = modelRows(ctx).filter((r) => r.authenticated);
	const providers = [...new Set(rows.map((r) => r.provider))].sort();
	const provider = await ctx.ui.select("Provider", providers);
	if (!provider) return undefined;
	const inProvider = rows.filter((r) => r.provider === provider);
	const picked = await ctx.ui.select(`${provider} models`, inProvider.map((r) => r.id));
	if (!picked) return undefined;
	return `${provider}/${picked}`;
}

/** The interactive editor behind bare `/rlp-config`. */
async function editLadderInteractively(ctx: ExtensionCommandContext, setModel: SetModel): Promise<void> {
	const doc = ladderRaw();
	if (!doc) {
		ctx.ui.notify(renderLadder(), "warning");
		return;
	}
	const workers = ((doc.workers as Json[]) ?? []).map((w) => String(w.id));
	const action = await ctx.ui.select("RLP ladder — what to change?", [
		"Set the orchestrator model (brain)",
		"Set the model per role (role → model)",
		"Add a worker arm",
		"Replace a worker arm",
		"Enable / disable a worker",
		"Escalation gate (laya vs hybrid)",
	]);
	if (!action) return;

	if (action.startsWith("Set the model per role")) {
		const view = await fetchLadderView(ctx);
		if (!view) {
			ctx.ui.notify(`Ⓡ no orchestration ladder at ${ladderPath()}`, "warning");
			return;
		}
		return editRolesInteractively(ctx, view);
	}

	if (action.startsWith("Set the orchestrator")) {
		const ref = await pickModelRef(ctx);
		if (!ref) return;
		const reply = await applyConfig(ctx, [{ op: "set_brain", model: ref }]);
		ctx.ui.notify(reply.lines.join("\n"), reply.ok ? "info" : "warning");
		if (!reply.ok) return;
		// Also move the live session, so "brain" stops being a description.
		try {
			const [provider, id] = [ref.slice(0, ref.indexOf("/")), ref.slice(ref.indexOf("/") + 1)];
			const model = ctx.modelRegistry.find(provider, id);
			if (model && (await setModel(model as never))) ctx.ui.notify(`Ⓡ this session now runs on ${ref}`, "info");
		} catch {
			/* the ladder is edited either way */
		}
		return;
	}

	if (action.startsWith("Add a worker arm")) {
		const worker = workers.length === 1 ? workers[0] : await ctx.ui.select("Worker", workers);
		if (!worker) return;
		const ref = await pickModelRef(ctx);
		if (!ref) return;
		const roles = (await ctx.ui.input("Roles (comma-separated)", "code,review")) ?? "code";
		const reply = await applyConfig(ctx, [
			{ op: "add_arm", worker, model: ref, roles: roles.split(",").map((r) => r.trim()).filter(Boolean), when: "added from the /rlp-config menu" },
		]);
		ctx.ui.notify(reply.lines.join("\n"), reply.ok ? "info" : "warning");
		return;
	}

	if (action.startsWith("Replace a worker arm")) {
		const worker = workers.length === 1 ? workers[0] : await ctx.ui.select("Worker", workers);
		if (!worker) return;
		const workerDoc = ((doc.workers as Json[]) ?? []).find((w) => w.id === worker);
		const arms = ((workerDoc?.models as Json[]) ?? []).map((m, i) => `${i}: ${m.model}`);
		if (arms.length === 0) return;
		const slot = await ctx.ui.select("Which arm?", arms);
		if (!slot) return;
		const ref = await pickModelRef(ctx);
		if (!ref) return;
		const reply = await applyConfig(ctx, [{ op: "set_arm", worker, match: Number(slot.split(":")[0]), model: ref }]);
		ctx.ui.notify(reply.lines.join("\n"), reply.ok ? "info" : "warning");
		return;
	}

	if (action.startsWith("Enable / disable")) {
		const worker = await ctx.ui.select("Worker", workers);
		if (!worker) return;
		const toggle = await ctx.ui.select(`${worker} is`, ["available (on)", "unavailable (off)"]);
		if (!toggle) return;
		const note = toggle.startsWith("unavailable")
			? ((await ctx.ui.input("Why is it unavailable?", "entitlement exhausted")) ?? "marked unavailable from /rlp-config")
			: undefined;
		const reply = await applyConfig(ctx, [{ op: "set_worker_available", worker, available: toggle.startsWith("available"), note }]);
		ctx.ui.notify(reply.lines.join("\n"), reply.ok ? "info" : "warning");
		return;
	}

	const gate = await ctx.ui.select("Escalation gate", [
		"hybrid — laya plus deterministic fan-out signals (recommended)",
		"laya — laya alone; unsure always defaults to direct",
	]);
	if (!gate) return;
	const reply = await applyConfig(ctx, [{ op: "set_routing", key: "gate", value: gate.startsWith("hybrid") ? "hybrid" : "laya" }]);
	ctx.ui.notify(reply.lines.join("\n"), reply.ok ? "info" : "warning");
}

// --- role bindings: role -> model(s) -----------------------------------------------
//
// The planner picks a model per *role* (code, debug, review, research, docs,
// explore, …): `roles.<role>` binds a role to a model, or to an ordered list of
// them, and it wins over the ladder's arm-priority order. This is the
// `/model`-style menu for the orchestrator: pick a role, then pick one or more
// models from a provider, in priority order.

interface RoleResolution {
	model: string;
	worker: string | null;
	binding: boolean;
	chain?: string[];
	index?: number;
	unavailable?: boolean;
}

interface LadderView {
	path: string;
	brain: string;
	roles: Record<string, string | string[]>;
	known_roles: string[];
	model_pool: string[];
	role_resolution: Record<string, RoleResolution | null>;
	workers: Array<{ id: string; harness?: string; available?: boolean; models: Array<{ model: string; roles: string[] }> }>;
}

/** The resolved ladder, from the engine (authoritative: it owns role resolution). */
async function fetchLadderView(ctx: ExtensionCommandContext): Promise<LadderView | undefined> {
	const python = findPython(ctx.cwd);
	if (!python) return undefined;
	const { stdout } = await new Promise<{ stdout: string }>((settle) => {
		execFile(
			python,
			["-m", "rlp_svc", "ladder", "--json"],
			{ cwd: ctx.cwd, env: process.env, maxBuffer: 8 * 1024 * 1024, timeout: 60_000 },
			(_error, out) => settle({ stdout: String(out ?? "") }),
		);
	});
	try {
		const payload = JSON.parse(stdout.slice(stdout.indexOf("{"))) as { ok?: boolean; result?: LadderView };
		return payload.ok && payload.result ? payload.result : undefined;
	} catch {
		return undefined;
	}
}

function renderRoles(view: LadderView): string {
	const lines = [`Ⓡ RLP roles · ${view.path}`, ""];
	lines.push("  model chain: [chosen] > fallback > …  (first entry a dispatchable worker can serve wins)");
	lines.push("");
	for (const role of view.known_roles) {
		const res = view.role_resolution[role];
		if (!res) {
			lines.push(`  ${role.padEnd(13)} (no arm carries this role)`);
			continue;
		}
		const chain = res.chain && res.chain.length > 0 ? res.chain : [res.model];
		const shown = chain.map((m, i) => (i === (res.index ?? 0) ? `[${m}]` : m)).join(" > ");
		const via = `${res.binding ? "binding" : "arm priority"}${res.worker ? ` on ${res.worker}` : "  [NO DISPATCHABLE WORKER]"}`;
		lines.push(`  ${role.padEnd(13)} ${shown}`);
		lines.push(`  ${" ".repeat(13)} ${via}`);
	}
	lines.push("");
	lines.push("  /rlp-roles --pick                  pick a role, then one or more models from a provider");
	lines.push("  /rlp-roles <role> a/x,b/y          bind a priority chain (first that can run wins)");
	lines.push("  /rlp-roles <role> off              clear the binding (back to arm priority)");
	lines.push(`  pool: ${view.model_pool.join(", ")}`);
	return lines.join("\n");
}

/** Parse "1 3 5-7 all name" into the selected options, preserving the typed order. */
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

/** No multi-select dialog exists on the command context, so a numbered index prompt is it. */
async function multiSelectIndices(ctx: ExtensionCommandContext, title: string, options: string[]): Promise<string[]> {
	if (options.length === 0) return [];
	const cap = 80;
	const list = options.slice(0, cap).map((o, i) => `  ${String(i + 1).padStart(3)}. ${o}`).join("\n");
	ctx.ui.notify(
		[
			title,
			"",
			list,
			options.length > cap ? `  …and ${options.length - cap} more` : "",
			"",
			'  reply with numbers or ranges (e.g. "1 3 5-7"), names, or "all"',
		]
			.filter(Boolean)
			.join("\n"),
	);
	const raw = await ctx.ui.input("Select models (order = priority)", "1 3 5-7");
	if (!raw?.trim()) return [];
	return parseSelection(raw.trim(), options);
}

/** Provider first, then one or more of the models attached to it. */
async function pickProviderModels(ctx: ExtensionCommandContext): Promise<string[] | undefined> {
	const rows = modelRows(ctx).filter((r) => r.authenticated);
	if (rows.length === 0) {
		ctx.ui.notify("Ⓡ no authenticated models to choose from — /provider or /login first", "warning");
		return undefined;
	}
	const providers = [...new Set(rows.map((r) => r.provider))].sort();
	const provider = await ctx.ui.select("Provider (models attached to it)", providers);
	if (!provider) return undefined;
	const ids = rows.filter((r) => r.provider === provider).map((r) => r.id);
	const chosen = await multiSelectIndices(ctx, `${provider} — ${ids.length} model(s): pick one or more`, ids);
	return chosen.length > 0 ? chosen.map((id) => `${provider}/${id}`) : undefined;
}

/** Build the ops to bind `role` to an ordered list of models, adding new arms first. */
async function bindRoleChainOps(
	ctx: ExtensionCommandContext,
	view: LadderView,
	role: string,
	refs: string[],
): Promise<unknown[] | string> {
	const chain = [...new Set(refs.filter(Boolean))];
	if (chain.length === 0) return "no models selected";
	const ops: unknown[] = [];
	const missing = chain.filter((ref) => !view.model_pool.includes(ref));
	if (missing.length > 0) {
		const piWorkers = view.workers.filter((w) => (w.harness ?? "pi") === "pi");
		const pool = piWorkers.length > 0 ? piWorkers : view.workers;
		if (pool.length === 0) return "the ladder has no worker to add the arm(s) to";
		const worker =
			pool.length === 1 || !ctx.hasUI
				? pool[0].id
				: await ctx.ui.select(`Add ${missing.length} new arm(s) to which worker?`, pool.map((w) => w.id));
		if (!worker) return "cancelled";
		for (const ref of missing) {
			ops.push({ op: "add_arm", worker, model: ref, roles: [role], when: `bound to the ${role} role from /rlp-roles` });
		}
	}
	ops.push(chain.length > 1 ? { op: "set_role", role, models: chain } : { op: "set_role", role, model: chain[0] });
	return ops;
}

async function editRolesInteractively(ctx: ExtensionCommandContext, view: LadderView): Promise<void> {
	const roleChoice = await ctx.ui.select(
		"RLP role",
		view.known_roles.map((role) => {
			const res = view.role_resolution[role];
			const current = res
				? res.chain && res.chain.length > 1
					? res.chain.join(" > ")
					: res.model
				: "(none)";
			return `${role}  →  ${current}`;
		}),
	);
	if (!roleChoice) return;
	const role = roleChoice.trim().split(/\s+/)[0];
	const picked = await ctx.ui.select(`Set the "${role}" role`, [
		"＋ pick models from a provider (multi-select)…",
		...view.model_pool,
		"off — use arm priority",
	]);
	if (!picked) return;
	if (picked.startsWith("off")) {
		const reply = await applyConfig(ctx, [{ op: "clear_role", role }]);
		ctx.ui.notify(reply.lines.join("\n"), reply.ok ? "info" : "warning");
		return;
	}
	let refs: string[];
	if (picked.startsWith("＋")) {
		const chosen = await pickProviderModels(ctx);
		if (!chosen) return;
		refs = chosen;
	} else {
		refs = [picked];
	}
	const ops = await bindRoleChainOps(ctx, view, role, refs);
	if (typeof ops === "string") {
		if (ops !== "cancelled") ctx.ui.notify(`Ⓡ ${ops}`, "warning");
		return;
	}
	const reply = await applyConfig(ctx, ops);
	ctx.ui.notify([...reply.lines, "", `  ${role} → ${refs.join(" > ")}`].join("\n"), reply.ok ? "info" : "warning");
}

// --- /models ----------------------------------------------------------------------

interface ModelRow {
	provider: string;
	id: string;
	name: string;
	authenticated: boolean;
	current: boolean;
	isDefault: boolean;
	ladders: LadderArm[];
}

function modelRows(ctx: ExtensionCommandContext): ModelRow[] {
	const current = ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : undefined;
	const settings = readJson(SETTINGS_FILE);
	const savedDefault =
		typeof settings?.defaultProvider === "string" && typeof settings?.defaultModel === "string"
			? `${settings.defaultProvider}/${settings.defaultModel}`
			: undefined;
	const arms = loadLadderArms();
	const rows: ModelRow[] = [];
	for (const model of ctx.modelRegistry.getAll()) {
		const ref = `${model.provider}/${model.id}`;
		rows.push({
			provider: model.provider,
			id: model.id,
			name: model.name || model.id,
			authenticated: ctx.modelRegistry.hasConfiguredAuth(model),
			current: ref === current,
			isDefault: ref === savedDefault,
			ladders: arms.filter((a) => a.provider === model.provider && a.model === model.id),
		});
	}
	return rows;
}

/** One line, no runs of whitespace — safe to drop into a fixed-width table. */
function collapse(text: string, limit: number): string {
	const flat = (text ?? "").replace(/\s+/g, " ").trim();
	return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

/**
 * Render the model list.
 *
 * Default is the set a person can actually use: providers that hold a
 * credential. Listing all of them is the problem this command exists to fix —
 * a refreshed catalogue on this host is 45 providers and 1559 models, and
 * scrolling that to find your own is exactly why the list read as "missing".
 * `--all` brings the rest back, deliberately.
 */
function renderModels(ctx: ExtensionCommandContext, filter: string, showAll: boolean): string {
	const all = modelRows(ctx);
	let rows = showAll ? all : all.filter((r) => r.authenticated);
	if (filter) {
		const needle = filter.toLowerCase();
		rows = rows.filter(
			(r) =>
				r.provider.toLowerCase().includes(needle) ||
				r.id.toLowerCase().includes(needle) ||
				r.name.toLowerCase().includes(needle),
		);
	}
	const hidden = all.length - all.filter((r) => r.authenticated).length;
	if (rows.length === 0) {
		return [
			"Ⓡ models",
			"",
			filter ? `  no model matches "${filter}".` : "  no authenticated models are visible.",
			"  /provider lists configured endpoints · /login adds a credential",
			hidden > 0 && !showAll ? `  ${hidden} more model(s) belong to providers without a credential — /models --all` : "",
		]
			.filter(Boolean)
			.join("\n");
	}

	const providers = [...new Set(rows.map((r) => r.provider))];
	const authed = providers.filter((p) => rows.some((r) => r.provider === p && r.authenticated));
	const lines = [
		`Ⓡ models · ${rows.length}${filter ? ` matching "${filter}"` : ""} of ${all.length} · ${authed.length}/${providers.length} providers authenticated`,
		"  ● this session   ★ your default   ⚑ RLP ladder arm   ○ no credentials",
		"  * the ladder marks that worker unavailable on this host",
		"",
	];
	for (const provider of providers) {
		const group = rows.filter((r) => r.provider === provider);
		const hasAuth = group.some((r) => r.authenticated);
		lines.push(`${hasAuth ? "" : "○ "}${provider}  (${group.length})`);
		for (const r of group) {
			const marks = [
				r.current ? "●" : " ",
				r.isDefault ? "★" : " ",
				r.ladders.length > 0 ? "⚑" : " ",
				r.authenticated ? " " : "○",
			].join(" ");
			const arm = r.ladders[0];
			// The `when` text is operator prose and wraps. Collapse it to one line
			// and keep the whole line under a conservative 120 columns, because a
			// wrapped continuation lands in the margin and reads as a broken table.
			// Budget: ~45 columns of indent + id + name, so the note gets ~40.
			const note = arm
				? `  — ${collapse(arm.available ? arm.worker : `${arm.worker}*`, 14)} ${collapse(arm.when, 40)}`
				: r.isDefault
					? "  — your saved default"
					: "";
			lines.push(`  ${marks} ${r.id}${r.name !== r.id ? `  (${r.name})` : ""}${note}`);
		}
		lines.push("");
	}
	lines.push("  /models --pick              switch model, or set a new default");
	lines.push(
		hidden > 0 && !showAll
			? `  /models --all               include the ${hidden} model(s) on providers with no credential`
			: "  /models --all               include providers with no credential",
	);
	lines.push("  /provider                   endpoints and credential state");
	lines.push("  /login <provider>           authenticate a provider");
	lines.push("  /rlp-config                 show or edit the orchestration ladder");
	lines.push("  /commands                   the full slash index");
	return lines.join("\n").trimEnd();
}

/**
 * `setModel` lives on the ExtensionAPI, not on the command context — passing
 * `ctx` and calling `ctx.setModel` throws at runtime while type-checking
 * cleanly, because the context type simply has no such member and the call was
 * reached through a loose shape. The function is captured from `pi` instead.
 */
type SetModel = (model: unknown) => Promise<boolean>;

async function pickModel(ctx: ExtensionCommandContext, setModel: SetModel): Promise<void> {
	if (!ctx.hasUI) return;
	const rows = modelRows(ctx);
	const providers = [
		...new Set(
			rows.filter((r) => r.authenticated).map((r) => r.provider),
		),
	].sort();
	const provider = await ctx.ui.select("Which provider?", providers);
	if (!provider) return;
	const inProvider = rows.filter((r) => r.provider === provider);	const model = await ctx.ui.select(
		`${provider} models`,
		inProvider.map((r) => {
			const marks = [r.current ? "●" : " ", r.isDefault ? "★" : " ", r.authenticated ? " " : "○"].join("");
			return `${marks} ${r.id}`;
		}),
	);
	if (!model) return;
	const ref = model.replace(/^[●★○\s]+/, "").trim();
	const target = inProvider.find((r) => r.id === ref);
	if (!target) return;

	const action = await ctx.ui.select(`${provider}/${target.id}`, [
		"Use for this session",
		"Set as the default for new sessions",
		"Add it to the RLP ladder as a worker arm",
		"Make it the RLP orchestrator (brain)",
		"Bind as the model for an RLP role",
		"Cancel",
	]);
	if (!action || action === "Cancel") return;

	if (action === "Use for this session") {
		const model = ctx.modelRegistry.find(target.provider, target.id);
		if (!model) {
			ctx.ui.notify(`Ⓡ ${target.provider}/${target.id} is not in the runtime catalogue.`, "warning");
			return;
		}
		const ok = await setModel(model);
		ctx.ui.notify(
			ok
				? `Ⓡ session model is now ${target.provider}/${target.id}`
				: `Ⓡ could not switch: ${target.provider} has no usable credential — /login ${target.provider}`,
			ok ? "info" : "warning",
		);
		return;
	}

	if (action === "Add it to the RLP ladder as a worker arm") {
		const doc = ladderRaw();
		const workers = ((doc?.workers as Json[]) ?? []).map((w) => String(w.id));
		if (workers.length === 0) {
			ctx.ui.notify(
				[`Ⓡ no orchestration ladder at ${ladderPath()}`, "  sh <RLP>/scripts/install.sh installs one, or /rlp-config to inspect"].join(
					"\n",
				),
				"warning",
			);
			return;
		}
		const worker = workers.length === 1 ? workers[0] : await ctx.ui.select("Which worker?", workers);
		if (!worker) return;
		const roles = (await ctx.ui.input("Roles (comma-separated)", "code,review,docs")) ?? "code";
		const reply = await applyConfig(ctx, [
			{
				op: "add_arm",
				worker,
				model: `${target.provider}/${target.id}`,
				roles: roles.split(",").map((r) => r.trim()).filter(Boolean),
				when: "added from /models",
			},
		]);
		ctx.ui.notify(reply.lines.join("\n"), reply.ok ? "info" : "warning");
		return;
	}

	if (action === "Make it the RLP orchestrator (brain)") {
		const reply = await applyConfig(ctx, [{ op: "set_brain", model: `${target.provider}/${target.id}` }]);
		ctx.ui.notify(reply.lines.join("\n"), reply.ok ? "info" : "warning");
		if (reply.ok) {
			const model = ctx.modelRegistry.find(target.provider, target.id);
			if (model && (await setModel(model as never))) ctx.ui.notify(`Ⓡ this session now runs on ${target.provider}/${target.id}`, "info");
		}
		return;
	}

	if (action === "Bind as the model for an RLP role") {
		const view = await fetchLadderView(ctx);
		if (!view) {
			ctx.ui.notify(`Ⓡ no orchestration ladder at ${ladderPath()}`, "warning");
			return;
		}
		const role = await ctx.ui.select("Bind this model to which RLP role?", view.known_roles);
		if (!role) return;
		const scope = await ctx.ui.select(`Bind ${target.id} to the "${role}" role`, [
			"Just this model",
			`Pick several from ${target.provider} (multi-select)…`,
			"Cancel",
		]);
		if (!scope || scope === "Cancel") return;
		let refs = [`${target.provider}/${target.id}`];
		if (scope.startsWith("Pick several")) {
			const ids = modelRows(ctx)
				.filter((r) => r.authenticated && r.provider === target.provider)
				.map((r) => r.id);
			const chosen = await multiSelectIndices(ctx, `${target.provider} — pick one or more models`, ids);
			if (chosen.length > 0) refs = chosen.map((id) => `${target.provider}/${id}`);
		}
		const ops = await bindRoleChainOps(ctx, view, role, refs);
		if (typeof ops === "string") {
			if (ops !== "cancelled") ctx.ui.notify(`Ⓡ ${ops}`, "warning");
			return;
		}
		const reply = await applyConfig(ctx, ops);
		ctx.ui.notify([...reply.lines, "", `  ${role} → ${refs.join(" > ")}`].join("\n"), reply.ok ? "info" : "warning");
		return;
	}

	const settings = readJson(SETTINGS_FILE) ?? {};
	settings.defaultProvider = target.provider;
	settings.defaultModel = target.id;
	const backup = writeJson(SETTINGS_FILE, settings);
	ctx.ui.notify(
		[
			`Ⓡ default is now ${target.provider}/${target.id}`,
			"",
			"  Applies to new sessions. Managed RLP workers keep using the ladder arms.",
			backup ? `  Backup: ${backup}` : "",
		]
			.filter(Boolean)
			.join("\n"),
	);
}

// --- /provider ---------------------------------------------------------------------

function renderProviders(): string {
	const providers = loadProviders();
	const ids = Object.keys(providers);
	if (ids.length === 0) {
		return [
			"Ⓡ providers",
			"",
			`  no endpoints configured (${MODELS_FILE})`,
			"",
			"  /provider add <id> <baseUrl> <modelId> [display name]",
		].join("\n");
	}
	const lines = [`Ⓡ providers · ${ids.length} in ${MODELS_FILE}`, ""];
	for (const id of ids) {
		const p = providers[id];
		const count = Array.isArray(p.models) ? p.models.length : 0;
		const state = authState(id);
		const badge = state === "none" ? "○" : state === "oauth" ? "◆" : "●";
		lines.push(`${badge} ${id}  —  ${p.baseUrl}`);
		lines.push(`   ${count} model(s) · ${credentialsHint(id)}`);
		if (count > 0) {
			const preview = p.models!.slice(0, 4).map((m) => m.id).join(", ");
			lines.push(`   ${preview}${count > 4 ? `, +${count - 4} more` : ""}`);
		}
		lines.push("");
	}
	lines.push("  ● api key   ◆ oauth   ○ no credentials");
	lines.push("  /provider add <id> <baseUrl> <modelId> [name]   attach a new endpoint");
	lines.push("  /provider remove <id>                           detach one");
	lines.push("  /login <id>                                    set or replace a credential");
	return lines.join("\n").trimEnd();
}

async function addProvider(ctx: ExtensionCommandContext, args: string): Promise<void> {
	// add <id> <baseUrl> <modelId> [displayName]
	const parts = args.split(/\s+/).filter(Boolean);
	if (parts.length < 3) {
		ctx.ui.notify(
			[
				"usage: /provider add <id> <baseUrl> <modelId> [display name]",
				"",
				"  e.g. /provider add myllm https://api.myllm.com/v1 large \"My LLM Large\"",
				"",
				"  Then paste the API key when asked; it is written to auth.json and never shown again.",
			].join("\n"),
			"warning",
		);
		return;
	}
	const [id, baseUrl, modelId, ...rest] = parts;
	if (!/^[a-zA-Z0-9._-]+$/.test(id)) {
		ctx.ui.notify(`Ⓡ "${id}" is not a usable provider id (letters, digits, . _ -).`, "warning");
		return;
	}
	if (!/^https?:\/\//.test(baseUrl)) {
		ctx.ui.notify(
			[
				`Ⓡ "${baseUrl}" is not an http(s) endpoint.`,
				"If the provider id contains a space, quote it:",
				`  /provider add "${id}" ${baseUrl} ${modelId}`,
			].join("\n"),
			"warning",
		);
		return;
	}
	const providers = loadProviders();
	const replacing = Boolean(providers[id]);
	if (replacing) {
		const replace = await ctx.ui.confirm(
			`Provider "${id}" already exists`,
			`${providers[id].baseUrl}\n\nOverwrite it? The old entry is backed up.`,
		);
		if (!replace) return;
	}
	const apiKey = ctx.hasUI
		? await ctx.ui.input(`API key for ${id} (blank to add later via /login ${id})`, "sk-…")
		: undefined;

	const doc = readJson(MODELS_FILE) ?? {};
	const bag = (doc.providers && typeof doc.providers === "object" ? doc.providers : {}) as Json;
	bag[id] = {
		name: rest.join(" ") || id,
		baseUrl,
		api: "openai-completions",
		// The credential goes to auth.json only. Some older entries also inline
		// `apiKey` here; duplicating a secret into a second file doubles the
		// places a leak can come from for no gain.
		models: [
			{
				id: modelId,
				name: rest.join(" ") || modelId,
				reasoning: false,
				input: ["text"],
				contextWindow: 128000,
				maxTokens: 8192,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
			},
		],
	};
	const backup = writeJson(MODELS_FILE, { ...doc, providers: bag });

	if (apiKey) {
		const auth = readJson(AUTH_FILE) ?? {};
		auth[id] = { type: "api_key", key: apiKey };
		writeJson(AUTH_FILE, auth);
	}
	ctx.ui.notify(
		[
			`Ⓡ provider "${id}" ${replacing ? "replaced" : "added"}`,
			"",
			`  endpoint  ${baseUrl}`,
			`  model     ${modelId}`,
			`  auth      ${apiKey ? "key written to auth.json" : `none yet — /login ${id}`}`,
			backup ? `  backup    ${backup}` : "  backup    none (new file)",
			"",
			"  /reload picks it up without restarting, then /models --pick to try it.",
		].join("\n"),
	);
}

function removeProvider(ctx: ExtensionCommandContext, id: string): void {
	const providers = loadProviders();
	if (!providers[id]) {
		ctx.ui.notify(`Ⓡ no provider "${id}" in ${MODELS_FILE}`, "warning");
		return;
	}
	const bag = { ...providers };
	delete bag[id];
	const doc = readJson(MODELS_FILE) ?? {};
	const backup = writeJson(MODELS_FILE, { ...doc, providers: bag });
	ctx.ui.notify(
		[
			`Ⓡ provider "${id}" removed from models.json`,
			"",
			backup ? `  backup  ${backup}` : "  backup  none",
			"  its credential in auth.json was left alone — remove it there if unwanted.",
			"  /reload to apply.",
		].join("\n"),
	);
}

// --- /commands --------------------------------------------------------------------

const OMNIGENT: Array<[string, string]> = [
	["omni run <agent>", "start an omnigent session (the orchestrator plane)"],
	["omni session", "list and manage omnigent sessions"],
	["omni attach", "attach a REPL to a live session"],
	["omni resume", "resume a conversation"],
	["omni config", "omnigent defaults and credentials"],
	["omni doctor", "omnigent maintenance checks"],
	["omni diagnose", "read-only environment snapshot"],
	["omni usage", "usage and cost report"],
	["omni setup", "first-time setup (also enables optional harnesses)"],
	["omni server", "start or manage the background server"],
	["omni start | stop", "run omnigent on this machine, or stop it"],
];

/**
 * Which extension files RLP owns. The install records them in
 * `<agent dir>/rlp-location.json`; without that marker, fall back to the names
 * RLP ships. Anything else in the extensions dir is a third-party extension the
 * user opted into — optional, and never something RLP depends on.
 */
function rlpExtensionFiles(): Set<string> {
	const marker = readJson(join(AGENT_DIR, "rlp-location.json"));
	const listed = marker?.extensions;
	if (Array.isArray(listed)) return new Set(listed.map(String));
	return new Set(["menus.ts", "rlp-commands.ts", "rlp-orchestrate.ts"]);
}

/** Split the extensions dir into RLP's own files and optional third-party ones. */
function extensionFiles(): { rlp: string[]; optional: string[] } {
	const dir = join(AGENT_DIR, "extensions");
	let files: string[] = [];
	try {
		files = readdirSync(dir).filter((f) => f.endsWith(".ts") || f.endsWith(".js"));
	} catch {
		return { rlp: [], optional: [] };
	}
	const owned = rlpExtensionFiles();
	return {
		rlp: files.filter((f) => owned.has(f)),
		optional: files.filter((f) => !owned.has(f)),
	};
}

function skillCommands(): Array<[string, string]> {
	const dirs = [join(AGENT_DIR, "skills")];
	const omnigent = join(homedir(), ".omnigent", "agents");
	try {
		for (const agent of readdirSync(omnigent)) {
			const skills = join(omnigent, agent, "skills");
			if (existsSync(skills)) dirs.push(skills);
		}
	} catch {
		/* no omnigent agents */
	}
	const found: Array<[string, string]> = [];
	for (const dir of dirs) {
		try {
			for (const entry of readdirSync(dir)) {
				const file = join(dir, entry, "SKILL.md");
				if (!existsSync(file)) continue;
				const first = readFileSync(file, "utf8").slice(0, 600);
				const name = first.match(/^name:\s*(.+)$/m)?.[1]?.trim() ?? entry;
				const description = first.match(/^description:\s*(.+)$/m)?.[1]?.trim() ?? "";
				found.push([`/skill:${name}`, description]);
			}
		} catch {
			/* unreadable dir */
		}
	}
	return found;
}

async function renderCommands(ctx: ExtensionCommandContext, filter: string): Promise<string> {
	const { BUILTIN_SLASH_COMMANDS } = await import("@earendil-works/pi-coding-agent");
	const groups: Array<[string, Array<[string, string]>]> = [];
	const wanted = (name: string) => !filter || name.toLowerCase().includes(filter.toLowerCase());

	const byName = new Map(BUILTIN_SLASH_COMMANDS.map((c) => [c.name, c]));
	const bucket = (match: (name: string) => boolean, title: string) => {
		const items: Array<[string, string]> = BUILTIN_SLASH_COMMANDS.filter(
			(c) => match(c.name) && wanted(c.name),
		).map((c): [string, string] => {
			const hint = c.argumentHint ? ` ${c.argumentHint}` : "";
			return [`/${c.name}${hint}`, c.description];
		});
		if (items.length > 0) groups.push([title, items]);
	};

	bucket((n) => ["new", "resume", "tree", "fork", "clone", "compact", "session", "name", "reload", "quit"].includes(n), "Session & history");
	bucket((n) => ["model", "scoped-models", "thinking", "login", "logout", "trust"].includes(n), "Models & auth");
	bucket((n) => ["settings", "hotkeys"].includes(n), "Preferences");
	bucket((n) => ["export", "import", "share", "copy", "changelog", "bug"].includes(n), "Output & sharing");
	bucket((n) => ["orchestration"].includes(n), "RLP ladder (harness)");

	const rlpAll: Array<[string, string]> = [
		["/rlp", "status card and action menu"],
		["/rlp-plan <request>", "headless plan: gate → DAG → routing → waves"],
		["/rlp-triage <request>", "the gate alone, one forward pass"],
		["/rlp-doctor", "is this host runnable?"],
		["/rlp-ladder", "the resolved model ladder"],
		["/rlp-config", "show or edit the ladder (brain, worker arms, gate)"],
		["/rlp-roles", "set the orchestration model per role (role -> model)"],
		["/rlp-run <request>", "compose the orchestrator command"],
		["/commands", "this index"],
		["/models [filter]", "models grouped by provider, marked"],
		["/models --pick", "switch model, or set the default"],
		["/provider", "endpoints and credential state"],
		["/provider add|remove", "attach or detach a provider"],
	];
	const rlp: Array<[string, string]> = rlpAll.filter(([n]) => wanted(n) || wanted("rlp"));
	groups.push(["RLP engine & menus", rlp]);

	const ext = extensionFiles();
	const extItem = (file: string, note: string): [string, string] => [
		`(extension) ${file.replace(/\.(ts|js)$/, "")}`,
		note,
	];
	const rlpExt: Array<[string, string]> = ext.rlp
		.filter((f) => wanted(f) || wanted("extension"))
		.map((f) => extItem(f, "RLP-owned — installed and updated by scripts/install.sh"));
	if (rlpExt.length > 0) groups.push(["RLP extensions (this tool)", rlpExt]);
	const optionalExt: Array<[string, string]> = ext.optional
		.filter((f) => wanted(f) || wanted("extension") || wanted("optional"))
		.map((f) => extItem(f, "optional third-party — not part of RLP, safe to remove"));
	if (optionalExt.length > 0) groups.push(["Other extensions (optional — not part of RLP)", optionalExt]);
	const skills: Array<[string, string]> = skillCommands().filter(([n]) => wanted(n) || wanted("skill"));
	if (skills.length > 0) groups.push(["Skills", skills]);
	const omni: Array<[string, string]> = OMNIGENT.filter(([n]) => wanted(n) || wanted("omni"));
	groups.push(["Omnigent (shell)", omni]);

	const total = groups.reduce((n, [, items]) => n + items.length, 0);
	const lines = [`Ⓡ commands · ${total}${filter ? ` matching "${filter}"` : ""}`, ""];
	for (const [title, items] of groups) {
		lines.push(`  ${title}`);
		for (const [name, description] of items) {
			lines.push(`    ${name.padEnd(26)} ${description}`);
		}
		lines.push("");
	}
	lines.push(`  ${byName.size} harness built-ins live in this harness; /orchestration shows the RLP ladder.`);
	lines.push("  Shell equivalents: rlp plan | rlp doctor | rlp ladder | rpi | omni run");
	return lines.join("\n").trimEnd();
}

// --- registration --------------------------------------------------------------------

export default function harnessMenus(pi: ExtensionAPI): void {
	pi.registerCommand("commands", {
		description: "Everything the slash menu offers, grouped (harness, RLP, skills, omnigent)",
		handler: async (args, ctx) => {
			ctx.ui.notify(await renderCommands(ctx, args.trim()));
		},
	});

	pi.registerCommand("models", {
		description: "Models grouped by provider — authenticated ones by default, marked by role",
		handler: async (args, ctx) => {
			const raw = args.trim();
			if (raw === "--pick" || raw === "-p") {
				return pickModel(ctx, (model) => pi.setModel(model as never));
			}
			const showAll = raw === "--all" || raw.startsWith("--all ");
			const filter = showAll ? raw.slice(5).trim() : raw.replace(/^--/, "");
			ctx.ui.notify(renderModels(ctx, filter, showAll));
		},
	});

	pi.registerCommand("provider", {
		description: "List / add / remove model providers (endpoints + credentials)",
		handler: async (args, ctx) => {
			const raw = args.trim();
			if (!raw) return void ctx.ui.notify(renderProviders());
			const [verb, ...rest] = raw.split(/\s+/);
			if (verb === "add") return addProvider(ctx, rest.join(" "));
			if (verb === "remove" || verb === "rm") {
				if (!rest[0]) return void ctx.ui.notify("usage: /provider remove <id>", "warning");
				return removeProvider(ctx, rest[0]);
			}
			return void ctx.ui.notify(renderProviders());
		},
	});

	pi.registerCommand("rlp-config", {
		description: "Show or edit the RLP orchestration ladder: brain, worker arms, per-role models, gate",
		handler: async (args, ctx) => {
			const raw = args.trim();
			if (!raw) {
				if (ctx.hasUI) return editLadderInteractively(ctx, (model) => pi.setModel(model as never));
				return void ctx.ui.notify(renderLadder());
			}
			if (raw === "show" || raw === "list") return void ctx.ui.notify(renderLadder());
			const dryRun = /(^|\s)--dry-run(\s|$)/.test(raw);
			const clean = raw.replace(/(^|\s)--dry-run(\s|$)/, " ").trim();
			if (!clean) return void ctx.ui.notify(renderLadder());
			const ops = opsFromArgs(clean);
			if (typeof ops === "string") return void ctx.ui.notify(`Ⓡ ${ops}\n\n${renderLadder()}`, "warning");
			const reply = await applyConfig(ctx, ops, dryRun);
			ctx.ui.notify(reply.lines.join("\n"), reply.ok ? "info" : "warning");
		},
	});

	pi.registerCommand("rlp-roles", {
		description: "Set the orchestration model for each role (role -> model), like /model for the ladder",
		handler: async (args, ctx) => {
			const raw = args.trim();
			const view = await fetchLadderView(ctx);
			if (!view) {
				ctx.ui.notify(`Ⓡ no orchestration ladder at ${ladderPath()}`, "warning");
				return;
			}
			if (!raw || raw === "show" || raw === "list") {
				if (ctx.hasUI && !raw) return editRolesInteractively(ctx, view);
				return void ctx.ui.notify(renderRoles(view));
			}
			if (raw === "--pick" || raw === "-p") {
				if (!ctx.hasUI) return void ctx.ui.notify(renderRoles(view));
				return editRolesInteractively(ctx, view);
			}
			const [role, value] = raw.split(/\s+/);
			if (!role || !value) {
				ctx.ui.notify(`Ⓡ usage: /rlp-roles <role> <provider/model[,provider/model…]> | <role> off | --pick\n\n${renderRoles(view)}`, "warning");
				return;
			}
			if (value === "off") {
				const reply = await applyConfig(ctx, [{ op: "clear_role", role }]);
				ctx.ui.notify(reply.lines.join("\n"), reply.ok ? "info" : "warning");
				return;
			}
			const refs = value.split(",").map((v) => v.trim()).filter(Boolean);
			const ops = await bindRoleChainOps(ctx, view, role, refs);
			if (typeof ops === "string") {
				if (ops !== "cancelled") ctx.ui.notify(`Ⓡ ${ops}`, "warning");
				return;
			}
			const reply = await applyConfig(ctx, ops);
			ctx.ui.notify([...reply.lines, "", `  ${role} → ${refs.join(" > ")}`].join("\n"), reply.ok ? "info" : "warning");
		},
	});
}
