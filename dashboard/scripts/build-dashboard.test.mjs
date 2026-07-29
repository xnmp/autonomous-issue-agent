import assert from "node:assert/strict";
import { access, mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  assertInputsUnchanged,
  discardUnpublishedBuild,
} from "./build-dashboard.mjs";

test("accepts the exact input snapshot compiled by Vite", () => {
  const snapshot = { "src/App.tsx": "abc", "vite.config.ts": "def" };
  assert.doesNotThrow(() => assertInputsUnchanged(snapshot, { ...snapshot }));
});

test("rejects changed, added, and removed inputs during a build", () => {
  const before = { "src/App.tsx": "abc", "vite.config.ts": "def" };
  for (const after of [
    { ...before, "src/App.tsx": "changed" },
    { ...before, "src/new.ts": "new" },
    { "src/App.tsx": "abc" },
  ]) {
    assert.throws(
      () => assertInputsUnchanged(before, after),
      /source\/config changed during the build/,
    );
  }
});

test("removes failed unpublished builds without deleting published versions", async () => {
  const root = await mkdtemp(join(tmpdir(), "dashboard-build-"));
  const unpublished = join(root, "unpublished");
  const published = join(root, "published");
  try {
    await mkdir(unpublished);
    await writeFile(join(unpublished, "partial.js"), "partial");
    await discardUnpublishedBuild(unpublished, false);
    await assert.rejects(access(unpublished));

    await mkdir(published);
    await writeFile(join(published, "app.js"), "complete");
    await discardUnpublishedBuild(published, true);
    await access(join(published, "app.js"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
