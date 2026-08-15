#!/usr/bin/env node
/**
 * Visible, isolated Chrome for Testing proof runner.
 *
 * This controller never opens the owner's default browser or profile. It uses one
 * pinned shared Chrome for Testing installation and a fresh marked profile per run.
 * Node 22+ standard library only.
 */
import { spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

import {
  bindRuntimeEvidence,
  captureScreenshot,
  classifyRuntime,
  evaluate,
  PLAN_SCHEMA,
  runAction,
  validatePlan,
} from "./controlled_chrome_actions.mjs";
import {
  removeOwnedProfile,
  launchOwnedChrome,
  sleep,
  stopOwnedChrome,
  timestamp,
  waitFor,
} from "./controlled_chrome_cdp.mjs";
import {
  CFT,
  ControlledChromeError,
  ensureChromeInstalled,
} from "./controlled_chrome_install.mjs";

export { CFT, PLAN_SCHEMA, classifyRuntime, validatePlan };
export { createOwnedProfile, removeOwnedProfile } from "./controlled_chrome_cdp.mjs";
export { installedChromePath } from "./controlled_chrome_install.mjs";

const PROOF_SCHEMA = "gpt-controlled-chrome-proof";

function progressReporter(report) {
  return (message, details = {}) => {
    const event = { at: timestamp(), message, details };
    report.progress.push(event);
    const detail = Object.keys(details).length ? ` ${JSON.stringify(details)}` : "";
    console.error(`[GPT Controlled Chrome] ${message}${detail}`);
  };
}

async function prepareOutput(value) {
  const output = path.resolve(value);
  if (existsSync(output)) {
    const stat = await fs.lstat(output);
    if (!stat.isDirectory() || stat.isSymbolicLink()) {
      throw new ControlledChromeError(`Proof output must be a real directory: ${output}`);
    }
    const entries = await fs.readdir(output);
    if (entries.length) {
      throw new ControlledChromeError(
        `Proof output directory must be empty so earlier evidence is never overwritten: ${output}; found ${entries.length} existing item(s)`,
      );
    }
  } else {
    await fs.mkdir(output, { recursive: true });
  }
  return output;
}

async function atomicWriteJson(file, value) {
  const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
  await fs.writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  await fs.rename(temporary, file);
}

function planSha256(plan) {
  return createHash("sha256").update(JSON.stringify(plan)).digest("hex");
}

function freshRuntime() {
  return {
    console: [],
    exceptions: [],
    networkFailures: [],
    httpErrors: [],
    warnings: [],
  };
}

async function enableEvidenceDomains(client, viewport) {
  await Promise.all([
    client.send("Page.enable"),
    client.send("Runtime.enable"),
    client.send("DOM.enable"),
    client.send("Log.enable"),
    client.send("Network.enable"),
    client.send("Emulation.setDeviceMetricsOverride", {
      ...viewport,
      mobile: false,
      screenWidth: viewport.width,
      screenHeight: viewport.height,
    }),
  ]);
}

export async function runProof(planValue, outputValue, { signal } = {}) {
  const plan = validatePlan(planValue);
  const output = await prepareOutput(outputValue);
  const proofPath = path.join(output, "proof.json");
  const partialPath = path.join(output, "proof.partial.json");
  const report = {
    schema: PROOF_SCHEMA,
    version: 2,
    status: "running",
    name: plan.name,
    planSha256: planSha256(plan),
    startedAt: timestamp(),
    requestedUrl: plan.url,
    context: new URL(plan.url).protocol.replace(":", ""),
    viewport: plan.viewport,
    configuredDeadlines: plan.deadlines,
    browser: {
      ownership: "harness-only-child-process-and-fresh-temporary-profile",
      product: "Chrome for Testing",
      pinnedVersion: CFT.version,
      headless: false,
      normalFileSecurity: true,
      existingProfileAccess: false,
      ownerDefaultBrowserUsed: false,
    },
    actions: [],
    progress: [],
    artifacts: [],
  };
  const progress = progressReporter(report);
  const persistPartial = () => atomicWriteJson(partialPath, report);
  let session;
  let runtime = freshRuntime();

  try {
    progress("Preparing the dedicated controlled browser", {
      requestedUrl: plan.url,
      context: report.context,
    });
    await persistPartial();
    const installation = await ensureChromeInstalled({ progress });
    report.browser.executable = installation.executable;
    report.browser.installReceipt = installation.receipt;
    await persistPartial();

    session = await launchOwnedChrome({
      executable: installation.executable,
      output,
      viewport: plan.viewport,
      deadlines: plan.deadlines,
      signal,
      progress,
    });
    report.browser.pid = session.child.pid;
    report.browser.debuggingPort = session.port;
    report.artifacts.push(session.stdoutPath, session.stderrPath);
    bindRuntimeEvidence(session.client, runtime);
    await enableEvidenceDomains(session.client, plan.viewport);
    await persistPartial();

    progress("Navigating the owned browser", { url: plan.url });
    const navigation = await session.client.send("Page.navigate", { url: plan.url });
    if (navigation.errorText) {
      throw new ControlledChromeError(
        `Chrome could not navigate to ${plan.url}: ${navigation.errorText}`,
      );
    }
    await waitFor(
      `initial load of ${plan.url}`,
      async () => (await evaluate(session.client, "document.readyState")) === "complete",
      {
        deadlineMs: plan.deadlines.navigationMs,
        heartbeatMs: plan.deadlines.heartbeatMs,
        signal,
        progress,
      },
    );
    report.loadedAt = timestamp();
    await persistPartial();

    for (let index = 0; index < plan.actions.length; index += 1) {
      try {
        const result = await runAction(session.client, plan.actions[index], {
          index,
          output,
          artifacts: report.artifacts,
          defaultActionMs: plan.deadlines.actionMs,
          heartbeatMs: plan.deadlines.heartbeatMs,
          signal,
          progress,
        });
        report.actions.push(result);
      } catch (error) {
        report.actions.push({
          index,
          type: plan.actions[index].type,
          status: "fail",
          completedAt: timestamp(),
          failure: error instanceof Error ? error.message : String(error),
        });
        await persistPartial();
        throw error;
      }
      await persistPartial();
    }

    if (plan.pauseAfterMs) {
      progress("Holding the visible final state before capture", { pauseAfterMs: plan.pauseAfterMs });
      await sleep(plan.pauseAfterMs, signal, "final-state pause");
    }
    const finalScreenshot = await captureScreenshot(session.client, output, "final");
    const domPath = path.join(output, "dom.html");
    await fs.writeFile(
      domPath,
      await evaluate(session.client, "document.documentElement.outerHTML"),
      "utf8",
    );
    report.artifacts.push(finalScreenshot, domPath);
    report.finalUrl = await evaluate(session.client, "location.href");
    report.title = await evaluate(session.client, "document.title");
    const classified = classifyRuntime(runtime, plan.ignoreErrorPatterns);
    report.runtime = { ...runtime, ...classified };
    if (classified.errors.length) {
      report.status = "fail";
      report.failure = `${classified.errors.length} unignored browser runtime error(s); inspect proof.json runtime.errors`;
    } else {
      report.status = "pass";
    }
  } catch (error) {
    report.status = "fail";
    report.failure = error instanceof Error ? error.message : String(error);
    if (error?.details && Object.keys(error.details).length) {
      report.failureDetails = error.details;
    }
    report.runtime = {
      ...runtime,
      ...classifyRuntime(runtime, plan.ignoreErrorPatterns),
    };
  } finally {
    report.completedAt = timestamp();
    if (session) {
      try {
        await stopOwnedChrome(session, {
          shutdownMs: plan.deadlines.shutdownMs,
          progress,
        });
      } catch (error) {
        report.status = "fail";
        report.failure = `${report.failure ? `${report.failure}; ` : ""}owned-browser shutdown failed: ${error.message}`;
      }
      try {
        await removeOwnedProfile(session.profile);
        report.browser.temporaryProfileRemoved = true;
      } catch (error) {
        report.browser.temporaryProfileRemoved = false;
        report.status = "fail";
        report.failure = `${report.failure ? `${report.failure}; ` : ""}owned-profile cleanup failed for ${session.profile}: ${error.message}`;
      }
    }
    await atomicWriteJson(proofPath, report);
    await fs.rm(partialPath, { force: true });
  }
  return report;
}

export function templatePlan() {
  return {
    schema: PLAN_SCHEMA,
    version: 1,
    name: "visible-owned-browser-proof",
    url: "http://127.0.0.1:3000/",
    viewport: { width: 1440, height: 1000, deviceScaleFactor: 1 },
    deadlines: {
      launchMs: 60_000,
      navigationMs: 60_000,
      actionMs: 30_000,
      shutdownMs: 10_000,
      heartbeatMs: 2_000,
    },
    actions: [
      { type: "wait-for", selector: "main" },
      { type: "click", selector: "button[data-test='primary-action']" },
      { type: "assert", selector: "[data-test='result']", text: "Expected result" },
      { type: "screenshot", name: "primary-flow" },
    ],
    ignoreErrorPatterns: [],
    pauseAfterMs: 750,
  };
}

function parseOptions(values, allowed) {
  const options = {};
  for (let index = 0; index < values.length; index += 2) {
    const flag = values[index];
    const value = values[index + 1];
    if (!flag?.startsWith("--") || value === undefined) {
      throw new ControlledChromeError(`Expected --name value pairs; received ${JSON.stringify(values.slice(index))}`);
    }
    const name = flag.slice(2);
    if (!allowed.has(name)) throw new ControlledChromeError(`Unknown option ${flag}`);
    if (name in options) throw new ControlledChromeError(`Option ${flag} was supplied more than once`);
    options[name] = value;
  }
  return options;
}

async function doctor() {
  const installation = await ensureChromeInstalled({
    progress: (message, details) =>
      console.error(`[GPT Controlled Chrome] ${message} ${JSON.stringify(details)}`),
  });
  const version = spawnSync(installation.executable, ["--version"], {
    encoding: "utf8",
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (version.status !== 0) {
    throw new ControlledChromeError(
      `Dedicated Chrome executable did not answer --version: ${(version.stderr || version.stdout || "no diagnostic").trim()}`,
    );
  }
  return {
    status: "ready",
    executable: installation.executable,
    reportedVersion: version.stdout.trim(),
    pin: CFT,
    ownerBrowserTouched: false,
  };
}

async function main(argv = process.argv.slice(2)) {
  const command = argv[0];
  if (command === "install" || command === "doctor") {
    if (argv.length !== 1) throw new ControlledChromeError(`${command} accepts no options`);
    console.log(JSON.stringify(await doctor(), null, 2));
    return 0;
  }
  if (command === "template") {
    if (argv.length !== 1) throw new ControlledChromeError("template accepts no options");
    console.log(JSON.stringify(templatePlan(), null, 2));
    return 0;
  }
  if (command === "run") {
    const options = parseOptions(argv.slice(1), new Set(["plan", "output"]));
    if (!options.plan || !options.output) {
      throw new ControlledChromeError("run requires --plan <plan.json> and --output <empty-directory>");
    }
    let plan;
    try {
      plan = JSON.parse(await fs.readFile(path.resolve(options.plan), "utf8"));
    } catch (error) {
      throw new ControlledChromeError(`Could not read proof plan ${path.resolve(options.plan)}: ${error.message}`);
    }
    const controller = new AbortController();
    const interrupt = () => controller.abort(new Error("the controller received an interrupt signal"));
    process.once("SIGINT", interrupt);
    process.once("SIGTERM", interrupt);
    try {
      const report = await runProof(plan, options.output, { signal: controller.signal });
      console.log(
        JSON.stringify(
          {
            status: report.status,
            proof: path.join(path.resolve(options.output), "proof.json"),
            context: report.context,
            finalUrl: report.finalUrl || null,
          },
          null,
          2,
        ),
      );
      return report.status === "pass" ? 0 : 2;
    } finally {
      process.removeListener("SIGINT", interrupt);
      process.removeListener("SIGTERM", interrupt);
    }
  }
  console.log(
    [
      "Usage:",
      "  node controlled_chrome.mjs doctor",
      "  node controlled_chrome.mjs install",
      "  node controlled_chrome.mjs template",
      "  node controlled_chrome.mjs run --plan <plan.json> --output <empty-directory>",
      "",
      "The tool always launches visible pinned Chrome for Testing with a fresh owned profile.",
      "It never uses the owner's default browser, never uses the owner's profile, and never disables file security.",
    ].join("\n"),
  );
  return command ? 2 : 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().then(
    (code) => {
      process.exitCode = code;
    },
    (error) => {
      console.error(`GPT Controlled Chrome ERROR: ${error.message}`);
      process.exitCode = 2;
    },
  );
}

