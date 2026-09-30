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
 *   5. `/direct on` and `/direct off` move the ladder's gate, and `/direct`
 *      reports which mode is in effect and what put it there;
 *   6. `/setup` runs the whole wizard with every dialog cancelled and reaches its
 *      summary — and writes nothing, because nothing was chosen;
 *   7. `/setup` offers the providers a sandboxed fake pi store has connected:
 *      cancelling that offer writes nothing, accepting it copies the endpoints
 *      and credentials into RLP's store verbatim, pi's own files byte-for-byte
 *      unchanged, and no credential value ever reaches the session.
 *
 * It talks to no model and needs no credential: the endpoint it attaches is
 * `http://127.0.0.1:9` (nothing listens there), and all four pieces of state —
 * models.json, auth.json, the ladder and pi's own store — are redirected into a
 * temporary directory, so a run can never touch the user's real credentials or
 * reset their model choices. The pi store in particular: `provider scan` would
 * otherwise read the developer's real `~/.pi`, and a check that offers the
 * user's live credentials on screen is not a check. The ladder *is* written
 * here, because `/direct` is a write path and asserting on a copy is the only
 * way to prove it without taking somebody's configuration away from them.
 */
import { execFileSync, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const binary = process.argv[2] || "rlp";
const STEP_TIMEOUT_MS = 120_000;
const SANDBOX = mkdtempSync(join(tmpdir(), "rlp-provider-check-"));

// The session must run *this checkout's* extensions, not whatever the last
// install dropped in ~/.rlp/agent: a wizard check that quietly exercises a
// stale copy cannot see the step under review — it just watches the old wizard
// pass. So build a throwaway agent dir from the checkout, exactly as
// scripts/check-first-ask does, and point the session (and the engine's
// default paths) at it. `RLP_NO_MIGRATE=1` keeps the developer's real ~/.pi
// out of the sandbox; the credential stores start empty and stay empty unless
// a wizard step writes them.
const REPO = resolve(import.meta.dirname, "..");
const AGENT = join(SANDBOX, "agent");
execFileSync("sh", [join(REPO, "scripts/sync-agent-dir"), REPO], {
	env: { ...process.env, RLP_CODING_AGENT_DIR: AGENT, RLP_NO_MIGRATE: "1", RLP_QUIET: "1" },
});
for (const store of ["models.json", "auth.json"]) {
	writeFileSync(join(AGENT, store), "{}\n");
	chmodSync(join(AGENT, store), store === "auth.json" ? 0o600 : 0o644);
}
const MODELS = join(SANDBOX, "models.json");
const AUTH = join(SANDBOX, "auth.json");
// The ladder is copied, not read in place: `/direct` writes it, and a check that
// exercised a write path against the user's own file would leave them with
// someone else's mode afterwards. `RLP_ORCHESTRATION` is the engine's own
// override, so this is the same file every part of the session reads; failing
// that, the ladder this checkout ships (synced into the throwaway agent dir
// above) — the wizard is judged as configured by the source under review, not
// by whatever the host's install last touched.
const LADDER = join(SANDBOX, "orchestration.json");
const REAL_LADDER = process.env.RLP_ORCHESTRATION
	? resolve(process.env.RLP_ORCHESTRATION)
	: join(AGENT, "orchestration.json");
copyFileSync(REAL_LADDER, LADDER);
const PROVIDER = "rlp-check-demo";
const SECRET = "sk-check-secret-value";
// pi's own store, faked. `provider scan` reads `$RLP_PI_AGENT_DIR` (else
// `~/.pi/agent`); pointing it here means the wizard's pi step is exercised
// against this file, not the developer's live logins. One custom endpoint with
// a key and one oauth credential for a builtin cover the two row kinds.
const PI = join(SANDBOX, "pi");
const PI_CUSTOM = "check-pi-vllm";
const PI_BUILTIN = "anthropic";
const PI_SECRET = "sk-pi-store-secret";
const PI_OAUTH = "pi-oauth-access-token";
mkdirSync(PI);
writeFileSync(
	join(PI, "models.json"),
	JSON.stringify({
		providers: {
			[PI_CUSTOM]: {
				name: PI_CUSTOM,
				baseUrl: "http://127.0.0.1:9/v1",
				api: "openai-completions",
				models: [{ id: "pi-model-a" }, { id: "pi-model-b" }],
				piOnlyField: { preserved: true },
			},
		},
	}),
);
writeFileSync(join(PI, "auth.json"), JSON.stringify({ [PI_CUSTOM]: { type: "key", key: PI_SECRET }, [PI_BUILTIN]: { type: "oauth", access: PI_OAUTH, refresh: "keep-me" } }));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function digest(path) {
	if (!existsSync(path)) return "absent";
	return createHash("sha256").update(readFileSync(path)).digest("hex").slice(0, 16);
}

const child = spawn(binary, ["--mode", "rpc", "--no-session"], {
	stdio: ["pipe", "pipe", "pipe"],
	env: {
		...process.env,
		RLP_CODING_AGENT_DIR: AGENT,
		RLP_PI_MODELS: MODELS,
		RLP_PI_AUTH: AUTH,
		RLP_ORCHESTRATION: LADDER,
		RLP_PI_AGENT_DIR: PI,
	},
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
 * is to cancel, which is the path a hesitant user takes. It also receives what
 * the step has seen so far — several wizard steps share one dialog title
 * ("Select (empty = none)" is every multiSelect), so the notify that preceded
 * a question is sometimes the only way to know which step is asking.
 */
async function step(label, prompt, expect, dialog = () => ({ cancelled: true })) {
	const notifies = [];
	const dialogs = [];
	onNotify = (message) => notifies.push(message);
	onDialog = (request) => {
		dialogs.push(`${request.method}: ${request.title ?? ""}`);
		answer(request.id, dialog(request, { dialogs, notifies }) ?? { cancelled: true });
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

	// --- 7. the mode switch, which is a write path into the ladder
	const ladderBefore = digest(LADDER);
	const gateOf = () => {
		try {
			return JSON.parse(readFileSync(LADDER, "utf8"))?.routing?.gate ?? "(absent)";
		} catch {
			return "(unreadable)";
		}
	};
	const gateAtStart = gateOf();
	const onDirect = await step(
		"/direct on",
		"/direct on",
		(m) => m.includes("mode is now"),
	);
	record(onDirect.matched.includes("direct-only"), `on says what it turned off: ${onDirect.matched.split("\n")[0]}`);
	record(gateOf() === "direct", `the ladder on disk really says direct (was ${gateAtStart})`);
	console.log("  ok  /direct on writes routing.gate=direct through the engine");

	const status = await step("/direct (status)", "/direct", (m) => m.includes("mode ·"));
	record(status.matched.includes("direct-only"), "the status line names the mode in effect");
	record(status.matched.includes("$RLP_DIRECT") || status.matched.includes("routing.gate"), "and what put it there");
	record(
		status.matched.includes("/direct off") || status.matched.includes("rlp mode"),
		"and the way back, without being asked for",
	);
	console.log("  ok  /direct reports the effective mode, its source, and the way out");

	const confused = await step("/direct <nonsense>", "/direct sideways", (m) => m.includes("usage: /direct"));
	record(
		confused.matched.includes("on") && confused.matched.includes("off"),
		"a wrong argument names the right ones",
	);

	const off = await step(
		"/direct off",
		"/direct off",
		(m) => m.includes("mode is now"),
	);
	record(off.matched.includes("full"), "off hands the decision back to the gate");
	record(gateOf() === "hybrid", `and the ladder says hybrid again (got ${gateOf()})`);
	// Whatever this host's ladder said before, the round trip ends where it
	// started — a switch that only ever turns one way is not a switch.
	if (gateAtStart === "hybrid" || gateAtStart === "direct") {
		record(gateOf() === "hybrid", "the round trip leaves the default gate");
	}
	console.log("  ok  /direct on|off round-trips through the validated config path");

	// --- 8. the whole wizard, every dialog cancelled. The digest is taken here,
	// after the mode round trip above, because that round trip is allowed to
	// write and this one is not.
	const before = digest(LADDER);
	const storesBefore = [digest(MODELS), digest(AUTH)];
	const piBefore = [digest(join(PI, "models.json")), digest(join(PI, "auth.json"))];
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
	// How many questions the wizard can ask depends on whether this host has a
	// credentialed provider to choose models from, so the assertion is on the
	// shape of each branch rather than on a fixed count. What must hold either
	// way: the endpoints step is always offered, and a skipped step is *said*.
	// The questions are numbered out of five now, and the first one is the mode:
	// what this host does with a request. Everything after it depends on the
	// answer, so a wizard that cancelled with no credentialed provider must not
	// offer the model steps as empty pickers.
	// The arms step prints its numbered list as a message and asks one input, so
	// "what the wizard showed" is dialogs *and* notifies — judging the dialogs
	// alone made a step that ran look like a step that was skipped.
	const asked = [...setup.dialogs, ...setup.notifies].join(" | ");
	const skippedModelSteps = setup.notifies.some((m) => m.includes("no provider has a credential"));
	record(
		asked.includes("Step 1 of 5"),
		`the wizard asks what RLP should do (saw ${setup.dialogs.length}: ${asked.slice(0, 220)})`,
	);
	record(asked.includes("Step 2 of 5"), "and then offers the endpoints step");
	if (skippedModelSteps) {
		record(
			!setup.dialogs.join(" | ").includes("Step 3 of 5") && !asked.includes("Step 4 of 5"),
			"with no credentialed provider, the model steps are not offered as empty pickers",
		);
		record(
			setup.notifies.some((m) => m.includes("/provider connect")),
			"and the skip names the command that unblocks it",
		);
	} else {
		record(
			asked.includes("Step 3 of 5") && asked.includes("Step 4 of 5"),
			`with a credentialed provider, the model and arm steps are offered: ${asked.slice(0, 220)}`,
		);
	}
	record(setup.matched.includes("reload"), "the summary says how to pick the changes up");
	record(
		digest(LADDER) === before,
		"cancelling every step wrote nothing to the ladder",
	);
	// The pi step is detection-before-asking, and its input is one of the
	// dialogs the blanket cancel answers — so what must show here is that it
	// was *offered* (the multiSelect prints its list as a notify) and that
	// the cancel wrote nothing to either store.
	record(
		asked.includes("providers pi already has connected"),
		"the wizard offers what pi already has connected, before the endpoint questions",
	);
	record(
		digest(MODELS) === storesBefore[0] && digest(AUTH) === storesBefore[1],
		"cancelling the pi offer wrote nothing to RLP's stores",
	);
	console.log(`  ok  /setup walks all ${setup.dialogs.length} steps and writes nothing when cancelled`);

	// --- 9. the pi offer accepted: the copy. The same wizard, but this
	// handler answers the pi step's batch with "all" and its confirmation with
	// the yes option, cancelling everything else. The expected notify is the
	// import report itself — after it the wizard stalls harmlessly on an
	// unanswered step (nothing answers it, and the child dies next).
	let answeredPi = false;
	const ladderBeforeCopy = digest(LADDER);
	const accept = await step(
		"/setup (pi's providers, accepted)",
		"/setup",
		(m) => m.includes("copied into RLP"),
		(request, seen) => {
			if (request.method === "input" && (request.title ?? "").startsWith("Select (empty")) {
				// Two wizard steps share this input title (tools, then
				// providers); only the one preceded by the pi notify is
				// answered, and only once — a later same-titled input
				// (the arms step) must not get "all" meant for pi.
				if (answeredPi || !seen.notifies.some((m) => m.includes("providers pi already has connected"))) {
					return { cancelled: true };
				}
				answeredPi = true;
				return { value: "all" };
			}
			if (request.method === "select" && (request.title ?? "").startsWith("Copy ")) {
				const yes = (request.options ?? []).find((o) => o.startsWith("Copy"));
				return yes ? { value: yes } : { cancelled: true };
			}
			return { cancelled: true };
		},
	);
	record(
		accept.matched.includes(PI_CUSTOM) && accept.matched.includes(PI_BUILTIN),
		`the copy report names both providers: ${accept.matched.split("\n")[0]}`,
	);
	const piDoc = JSON.parse(readFileSync(join(PI, "models.json"), "utf8"));
	const rlpModels = existsSync(MODELS) ? JSON.parse(readFileSync(MODELS, "utf8")) : {};
	record(
		JSON.stringify(rlpModels?.providers?.[PI_CUSTOM]) === JSON.stringify(piDoc.providers[PI_CUSTOM]),
		"the endpoint crossed over verbatim — pi's unknown fields and all",
	);
	record(!rlpModels?.providers?.[PI_BUILTIN], "a builtin credential did not fabricate a models.json entry");
	const rlpAuth = existsSync(AUTH) ? JSON.parse(readFileSync(AUTH, "utf8")) : {};
	record(rlpAuth?.[PI_CUSTOM]?.key === PI_SECRET, "the custom key crossed into RLP's auth.json");
	record(
		rlpAuth?.[PI_BUILTIN]?.access === PI_OAUTH && rlpAuth?.[PI_BUILTIN]?.refresh === "keep-me",
		"the oauth entry crossed with its refresh fields intact",
	);
	record((statSync(AUTH).mode & 0o777) === 0o600, "the copied credential store is 0600");
	record(
		digest(join(PI, "models.json")) === piBefore[0] && digest(join(PI, "auth.json")) === piBefore[1],
		"pi's own files were never touched — the copy is one-way",
	);
	record(digest(LADDER) === ladderBeforeCopy, "copying providers wrote nothing to the ladder");
	const sessionText = [...accept.notifies, ...accept.dialogs].join(" | ");
	record(
		!sessionText.includes(PI_SECRET) && !sessionText.includes(PI_OAUTH) && !sessionText.includes("sk-"),
		"no credential value reached the session",
	);
	console.log("  ok  /setup copies pi's providers into RLP verbatim, 0600, one-way, nothing echoed");

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