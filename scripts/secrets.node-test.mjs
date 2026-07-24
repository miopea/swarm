import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  assertSafeTarget,
  parseEnv,
  updateEnvText,
  validateSecret,
} from "./secrets.mjs";

test("parseEnv reads assignments without exposing comments as values", () => {
  assert.deepEqual(parseEnv("# comment\nONE=first\nTWO=\"second value\"\n"), {
    ONE: "first",
    TWO: "second value",
  });
});

test("updateEnvText preserves unrelated entries and replaces mapped values", () => {
  const result = updateEnvText(
    "EXISTING=keep\nTOKEN=old\n",
    new Map([
      ["TOKEN", "new"],
      ["MULTILINE", "line one\nline two"],
    ]),
  );

  assert.equal(
    result,
    'EXISTING=keep\nTOKEN=new\nMULTILINE="line one\\nline two"\n',
  );
});

test("JSON values round-trip through environment serialization", () => {
  const source = '{"type":"service_account","private_key":"line one\\nline two"}';
  const rendered = updateEnvText("", new Map([["SERVICE_ACCOUNT", source]]));

  assert.equal(parseEnv(rendered).SERVICE_ACCOUNT, source);
});

test("validateSecret supports JSON, PEM, URL, and non-empty values", () => {
  assert.doesNotThrow(() => validateSecret("JSON_KEY", '{"ok":true}', "json"));
  assert.doesNotThrow(() =>
    validateSecret(
      "PRIVATE_KEY",
      "-----BEGIN PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----",
      "pem",
    ),
  );
  assert.doesNotThrow(() =>
    validateSecret("SERVICE_URL", "https://example.com", "url"),
  );
  assert.throws(() => validateSecret("EMPTY", "", "nonempty"), /EMPTY/);
});

test("assertSafeTarget rejects paths outside the repository", async () => {
  const repository = await mkdtemp(path.join(tmpdir(), "secrets-safe-"));
  await assert.rejects(
    assertSafeTarget(repository, "../outside.env"),
    /outside the repository/,
  );
});

test("assertSafeTarget accepts an ignored untracked environment file", async () => {
  const repository = await mkdtemp(path.join(tmpdir(), "secrets-git-"));
  await mkdir(path.join(repository, "config"));
  await writeFile(path.join(repository, ".gitignore"), ".env\n");
  await writeFile(path.join(repository, ".env"), "OLD=value\n");

  const { execFile } = await import("node:child_process");
  await new Promise((resolve, reject) => {
    execFile("git", ["init", "-q"], { cwd: repository }, (error) => {
      if (error) reject(error);
      else resolve();
    });
  });

  await assertSafeTarget(repository, ".env");
  assert.equal(await readFile(path.join(repository, ".env"), "utf8"), "OLD=value\n");
});
