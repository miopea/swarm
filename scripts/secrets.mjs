#!/usr/bin/env node

import { execFile as execFileCallback } from "node:child_process";
import { constants } from "node:fs";
import {
  access,
  chmod,
  readFile,
  rename,
  stat,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

const execFile = promisify(execFileCallback);
const DEFAULT_MANIFEST = "config/secrets.manifest.json";

function unquote(value) {
  if (
    value.length >= 2 &&
    ((value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'")))
  ) {
    const inner = value.slice(1, -1);
    return value.startsWith('"') ? JSON.parse(value) : inner;
  }
  return value;
}

export function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (match) values[match[1]] = unquote(match[2].trim());
  }
  return values;
}

function encodeEnvValue(value) {
  if (value === "") return '""';
  if (/[\r\n#"'`\s]/u.test(value)) {
    return `"${value
      .replaceAll("\\", "\\\\")
      .replaceAll('"', '\\"')
      .replaceAll("\r", "\\r")
      .replaceAll("\n", "\\n")}"`;
  }
  return value;
}

export function updateEnvText(text, updates) {
  const remaining = new Map(updates);
  const lines = text.replace(/\s*$/, "").split(/\r?\n/);
  const output = lines.length === 1 && lines[0] === "" ? [] : lines;

  for (let index = 0; index < output.length; index += 1) {
    const match = output[index].match(
      /^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)=/,
    );
    if (!match || !remaining.has(match[2])) continue;
    output[index] = `${match[1]}${match[2]}=${encodeEnvValue(
      remaining.get(match[2]),
    )}`;
    remaining.delete(match[2]);
  }

  for (const [name, value] of remaining) {
    output.push(`${name}=${encodeEnvValue(value)}`);
  }
  return `${output.join("\n")}\n`;
}

export function validateSecret(name, value, validator = "nonempty") {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${name} is empty`);
  }
  if (validator === "nonempty") return;
  if (validator === "json") {
    JSON.parse(value);
    return;
  }
  if (validator === "pem") {
    if (!value.replaceAll("\\n", "\n").includes("-----BEGIN PRIVATE KEY-----")) {
      throw new Error(`${name} is not a recognizable private key`);
    }
    return;
  }
  if (validator === "url") {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol)) {
      throw new Error(`${name} must use HTTP or HTTPS`);
    }
    return;
  }
  throw new Error(`${name} uses unknown validator ${validator}`);
}

async function git(repository, args) {
  return execFile("git", args, { cwd: repository });
}

export async function assertSafeTarget(repository, relativeTarget) {
  const target = path.resolve(repository, relativeTarget);
  const relative = path.relative(repository, target);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`${relativeTarget} resolves outside the repository`);
  }
  try {
    await git(repository, ["ls-files", "--error-unmatch", "--", relativeTarget]);
    throw new Error(`${relativeTarget} is tracked by Git; refusing to write secrets`);
  } catch (error) {
    if (error.message?.includes("refusing to write secrets")) throw error;
  }
  try {
    await git(repository, ["check-ignore", "--quiet", "--", relativeTarget]);
  } catch {
    throw new Error(`${relativeTarget} is not ignored by Git`);
  }
  return target;
}

async function readManifest(repository, manifestPath) {
  const absolute = path.resolve(repository, manifestPath);
  const manifest = JSON.parse(await readFile(absolute, "utf8"));
  if (manifest.version !== 1 || typeof manifest.vault !== "string") {
    throw new Error("Manifest must have version 1 and a vault name");
  }
  if (!Array.isArray(manifest.targets)) {
    throw new Error("Manifest targets must be an array");
  }
  return manifest;
}

async function fetchSecret(vault, name) {
  const { stdout } = await execFile("az", [
    "keyvault",
    "secret",
    "show",
    "--vault-name",
    vault,
    "--name",
    name,
    "--query",
    "value",
    "-o",
    "tsv",
    "--only-show-errors",
  ]);
  return stdout.replace(/\r?\n$/, "");
}

async function readOptional(file) {
  try {
    return await readFile(file, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return "";
    throw error;
  }
}

async function check(repository, manifest) {
  let missing = 0;
  for (const target of manifest.targets) {
    const absolute = await assertSafeTarget(repository, target.path);
    const values = parseEnv(await readOptional(absolute));
    for (const secret of target.secrets) {
      try {
        validateSecret(secret.env, values[secret.env], secret.validate);
      } catch {
        console.error(`MISSING ${target.path}: ${secret.env}`);
        missing += 1;
      }
    }
  }
  if (missing > 0) throw new Error(`${missing} required secret(s) are unavailable`);
  const count = manifest.targets.reduce(
    (total, target) => total + target.secrets.length,
    0,
  );
  console.log(`PASS: ${count} required secret(s) are present and structurally valid`);
}

async function pull(repository, manifest) {
  await execFile("az", ["account", "show", "--only-show-errors"]);
  let count = 0;
  for (const target of manifest.targets) {
    const absolute = await assertSafeTarget(repository, target.path);
    const entries = await Promise.all(
      target.secrets.map(async (secret) => {
        const value = await fetchSecret(manifest.vault, secret.secret);
        validateSecret(secret.env, value, secret.validate);
        return [secret.env, value];
      }),
    );
    const current = await readOptional(absolute);
    const next = updateEnvText(current, new Map(entries));
    const temporary = `${absolute}.secrets-tmp-${process.pid}`;
    const existingMode = await stat(absolute).then(
      (value) => value.mode,
      () => 0o600,
    );
    await writeFile(temporary, next, { mode: 0o600 });
    await chmod(temporary, existingMode & 0o777);
    await rename(temporary, absolute);
    count += entries.length;
    console.log(`UPDATED ${target.path}: ${entries.length} secret(s)`);
  }
  console.log(`PASS: pulled ${count} secret(s) from ${manifest.vault}`);
}

async function main() {
  const command = process.argv[2];
  const manifestIndex = process.argv.indexOf("--manifest");
  const manifestPath =
    manifestIndex >= 0 ? process.argv[manifestIndex + 1] : DEFAULT_MANIFEST;
  if (!["check", "pull"].includes(command) || !manifestPath) {
    console.error(
      "Usage: node scripts/secrets.mjs <check|pull> [--manifest path]",
    );
    process.exitCode = 2;
    return;
  }
  const repository = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "..",
  );
  await access(repository, constants.R_OK);
  const manifest = await readManifest(repository, manifestPath);
  if (command === "check") await check(repository, manifest);
  else await pull(repository, manifest);
}

if (path.resolve(process.argv[1] ?? "") === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(`ERROR: ${error.message}`);
    process.exitCode = 1;
  });
}
