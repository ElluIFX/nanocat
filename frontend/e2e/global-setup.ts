import type { FullConfig } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const projectRoot = resolve(frontendRoot, "..");

async function waitForExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  return await new Promise((resolveExit) => {
    const timer = setTimeout(() => resolveExit(false), timeoutMs);
    child.once("exit", () => {
      clearTimeout(timer);
      resolveExit(true);
    });
  });
}

async function stopProcessTree(child: ChildProcess): Promise<void> {
  if (child.pid === undefined || child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === "win32") {
    const killer = spawn("taskkill.exe", ["/PID", String(child.pid), "/T"], { stdio: "ignore" });
    await waitForExit(killer, 5_000);
  } else {
    process.kill(-child.pid, "SIGTERM");
  }
  if (await waitForExit(child, 10_000)) return;
  if (process.platform === "win32") {
    const killer = spawn("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
    await waitForExit(killer, 5_000);
  } else {
    process.kill(-child.pid, "SIGKILL");
  }
  await waitForExit(child, 5_000);
}

export default async function globalSetup(_config: FullConfig): Promise<() => Promise<void>> {
  if (process.env.NANOCAT_WEB_URL) return async () => {};

  const port = process.env.NANOCAT_E2E_PORT;
  if (!port) throw new Error("NANOCAT_E2E_PORT was not initialized by Playwright config");
  const baseURL = `http://127.0.0.1:${port}`;
  const workdir = await mkdtemp(join(tmpdir(), "nanocat-playwright-runtime-"));

  const uv = process.platform === "win32" ? "uv.exe" : "uv";
  const child = spawn(
    uv,
    ["run", "--no-sync", "python", "-m", "nanocat", "-w", workdir],
    {
      cwd: projectRoot,
      detached: process.platform !== "win32",
      env: { ...process.env, NANOCAT_WEB_HOST: "127.0.0.1", NANOCAT_WEB_PORT: port },
      stdio: "inherit",
    },
  );

  const deadline = Date.now() + 180_000;
  try {
    while (Date.now() < deadline) {
      if (child.exitCode !== null || child.signalCode !== null) {
        throw new Error(`NanoCat E2E runtime exited before becoming ready (${child.exitCode ?? child.signalCode})`);
      }
      try {
        const response = await fetch(`${baseURL}/auth/status`);
        if (response.ok) {
          return async () => {
            await stopProcessTree(child);
            await rm(workdir, { recursive: true, force: true });
          };
        }
      } catch {
        // Listener startup is still in progress.
      }
      await new Promise((resolveWait) => setTimeout(resolveWait, 250));
    }
    throw new Error("NanoCat E2E runtime did not become ready within 180 seconds");
  } catch (error) {
    await stopProcessTree(child);
    await rm(workdir, { recursive: true, force: true });
    throw error;
  }
}
