#!/usr/bin/env node
/**
 * End-to-end check for the provider / setup surface, driven through the real
 * harness over its RPC protocol.
 *
 * `scripts/check-harness.mjs` proves the commands *load*; this proves they
 * *work*: it types the slash commands into a live session, answers the dialogs
 * the way a person would (including cancelling), and asserts on what the
 * session reported. That is the only way to test this class of code — the
 * provider wizard is a conversation, and stubbing the conversation tests the
 * stub.
 *
 *   node scripts/check-provider.mjs [rlp-binary]
 *
 * Asserts, in order:
 *   1. `/provider list` renders endpoints from the engine, with credential
 *      state and the ladder arms each endpoint carries;
 *   2. `/provider add …` really writes: the endpoint lands in models.json, the
 *      credential lands in auth.json at 0600, and the credential is never echoed
 *      back into the session;
 *   3. `/provider test <id>` classifies the failure and never raises;
 *   4. `/provider remove <id>` takes it back out, credential included;
 *   5. `/setup` runs the whole wizard with every dialog cancelled and reaches
 *      its summary — and writes nothing, because nothing was chosen.
 *
 * It talks to no model and needs no credential: the endpoint it attaches is
 * `http://127.0.0.1:9` (nothing listens there), and both stores are redirected
 * to a temporary directory, so a run can never touch the user's real
 * credentials. The ladder is read but never written.
 */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";

const binary = process.argv[2] || "rlp";
const AGENT_DIR = process.env.RPI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
const LADDER = join(AGENT_DIR, "orchestration.json");
const STEP_TIMEOUT_MS = 120_000;
const SANDBOX = mkdtempSync(join(tmpdir(), "rlp-provider-check-"));
const MODELS = join(SANDBOX, "models.json");
const AUTH = join(SANDBOX, "auth.json");
const PROVIDER = "rlp-check-demo";
const SECRET = "sk-check-secret-value";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function digest(path) {
	if (!existsSync(path)) return "absent";
	return createHash("sha256").update(readFileSync(path)).digest("hex").slice(0, 16);
}

const child = spawn(binary, ["--mode", "rpc", "--no-session"], {
	stdio: ["pipe", "pipe", "pipe"],
	env: { ...process.env, RLP_PI_MODELS: MODELS, RLP_PI_AUTH: AUTH },
});
let buffer = "";
let onNotify = null;
let onDialog = null;
let nextId = 1;

child.stdout.on("data", (chunk) => {
	buffer += String(chunk);
	const lines = buffer.split("\n");
	buffer = lines.pop() ?? "";
	for (const line of lines) {
		if (!line.trim()) continue;
		let msg;
		try {
			msg = JSON.parse(line);
		} catch {
			continue;
		}
		if (msg.type !== "extension_ui_request") continue;
		if (msg.method === "notify") {
			onNotify?.(String(msg.message ?? ""));
			continue;
		}
		if (["select", "confirm", "input", "editor"].includes(msg.method)) {
			onDialog?.(msg);
		}
		// setStatus / setWidget / setTitle / set_editor_text: no answer expected.
	}
});
child.stderr.on("data", () => {
	/* the harness reports in-band; stderr is noise */
});

function answer(id, payload) {
	child.stdin.write(`${JSON.stringify({ type: "extension_ui_response", id, ...payload })}\n`);
}

/**
 * Type a slash command and wait for a notify that satisfies `expect`.
 * `dialog` answers every dialog that appears while the step runs; the default
 * is to cancel, which is the path a hesitant user takes.
 */
async function step(label, prompt, expect, dialog = () => ({ cancelled: true })) {
	const notifies = [];
	const dialogs = [];
	onNotify = (message) => notifies.push(message);
	onDialog = (request) => {
		dialogs.push(`${request.method}: ${request.title ?? ""}`);
		answer(request.id, dialog(request) ?? { cancelled: true });
	};
	child.stdin.write(`${JSON.stringify({ id: nextId++, type: "prompt", message: prompt })}\n`);

	const deadline = Date.now() + STEP_TIMEOUT_MS;
	let matched = null;
	while (Date.now() < deadline) {
		matched = notifies.find((m) => expect(m));
		if (matched) break;
		await sleep(100);
	}
	onNotify = null;
	onDialog = null;
	if (!matched) {
		const seen = notifies.length > 0 ? notifies.map((m) => `      ${m.split("\n")[0].slice(0, 120)}`).join("\n") : "      (nothing)";
		throw new Error(`step failed: ${label}\n    looked for: ${expect}\n    saw:\n${seen}`);
	}
	return { matched, notifies, dialogs };
}

const failures = [];
const record = (ok, message) => {
	if (!ok) failures.push(message);
	return ok;
};

try {
	await sleep(1500); // let the extensions load before the first prompt

	// --- 1. an empty store explains itself
	const empty = await step("/provider list (empty store)", "/provider list", (m) => m.includes("endpoints ·"));
	record(
		empty.matched.includes("no endpoints configured"),
		"an empty store says so rather than rendering an empty list",
	);
	record(
		empty.matched.includes("/provider connect") || empty.matched.includes("/setup"),
		"an empty store says how to attach one",
	);
	console.log("  ok  /provider list explains an empty store");

	// --- 2. a real attach, into the sandbox
	const add = await step(
		"/provider add <id> <url> <model>",
		`/provider add ${PROVIDER} http://127.0.0.1:9/v1 check-model`,
		(m) => m.includes("connected") || m.includes("could not attach"),
		(request) => (request.method === "input" ? { value: SECRET } : { cancelled: true }),
	);
	record(add.matched.includes("connected"), `the attach reported success: ${add.matched.split("\n")[0]}`);
	record(!add.matched.includes(SECRET), "the credential was not echoed back into the session");
	const modelsDoc = existsSync(MODELS) ? JSON.parse(readFileSync(MODELS, "utf8")) : {};
	record(Boolean(modelsDoc?.providers?.[PROVIDER]), "the endpoint was written to models.json");
	record(
		modelsDoc?.providers?.[PROVIDER]?.baseUrl === "http://127.0.0.1:9/v1",
		"the endpoint kept its URL (trailing slash normalised)",
	);
	record(!JSON.stringify(modelsDoc).includes(SECRET), "models.json holds no secret");
	const authDoc = existsSync(AUTH) ? JSON.parse(readFileSync(AUTH, "utf8")) : {};
	record(authDoc?.[PROVIDER]?.key === SECRET, "the credential was written to auth.json");
	record(
		existsSync(AUTH) && (statSync(AUTH).mode & 0o777) === 0o600,
		`auth.json is 0600 (got ${existsSync(AUTH) ? (statSync(AUTH).mode & 0o777).toString(8) : "nothing"})`,
	);
	console.log("  ok  /provider add writes through the engine, key at 0600, nothing echoed");

	// --- 3. the endpoint report, now that there is one
	const listed = await step("/provider list (after the attach)", "/provider list", (m) =>
		m.includes(PROVIDER),
	);
	record(/(●|◆|○)\s/.test(listed.matched), "the report shows a credential badge (● api key / ◆ oauth / ○ none)");
	record(listed.matched.includes("RLP arms"), "the report says which endpoints the orchestrator uses");
	record(!listed.matched.includes(SECRET) && !listed.matched.includes("sk-"), "the report never prints a credential");
	console.log("  ok  /provider list shows the new endpoint's state and ladder standing");

	// --- 4. a classified failure, not a stack
	const missing = await step(
		"/provider test <unreachable>",
		`/provider test ${PROVIDER}`,
		(m) => m.includes("did not answer"),
	);
	record(
		missing.matched.includes("network") || missing.matched.includes("not_found"),
		`nothing is listening, and that is what it says: ${missing.matched.split("\n").find((l) => l.includes("what")) ?? ""}`,
	);
	record(missing.matched.includes("fix"), "the failure carries a fix line");

	const unknown = await step(
		"/provider test <unknown>",
		"/provider test definitely-not-a-provider",
		(m) => m.includes("did not answer"),
	);
	record(
		unknown.matched.includes("not_found") || unknown.matched.includes("no endpoint"),
		"an unknown provider is reported as not_found",
	);
	console.log("  ok  /provider test classifies failures instead of raising");

	// --- 5. an unknown verb teaches the verbs
	const nonsense = await step(
		"/provider <nonsense>",
		"/provider frobnicate",
		(m) => m.includes("unknown provider verb") || m.includes("endpoints ·"),
	);
	record(nonsense.matched.includes("unknown provider verb"), "an unknown verb names the real ones");
	console.log("  ok  /provider <nonsense> says what the verbs are");

	// --- 6. detach, credential included
	const removed = await step(
		"/provider remove <id>",
		`/provider remove ${PROVIDER}`,
		(m) => m.includes("removed") || m.includes("could not remove"),
		(request) => (request.method === "confirm" ? { confirmed: true } : { cancelled: true }),
	);
	record(removed.matched.includes("removed"), `the detach reported success: ${removed.matched.split("\n")[0]}`);
	const afterRemoval = existsSync(MODELS) ? JSON.parse(readFileSync(MODELS, "utf8")) : {};
	record(!afterRemoval?.providers?.[PROVIDER], "the endpoint is gone from models.json");
	record(
		!existsSync(AUTH) || !JSON.parse(readFileSync(AUTH, "utf8"))[PROVIDER],
		"the credential is gone from auth.json",
	);
	console.log("  ok  /provider remove detaches the endpoint and its credential");

	// --- 7. the slash index, which is what proves the TUI resolved RLP's own dir
	const index = await step("/commands", "/commands", (m) => m.includes("commands ·"));
	record(
		index.matched.includes("RLP extensions (this tool)") && index.matched.includes("rlp-provider"),
		"the index finds RLP's extensions in RLP's own agent dir",
	);
	record(
		!/optional|third-party/i.test(index.matched.split("RLP extensions")[0] ?? ""),
		"nothing outside RLP's dir is implied as a dependency",
	);
	const skillLines = index.matched.split("\n").filter((l) => l.includes("/skill:rlp-"));
	record(
		skillLines.length === new Set(skillLines.map((l) => l.trim())).size,
		`each skill is listed once (saw ${skillLines.length} line(s) for RLP skills)`,
	);
	record(skillLines.length >= 5, `RLP's own skills are installed and indexed: ${skillLines.length}`);
	console.log(`  ok  /commands indexes RLP's own extensions and ${skillLines.length} skills, once each`);

	// --- 8. the whole wizard, every dialog cancelled
	const before = digest(LADDER);
	const setup = await step(
		"/setup (all dialogs cancelled)",
		"/setup",
		(m) => m.includes("setup done"),
		() => ({ cancelled: true }),
	);
	record(setup.notifies.some((m) => m.includes("RLP setup")), "/setup announces itself with the doctor verdict");
	record(
		setup.notifies.some((m) => m.includes("runnable") || m.includes("problems found")),
		"/setup reports this host's health first",
	);
	record(
		setup.dialogs.length >= 3,
		`the wizard asked its questions (saw ${setup.dialogs.length}: ${setup.dialogs.join(" | ").slice(0, 200)})`,
	);
	record(setup.matched.includes("reload"), "the summary says how to pick the changes up");
	record(
		digest(LADDER) === before,
		"cancelling every step wrote nothing to the ladder",
	);
	console.log(`  ok  /setup walks all ${setup.dialogs.length} steps and writes nothing when cancelled`);

	child.kill("SIGTERM");
} catch (e) {
	failures.push(String(e.message ?? e));
	child.kill("SIGKILL");
}

await sleep(300);
rmSync(SANDBOX, { recursive: true, force: true });

if (failures.length > 0) {
	console.error("provider check FAILED:");
	for (const f of failures) console.error(`  - ${f}`);
	process.exit(1);
}
console.log("  provider/setup check ok: attach, list, classify, detach, guide, cancel cleanly");
process.exit(0);