/** Owned Chrome process and Chrome DevTools Protocol transport. */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, openSync, closeSync } from "node:fs";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { ControlledChromeError } from "./controlled_chrome_install.mjs";

const PROFILE_PREFIX = "gpt-controlled-chrome-";
const PROFILE_MARKER = ".gpt-controlled-chrome-profile.json";

export const timestamp = () => new Date().toISOString();

export function abortError(signal, activity) {
  if (!signal?.aborted) return;
  const reason = signal.reason instanceof Error ? signal.reason.message : String(signal.reason || "aborted");
  throw new ControlledChromeError(`${activity} was interrupted: ${reason}`);
}

export async function sleep(ms, signal, activity = "wait") {
  abortError(signal, activity);
  await new Promise((resolve, reject) => {
    let timer;
    const cleanup = () => signal?.removeEventListener("abort", onAbort);
    const finish = () => {
      cleanup();
      resolve();
    };
    const onAbort = () => {
      clearTimeout(timer);
      cleanup();
      reject(new ControlledChromeError(`${activity} was interrupted`));
    };
    timer = setTimeout(finish, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export async function waitFor(
  label,
  callback,
  { deadlineMs = 60_000, heartbeatMs = 2_000, signal, progress = () => {} } = {},
) {
  const started = Date.now();
  let nextHeartbeat = started + heartbeatMs;
  let lastError;
  while (true) {
    abortError(signal, label);
    try {
      const result = await callback();
      if (result) return result;
    } catch (error) {
      if (error?.details?.fatal) throw error;
      lastError = error;
    }
    const elapsedMs = Date.now() - started;
    if (deadlineMs !== null && elapsedMs >= deadlineMs) {
      throw new ControlledChromeError(
        `Timed out after ${elapsedMs}ms waiting for ${label}${lastError ? `; last diagnostic: ${lastError.message}` : ""}`,
        { label, elapsedMs, deadlineMs, lastError: lastError?.message },
      );
    }
    if (Date.now() >= nextHeartbeat) {
      progress(`Still waiting for ${label}`, {
        elapsedMs,
        lastDiagnostic: lastError?.message || null,
      });
      nextHeartbeat = Date.now() + heartbeatMs;
    }
    await sleep(100, signal, label);
  }
}

export class CDPClient {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
  }

  async connect({ signal } = {}) {
    abortError(signal, "CDP connection");
    this.socket = new WebSocket(this.url);
    await new Promise((resolve, reject) => {
      const onAbort = () => reject(new ControlledChromeError("CDP connection was interrupted"));
      signal?.addEventListener("abort", onAbort, { once: true });
      this.socket.addEventListener("open", () => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      }, { once: true });
      this.socket.addEventListener("error", () => {
        signal?.removeEventListener("abort", onAbort);
        reject(new ControlledChromeError(`CDP connection failed: ${this.url}`));
      }, { once: true });
    });
    this.socket.addEventListener("message", ({ data }) => {
      let message;
      try {
        message = JSON.parse(String(data));
      } catch (error) {
        this.rejectPending(new ControlledChromeError(`CDP returned invalid JSON: ${error.message}`));
        return;
      }
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) {
          pending.reject(
            new ControlledChromeError(`${pending.method}: ${message.error.message}`),
          );
        } else {
          pending.resolve(message.result || {});
        }
        return;
      }
      for (const listener of this.listeners.get(message.method) || []) {
        try {
          listener(message.params || {});
        } catch {
          // Evidence listeners must not break protocol transport.
        }
      }
    });
    this.socket.addEventListener("close", () =>
      this.rejectPending(new ControlledChromeError("CDP connection closed")),
    );
  }

  rejectPending(error) {
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }

  on(method, listener) {
    this.listeners.set(method, [...(this.listeners.get(method) || []), listener]);
  }

  send(method, params = {}) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new ControlledChromeError(`CDP is not connected for ${method}`));
    }
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, method });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) this.socket.close();
  }
}

export async function createOwnedProfile() {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), PROFILE_PREFIX));
  await fs.writeFile(
    path.join(profile, PROFILE_MARKER),
    `${JSON.stringify({ schema: "gpt-controlled-chrome-profile", controllerPid: process.pid, createdAt: timestamp() }, null, 2)}\n`,
    "utf8",
  );
  return profile;
}

export async function markOwnedBrowserPid(profile, browserPid) {
  const marker = path.join(profile, PROFILE_MARKER);
  const value = JSON.parse(await fs.readFile(marker, "utf8"));
  value.browserPid = browserPid;
  await fs.writeFile(marker, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

export async function removeOwnedProfile(profile) {
  const resolved = path.resolve(profile);
  const temporaryRoot = await fs.realpath(os.tmpdir());
  let realProfile;
  try {
    realProfile = await fs.realpath(resolved);
  } catch (error) {
    if (error.code === "ENOENT") return;
    throw error;
  }
  const marker = path.join(realProfile, PROFILE_MARKER);
  if (
    !realProfile.startsWith(`${temporaryRoot}${path.sep}`) ||
    !path.basename(realProfile).startsWith(PROFILE_PREFIX) ||
    !existsSync(marker)
  ) {
    throw new ControlledChromeError(`Refusing to remove unowned browser profile: ${realProfile}`);
  }
  const metadata = JSON.parse(await fs.readFile(marker, "utf8"));
  if (metadata.schema !== "gpt-controlled-chrome-profile") {
    throw new ControlledChromeError(
      `Refusing to remove profile with an invalid ownership marker: ${marker}`,
    );
  }
  await fs.rm(realProfile, { recursive: true, force: true });
}

export async function launchOwnedChrome({
  executable,
  output,
  viewport,
  deadlines,
  signal,
  progress = () => {},
}) {
  const profile = await createOwnedProfile();
  const stdoutPath = path.join(output, "chrome-stdout.log");
  const stderrPath = path.join(output, "chrome-stderr.log");
  const stdoutFd = openSync(stdoutPath, "a");
  const stderrFd = openSync(stderrPath, "a");
  let child;
  try {
    const args = [
      `--user-data-dir=${profile}`,
      "--remote-debugging-address=127.0.0.1",
      "--remote-debugging-port=0",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-default-apps",
      "--disable-extensions",
      "--disable-sync",
      "--metrics-recording-only",
      "--password-store=basic",
      `--window-size=${Math.round(viewport.width)},${Math.round(viewport.height)}`,
      "--window-position=30,30",
      "about:blank",
    ];
    child = spawn(executable, args, {
      detached: false,
      windowsHide: false,
      stdio: ["ignore", stdoutFd, stderrFd],
    });
    await new Promise((resolve, reject) => {
      child.once("spawn", resolve);
      child.once("error", (error) =>
        reject(
          new ControlledChromeError(
            `Could not start dedicated Chrome for Testing at ${executable}: ${error.message}`,
            { fatal: true, executable },
          ),
        ),
      );
    });
    await markOwnedBrowserPid(profile, child.pid);
    progress("Started a visible, isolated Chrome for Testing process", {
      pid: child.pid,
      profile,
    });
    const port = await waitFor(
      "the owned Chrome DevTools port",
      async () => {
        if (child.exitCode !== null) {
          throw new ControlledChromeError(
            `Owned Chrome exited with code ${child.exitCode}; inspect ${stderrPath}`,
            { fatal: true, exitCode: child.exitCode, stderrPath },
          );
        }
        const file = path.join(profile, "DevToolsActivePort");
        if (!existsSync(file)) return null;
        const value = (await fs.readFile(file, "utf8")).split(/\r?\n/)[0];
        return /^\d+$/.test(value) ? Number(value) : null;
      },
      {
        deadlineMs: deadlines.launchMs,
        heartbeatMs: deadlines.heartbeatMs,
        signal,
        progress,
      },
    );
    const socket = await waitFor(
      "the owned page target",
      async () => {
        const response = await fetch(`http://127.0.0.1:${port}/json/list`);
        if (!response.ok) return null;
        const targets = await response.json();
        return targets.find((target) => target.type === "page")?.webSocketDebuggerUrl;
      },
      {
        deadlineMs: deadlines.launchMs,
        heartbeatMs: deadlines.heartbeatMs,
        signal,
        progress,
      },
    );
    const client = new CDPClient(socket);
    await client.connect({ signal });
    return {
      child,
      client,
      profile,
      port,
      stdoutPath,
      stderrPath,
      closeDescriptors: () => {
        closeSync(stdoutFd);
        closeSync(stderrFd);
      },
    };
  } catch (error) {
    closeSync(stdoutFd);
    closeSync(stderrFd);
    if (child && child.exitCode === null) child.kill("SIGTERM");
    try {
      await removeOwnedProfile(profile);
    } catch {
      // The primary launch error remains the useful diagnostic.
    }
    throw error;
  }
}

export async function stopOwnedChrome(session, { shutdownMs = 10_000, progress = () => {} } = {}) {
  if (!session) return;
  try {
    await session.client?.send("Browser.close");
  } catch {
    // PID-bounded fallback follows.
  }
  const child = session.child;
  if (child && child.exitCode === null) {
    const exited = await new Promise((resolve) => {
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        child.removeListener("exit", onExit);
        resolve(value);
      };
      const onExit = () => finish(true);
      const timer = setTimeout(() => finish(false), shutdownMs);
      child.once("exit", onExit);
      // Cover the narrow race where Chrome exits between the outer check and listener setup.
      if (child.exitCode !== null) finish(true);
    });
    if (!exited && child.exitCode === null) {
      progress("Owned Chrome did not close in the configured shutdown grace period", {
        pid: child.pid,
        shutdownMs,
      });
      if (process.platform === "win32") {
        spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
          windowsHide: true,
          stdio: "ignore",
        });
      } else {
        child.kill("SIGKILL");
      }
    }
  }
  session.client?.close();
  session.closeDescriptors?.();
}
