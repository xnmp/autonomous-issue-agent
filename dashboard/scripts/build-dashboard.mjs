import { createHash, randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { mkdir, readdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const dashboardRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const inputSpecPath = join(dashboardRoot, "build-inputs.json");
const distRoot = join(dashboardRoot, "dist");
const buildId = `${Date.now()}-${process.pid}-${randomUUID()}`;
const relativeBuildDir = `builds/${buildId}`;
const distDir = join(distRoot, "builds", buildId);
const manifestPath = join(distDir, "build-manifest.json");
const pointerPath = join(distRoot, "current.json");

async function filesBelow(directory) {
  try {
    const entries = await readdir(directory, { withFileTypes: true });
    const nested = await Promise.all(
      entries.map(async (entry) => {
        const path = join(directory, entry.name);
        if (entry.isDirectory()) return filesBelow(path);
        if (entry.isFile()) return [path];
        return [];
      }),
    );
    return nested.flat();
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }
}

function relativeInput(path) {
  return relative(dashboardRoot, path).split(sep).join("/");
}

async function inputPaths(spec) {
  const explicit = await Promise.all(
    spec.files.map(async (name) => {
      const path = join(dashboardRoot, name);
      const info = await stat(path);
      if (!info.isFile()) throw new Error(`dashboard build input is not a file: ${name}`);
      return path;
    }),
  );
  const discovered = (
    await Promise.all(spec.directories.map((name) => filesBelow(join(dashboardRoot, name))))
  ).flat();
  return [...new Set([...explicit, ...discovered])].sort((a, b) =>
    relativeInput(a).localeCompare(relativeInput(b)),
  );
}

async function hashes(paths, root) {
  const result = {};
  for (const path of paths) {
    const name = relative(root, path).split(sep).join("/");
    result[name] = createHash("sha256").update(await readFile(path)).digest("hex");
  }
  return result;
}

async function outputPaths() {
  return (await filesBelow(distDir))
    .filter((path) => path !== manifestPath)
    .sort((a, b) => relative(a, distDir).localeCompare(relative(b, distDir)));
}

async function runTool(name, args) {
  const executable = join(
    dashboardRoot,
    "node_modules",
    ".bin",
    `${name}${process.platform === "win32" ? ".cmd" : ""}`,
  );
  await new Promise((resolvePromise, reject) => {
    const child = spawn(executable, args, { cwd: dashboardRoot, stdio: "inherit" });
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      if (code === 0) {
        resolvePromise();
        return;
      }
      reject(
        new Error(`${name} failed (${signal ? `signal ${signal}` : `exit ${code}`})`),
      );
    });
  });
}

const spec = JSON.parse(await readFile(inputSpecPath, "utf8"));
if (spec.schema !== 1 || !Array.isArray(spec.files) || !Array.isArray(spec.directories)) {
  throw new Error("dashboard/build-inputs.json has an unsupported shape");
}

export function assertInputsUnchanged(before, after) {
  if (JSON.stringify(before) === JSON.stringify(after)) return;
  throw new Error("dashboard source/config changed during the build; rerun npm run build");
}

export async function discardUnpublishedBuild(directory, published, remove = rm) {
  if (!published) await remove(directory, { recursive: true, force: true });
}

async function build() {
  let published = false;
  try {
    const inputsBefore = await hashes(await inputPaths(spec), dashboardRoot);
    await runTool("tsc", ["--noEmit"]);
    await runTool("vite", ["build", "--outDir", distDir, "--emptyOutDir"]);
    const inputsAfter = await hashes(await inputPaths(spec), dashboardRoot);
    assertInputsUnchanged(inputsBefore, inputsAfter);

    const outputs = await hashes(await outputPaths(), distDir);
    const manifest = `${JSON.stringify(
      { schema: 1, inputs: inputsBefore, outputs },
      null,
      2,
    )}\n`;
    const temporaryPath = `${manifestPath}.tmp`;
    await writeFile(temporaryPath, manifest, "utf8");
    await rename(temporaryPath, manifestPath);

    await mkdir(distRoot, { recursive: true });
    const pointerTemporary = `${pointerPath}.${buildId}.tmp`;
    await writeFile(
      pointerTemporary,
      `${JSON.stringify({ schema: 1, build: relativeBuildDir }, null, 2)}\n`,
      "utf8",
    );
    await rename(pointerTemporary, pointerPath);
    published = true;
  } finally {
    await discardUnpublishedBuild(distDir, published);
  }
}

if (resolve(process.argv[1] ?? "") === fileURLToPath(import.meta.url)) {
  await build();
}
