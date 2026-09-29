#!/usr/bin/env node
/**
 * Contract check for what the harness actually advertises.
 *
 * The static typecheck proves the extensions use the API correctly. It cannot
 * see that two of them registered the same command name — which is exactly the
 * failure that shipped `/rlp-run:1` and `/rlp-run:2` into the menu, where
 * neither invocation looked like the command you typed. That class of bug is
 * only visible from the running harness, so it is checked there.
 *
 * Asserts, over the live command list:
 *   - every expected RLP command is present under its real name
 *   - no name is registered twice (the loader disambiguates duplicates with :1/:2)
 *   - no stray :N suffixed duplicates of our own commands
 *   - the RLP slash commands load at all
 *
 *   node scripts/check-harness.mjs [rlp-binary]
 */
import { spawn } from "node:child_process";

const binary = process.argv[2] || "rlp";
const EXPECTED = ["rlp", "rlp-plan", "rlp-triage", "rlp-doctor", "rlp-ladder", "rlp-config", "rlp-roles", "rlp-run", "commands", "models", "provider"];

const child = spawn(binary, ["--mode", "rpc", "--no-session"], { stdio: ["pipe", "pipe", "pipe"] });
let buffer = "";
let commands = null;

const done = new Promise((resolve, reject) => {
	const timer = setTimeout(() => {
		child.kill("SIGKILL");
		reject(new Error("timed out waiting for get_commands"));
	}, 120_000);
	child.stdout.on("data", (chunk) => {
		buffer += String(chunk);
		for (const line of buffer.split("\n")) {
			let msg;
			try {
				msg = JSON.parse(line);
			} catch {
				continue;
			}
			if (msg.id === 1) {
				commands = msg.data?.commands ?? [];
				clearTimeout(timer);
				child.kill("SIGTERM");
				resolve(commands);
			}
		}
	});
	child.on("error", reject);
});

child.stdin.write(`${JSON.stringify({ id: 1, type: "get_commands" })}\n`);

const failures = [];
try {
	await done;
} catch (e) {
	console.error(`harness check failed: ${e.message}`);
	process.exit(1);
}

const names = commands.map((c) => c.name);
for (const want of EXPECTED) {
	if (!names.includes(want)) failures.push(`missing command /${want}`);
}

const seen = new Map();
for (const name of names) {
	if (name.startsWith("skill:")) continue;
	seen.set(name, (seen.get(name) ?? 0) + 1);
}
for (const [name, count] of seen) {
	if (count > 1) failures.push(`/${name} is registered ${count} times`);
}

const suffixed = names.filter((n) => /:\d+$/.test(n) && !n.startsWith("skill:"));
if (suffixed.length > 0) {
	failures.push(`commands the loader had to disambiguate (duplicate registration): ${suffixed.join(", ")}`);
}

if (failures.length > 0) {
	console.error("harness contract FAILED:");
	for (const f of failures) console.error(`  - ${f}`);
	process.exit(1);
}

console.log(
	`  harness contract ok: ${commands.length} commands, no duplicates, all ${EXPECTED.length} RLP commands present`,
);
process.exit(0);
