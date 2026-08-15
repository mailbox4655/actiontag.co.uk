/** Pinned, shared Chrome for Testing installation. Node 22+ standard library only. */
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { createWriteStream, existsSync } from "node:fs";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";

export const CFT = Object.freeze({
  version: "151.0.7922.77",
  platform: "win64",
  archiveUrl:
    "https://storage.googleapis.com/chrome-for-testing-public/151.0.7922.77/win64/chrome-win64.zip",
  archiveSha256: "a561db084cf08f3f4d25681ed3e764726b0537082f27063848a01e2e23d612ae",
  archiveName: "chrome-win64-151.0.7922.77.zip",
});

export class ControlledChromeError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = "ControlledChromeError";
    this.details = details;
  }
}

const INSTALL_MARKER = ".gpt-controlled-chrome-install";
const timestamp = () => new Date().toISOString();

function localDataRoot() {
  return process.env.LOCALAPPDATA || path.join(os.homedir(), ".local", "share");
}

export function centralChromeRoot() {
  return path.resolve(
    process.env.GPT_BRIDGE_CFT_ROOT ||
      path.join(localDataRoot(), "GPTDesignBridge", "chrome-for-testing"),
  );
}

export function downloadArchivePath() {
  return path.join(localDataRoot(), "GPTDesignBridge", "downloads", CFT.archiveName);
}

export function installedChromePath() {
  if (process.env.GPT_BRIDGE_CHROME_FOR_TESTING_PATH) {
    return path.resolve(process.env.GPT_BRIDGE_CHROME_FOR_TESTING_PATH);
  }
  return path.join(
    centralChromeRoot(),
    CFT.version,
    "chrome-win64",
    process.platform === "win32" ? "chrome.exe" : "chrome",
  );
}

function versionRoot() {
  return path.join(centralChromeRoot(), CFT.version);
}

function receiptPath() {
  return path.join(versionRoot(), "install-receipt.json");
}

async function sha256(file) {
  const hash = createHash("sha256");
  const handle = await fs.open(file, "r");
  try {
    for await (const chunk of handle.createReadStream()) hash.update(chunk);
  } finally {
    await handle.close();
  }
  return hash.digest("hex");
}

function assertDescendant(rootValue, candidateValue, operation) {
  const root = path.resolve(rootValue);
  const candidate = path.resolve(candidateValue);
  if (!candidate.startsWith(`${root}${path.sep}`)) {
    throw new ControlledChromeError(
      `Refusing ${operation} outside the controlled Chrome root ${root}: ${candidate}`,
    );
  }
  return candidate;
}

async function downloadPinnedArchive(target, progress) {
  await fs.mkdir(path.dirname(target), { recursive: true });
  const partial = `${target}.${process.pid}.${randomUUID()}.partial`;
  progress(`Downloading pinned Chrome for Testing ${CFT.version}`, {
    url: CFT.archiveUrl,
    target,
  });
  const response = await fetch(CFT.archiveUrl, { redirect: "follow" });
  if (!response.ok || !response.body) {
    throw new ControlledChromeError(
      `Chrome for Testing download failed with HTTP ${response.status} from ${CFT.archiveUrl}`,
      { status: response.status, url: CFT.archiveUrl },
    );
  }
  try {
    await pipeline(
      Readable.fromWeb(response.body),
      createWriteStream(partial, { flags: "wx" }),
    );
    await fs.rename(partial, target);
  } catch (error) {
    await fs.rm(partial, { force: true });
    throw error;
  }
}

async function verifiedArchive(progress) {
  const archive = downloadArchivePath();
  if (!existsSync(archive)) await downloadPinnedArchive(archive, progress);
  let actual = await sha256(archive);
  if (actual !== CFT.archiveSha256) {
    progress("Discarding a corrupt controlled-browser download before one clean retry", {
      archive,
      expectedSha256: CFT.archiveSha256,
      actualSha256: actual,
    });
    await fs.rm(archive, { force: true });
    await downloadPinnedArchive(archive, progress);
    actual = await sha256(archive);
  }
  if (actual !== CFT.archiveSha256) {
    throw new ControlledChromeError(
      `Pinned Chrome archive hash mismatch after a clean download: expected ${CFT.archiveSha256}, received ${actual}`,
      { archive, expectedSha256: CFT.archiveSha256, actualSha256: actual },
    );
  }
  return archive;
}

async function runExtractor(archive, staging) {
  await new Promise((resolve, reject) => {
    const child = spawn("tar.exe", ["-xf", archive, "-C", staging], {
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.once("error", (error) =>
      reject(
        new ControlledChromeError(`Could not start tar.exe to extract Chrome: ${error.message}`),
      ),
    );
    child.once("exit", (code) => {
      if (code === 0) return resolve();
      const detail = Buffer.concat(stderr.length ? stderr : stdout).toString("utf8").trim();
      reject(
        new ControlledChromeError(
          `Chrome archive extraction failed with code ${code}: ${detail || "tar.exe returned no diagnostic"}`,
        ),
      );
    });
  });
}

async function readValidReceipt(executable) {
  if (!existsSync(executable) || !existsSync(receiptPath())) return null;
  let receipt;
  try {
    receipt = JSON.parse(await fs.readFile(receiptPath(), "utf8"));
  } catch (error) {
    throw new ControlledChromeError(
      `Installed Chrome receipt is unreadable at ${receiptPath()}: ${error.message}`,
    );
  }
  const expected = {
    schema: "gpt-controlled-chrome-install",
    version: CFT.version,
    platform: CFT.platform,
    archiveUrl: CFT.archiveUrl,
    archiveSha256: CFT.archiveSha256,
  };
  for (const [field, value] of Object.entries(expected)) {
    if (receipt[field] !== value) {
      throw new ControlledChromeError(
        `Installed Chrome receipt field ${field} is inconsistent at ${receiptPath()}: expected ${JSON.stringify(value)}, received ${JSON.stringify(receipt[field])}`,
      );
    }
  }
  if (path.resolve(receipt.executable) !== path.resolve(executable)) {
    throw new ControlledChromeError(
      `Installed Chrome receipt points to ${receipt.executable}, but this controller requires ${executable}`,
    );
  }
  return receipt;
}

async function clearOwnedIncompleteInstall(progress) {
  const target = versionRoot();
  if (!existsSync(target)) return;
  const marker = path.join(target, INSTALL_MARKER);
  if (!existsSync(marker)) {
    throw new ControlledChromeError(
      `An incomplete, unmarked directory occupies the controlled Chrome version path ${target}. Move it aside manually; the harness will not delete an unowned directory.`,
    );
  }
  progress("Removing an incomplete install created by this controller", { target });
  await fs.rm(assertDescendant(centralChromeRoot(), target, "incomplete-install cleanup"), {
    recursive: true,
    force: true,
  });
}

export async function ensureChromeInstalled({ progress = () => {} } = {}) {
  const executable = installedChromePath();
  if (process.env.GPT_BRIDGE_CHROME_FOR_TESTING_PATH) {
    if (!existsSync(executable)) {
      throw new ControlledChromeError(
        `Dedicated Chrome for Testing path does not exist: ${executable}. The controller will not fall back to the owner's normal browser.`,
      );
    }
    return {
      executable,
      receipt: {
        schema: "gpt-controlled-chrome-external-pin",
        executable,
        suppliedBy: "GPT_BRIDGE_CHROME_FOR_TESTING_PATH",
      },
    };
  }
  if (process.platform !== "win32") {
    throw new ControlledChromeError(
      "The bundled Chrome for Testing pin is win64. Set GPT_BRIDGE_CHROME_FOR_TESTING_PATH to a dedicated non-personal Chrome for Testing binary on this platform.",
    );
  }

  const existing = await readValidReceipt(executable);
  if (existing) {
    progress("Using the verified shared Chrome for Testing installation", {
      executable,
      version: CFT.version,
    });
    return { executable, receipt: existing };
  }

  await clearOwnedIncompleteInstall(progress);
  const archive = await verifiedArchive(progress);
  const root = centralChromeRoot();
  await fs.mkdir(root, { recursive: true });
  const staging = await fs.mkdtemp(path.join(root, `.install-${CFT.version}-`));
  await fs.writeFile(path.join(staging, INSTALL_MARKER), `${timestamp()}\n`, "utf8");
  progress("Extracting the verified Chrome for Testing archive", { archive, staging });
  try {
    await runExtractor(archive, staging);
    const stagedExecutable = path.join(staging, "chrome-win64", "chrome.exe");
    if (!existsSync(stagedExecutable)) {
      throw new ControlledChromeError(
        `The verified archive did not produce chrome-win64/chrome.exe inside ${staging}`,
      );
    }
    await fs.rename(staging, versionRoot());
    const receipt = {
      schema: "gpt-controlled-chrome-install",
      version: CFT.version,
      platform: CFT.platform,
      archiveUrl: CFT.archiveUrl,
      archiveSha256: CFT.archiveSha256,
      executable,
      installedAt: timestamp(),
    };
    await fs.writeFile(receiptPath(), `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
    await fs.rm(archive, { force: true });
    progress("Chrome for Testing installation is ready", { executable });
    return { executable, receipt };
  } catch (error) {
    if (existsSync(staging)) {
      await fs.rm(assertDescendant(root, staging, "failed-install cleanup"), {
        recursive: true,
        force: true,
      });
    }
    throw error;
  }
}

