/** Proof-plan validation and native CDP interactions. */
import { existsSync } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";

import { ControlledChromeError } from "./controlled_chrome_install.mjs";
import { abortError, sleep, timestamp, waitFor } from "./controlled_chrome_cdp.mjs";

export const PLAN_SCHEMA = "gpt-controlled-chrome-plan";
export const ACTION_TYPES = new Set([
  "assert",
  "click",
  "evaluate",
  "hover",
  "press",
  "reload",
  "screenshot",
  "set-files",
  "type",
  "wait",
  "wait-for",
]);

const SELECTOR_ACTIONS = new Set([
  "assert",
  "click",
  "hover",
  "set-files",
  "type",
  "wait-for",
]);

function positiveNumber(value, label, { allowNull = false } = {}) {
  if (allowNull && value === null) return null;
  if (!Number.isFinite(value) || value <= 0) {
    throw new ControlledChromeError(`${label} must be a positive number${allowNull ? " or null" : ""}`);
  }
  return Number(value);
}

function nonNegativeNumber(value, label) {
  if (!Number.isFinite(value) || value < 0) {
    throw new ControlledChromeError(`${label} must be a non-negative number`);
  }
  return Number(value);
}

function deadlineValue(raw, field, fallback, { allowNull = true } = {}) {
  if (!(field in raw)) return fallback;
  return positiveNumber(raw[field], `deadlines.${field}`, { allowNull });
}

function validateAction(raw, index) {
  const label = `Action ${index + 1}`;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new ControlledChromeError(`${label} must be an object`);
  }
  if (!ACTION_TYPES.has(raw.type)) {
    throw new ControlledChromeError(`${label} has unsupported type ${JSON.stringify(raw.type)}`);
  }
  if (SELECTOR_ACTIONS.has(raw.type) && typeof raw.selector !== "string") {
    throw new ControlledChromeError(`${label} (${raw.type}) requires a selector string`);
  }
  if (raw.type === "set-files") {
    if (!Array.isArray(raw.files) || !raw.files.every((file) => typeof file === "string")) {
      throw new ControlledChromeError(`${label} set-files requires files as a string array`);
    }
  }
  if (raw.type === "evaluate" && typeof raw.expression !== "string") {
    throw new ControlledChromeError(`${label} evaluate requires an expression string`);
  }
  if (raw.type === "press" && typeof raw.key !== "string") {
    throw new ControlledChromeError(`${label} press requires a key string`);
  }
  if (raw.type === "wait") nonNegativeNumber(Number(raw.ms ?? 0), `${label}.ms`);
  if ("timeoutMs" in raw) {
    positiveNumber(raw.timeoutMs, `${label}.timeoutMs`, { allowNull: true });
  }
  return { ...raw };
}

export function validatePlan(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new ControlledChromeError("Proof plan must be a JSON object");
  }
  if (raw.schema !== PLAN_SCHEMA || raw.version !== 1) {
    throw new ControlledChromeError(`Proof plan must be ${PLAN_SCHEMA} version 1`);
  }
  let url;
  try {
    url = new URL(raw.url);
  } catch {
    throw new ControlledChromeError(`Proof plan URL is invalid: ${raw.url}`);
  }
  if (!["http:", "https:", "file:"].includes(url.protocol)) {
    throw new ControlledChromeError(`Proof plan URL protocol is not allowed: ${url.protocol}`);
  }
  if (!Array.isArray(raw.actions)) {
    throw new ControlledChromeError("actions must be an array");
  }
  const actions = raw.actions.map(validateAction);
  const ignoreErrorPatterns = raw.ignoreErrorPatterns || [];
  if (
    !Array.isArray(ignoreErrorPatterns) ||
    !ignoreErrorPatterns.every((item) => typeof item === "string")
  ) {
    throw new ControlledChromeError("ignoreErrorPatterns must be a string array");
  }
  for (const pattern of ignoreErrorPatterns) {
    try {
      new RegExp(pattern, "i");
    } catch (error) {
      throw new ControlledChromeError(
        `Invalid ignoreErrorPatterns expression ${JSON.stringify(pattern)}: ${error.message}`,
      );
    }
  }
  const viewport = {
    width: Number(raw.viewport?.width ?? 1440),
    height: Number(raw.viewport?.height ?? 1000),
    deviceScaleFactor: Number(raw.viewport?.deviceScaleFactor ?? 1),
  };
  for (const field of ["width", "height", "deviceScaleFactor"]) {
    positiveNumber(viewport[field], `viewport.${field}`);
  }
  const rawDeadlines = raw.deadlines || {};
  if (typeof rawDeadlines !== "object" || Array.isArray(rawDeadlines)) {
    throw new ControlledChromeError("deadlines must be an object when supplied");
  }
  const deadlines = {
    launchMs: deadlineValue(rawDeadlines, "launchMs", 60_000),
    navigationMs: deadlineValue(rawDeadlines, "navigationMs", 60_000),
    actionMs: deadlineValue(rawDeadlines, "actionMs", 30_000),
    shutdownMs: deadlineValue(rawDeadlines, "shutdownMs", 10_000, { allowNull: false }),
    heartbeatMs: deadlineValue(rawDeadlines, "heartbeatMs", 2_000, { allowNull: false }),
  };
  const pauseAfterMs = nonNegativeNumber(Number(raw.pauseAfterMs ?? 750), "pauseAfterMs");
  return {
    schema: PLAN_SCHEMA,
    version: 1,
    name: String(raw.name || "controlled-chrome-proof"),
    url: url.href,
    actions,
    viewport,
    deadlines,
    ignoreErrorPatterns,
    pauseAfterMs,
  };
}

export async function evaluate(client, expression) {
  const result = await client.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
    userGesture: true,
  });
  if (result.exceptionDetails) {
    throw new ControlledChromeError(
      `Browser evaluation failed: ${result.exceptionDetails.exception?.description || result.exceptionDetails.text}`,
    );
  }
  return result.result?.value;
}

function selectorExpression(selector) {
  return `(()=>{
    const element=document.querySelector(${JSON.stringify(selector)});
    if(!element)return null;
    element.scrollIntoView({block:"center",inline:"center"});
    const rect=element.getBoundingClientRect();
    const style=getComputedStyle(element);
    return {
      x:rect.left+rect.width/2,
      y:rect.top+rect.height/2,
      width:rect.width,
      height:rect.height,
      visible:rect.width>0&&rect.height>0&&style.display!=="none"&&style.visibility!=="hidden"&&Number(style.opacity)!==0,
      text:element.innerText??element.textContent??"",
      value:"value" in element?element.value:null,
      disabled:Boolean(element.disabled),
      tag:element.tagName
    };
  })()`;
}

export async function visibleElement(client, selector, options = {}) {
  return waitFor(
    `visible selector ${selector}`,
    async () => {
      const value = await evaluate(client, selectorExpression(selector));
      return value?.visible ? value : null;
    },
    options,
  );
}

async function click(client, selector, waitOptions) {
  const point = await visibleElement(client, selector, waitOptions);
  if (point.disabled) throw new ControlledChromeError(`Cannot click disabled element ${selector}`);
  await client.send("Input.dispatchMouseEvent", {
    type: "mouseMoved",
    x: point.x,
    y: point.y,
  });
  for (const type of ["mousePressed", "mouseReleased"]) {
    await client.send("Input.dispatchMouseEvent", {
      type,
      x: point.x,
      y: point.y,
      button: "left",
      clickCount: 1,
    });
  }
}

const KEYS = {
  Enter: ["Enter", 13],
  Escape: ["Escape", 27],
  Tab: ["Tab", 9],
  Backspace: ["Backspace", 8],
  Delete: ["Delete", 46],
  ArrowDown: ["ArrowDown", 40],
  ArrowUp: ["ArrowUp", 38],
  ArrowLeft: ["ArrowLeft", 37],
  ArrowRight: ["ArrowRight", 39],
  Home: ["Home", 36],
  End: ["End", 35],
};

async function press(client, key, modifiers = 0) {
  const [code, windowsVirtualKeyCode] = KEYS[key] || [
    key,
    key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0,
  ];
  for (const type of ["rawKeyDown", "keyUp"]) {
    await client.send("Input.dispatchKeyEvent", {
      type,
      key,
      code,
      windowsVirtualKeyCode,
      nativeVirtualKeyCode: windowsVirtualKeyCode,
      modifiers,
    });
  }
}

function artifactName(value) {
  const result = String(value)
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (!result) throw new ControlledChromeError("Screenshot artifact name is empty");
  return result;
}

export async function captureScreenshot(client, output, name) {
  const shot = await client.send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: false,
    fromSurface: true,
  });
  const file = path.join(output, `${artifactName(name)}.png`);
  await fs.writeFile(file, Buffer.from(shot.data, "base64"));
  return file;
}

function withDeadline(promise, deadlineMs, label) {
  if (deadlineMs === null) return promise;
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new ControlledChromeError(`${label} exceeded its configured ${deadlineMs}ms deadline`)),
      deadlineMs,
    );
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

async function performAction(client, item, context) {
  const waitOptions = {
    deadlineMs: item.timeoutMs ?? context.defaultActionMs,
    heartbeatMs: context.heartbeatMs,
    signal: context.signal,
    progress: context.progress,
  };
  if (item.type === "wait") {
    await sleep(Number(item.ms ?? 0), context.signal, `wait action ${context.index + 1}`);
  } else if (item.type === "wait-for") {
    await visibleElement(client, item.selector, waitOptions);
  } else if (item.type === "click") {
    await click(client, item.selector, waitOptions);
  } else if (item.type === "hover") {
    const point = await visibleElement(client, item.selector, waitOptions);
    await client.send("Input.dispatchMouseEvent", {
      type: "mouseMoved",
      x: point.x,
      y: point.y,
    });
  } else if (item.type === "type") {
    await click(client, item.selector, waitOptions);
    if (item.clear !== false) {
      await press(client, "a", 2);
      await press(client, "Backspace");
    }
    await client.send("Input.insertText", { text: String(item.text ?? "") });
  } else if (item.type === "press") {
    await press(client, item.key, Number(item.modifiers ?? 0));
  } else if (item.type === "reload") {
    await client.send("Page.reload", { ignoreCache: Boolean(item.ignoreCache) });
    await waitFor(
      "page reload",
      async () => (await evaluate(client, "document.readyState")) === "complete",
      waitOptions,
    );
  } else if (item.type === "assert") {
    const state = await visibleElement(client, item.selector, waitOptions);
    if ("text" in item && !state.text.includes(String(item.text))) {
      throw new ControlledChromeError(
        `Expected ${item.selector} text to contain ${JSON.stringify(item.text)}; received ${JSON.stringify(state.text)}`,
      );
    }
    if ("value" in item && state.value !== item.value) {
      throw new ControlledChromeError(
        `Expected ${item.selector} value ${JSON.stringify(item.value)}; received ${JSON.stringify(state.value)}`,
      );
    }
  } else if (item.type === "evaluate") {
    await evaluate(client, item.expression);
  } else if (item.type === "screenshot") {
    context.artifacts.push(
      await captureScreenshot(client, context.output, item.name || `step-${context.index + 1}`),
    );
  } else if (item.type === "set-files") {
    const document = await client.send("DOM.getDocument", { depth: -1, pierce: true });
    const match = await client.send("DOM.querySelector", {
      nodeId: document.root.nodeId,
      selector: item.selector,
    });
    if (!match.nodeId) {
      throw new ControlledChromeError(
        `File input ${item.selector} was not found while action ${context.index + 1} tried to attach files`,
      );
    }
    const files = item.files.map((file) => path.resolve(file));
    for (const file of files) {
      if (!existsSync(file)) {
        throw new ControlledChromeError(
          `Input file ${file} does not exist; action ${context.index + 1} needs it for ${item.selector}`,
        );
      }
    }
    await client.send("DOM.setFileInputFiles", { nodeId: match.nodeId, files });
  }
}

export async function runAction(client, item, context) {
  abortError(context.signal, `action ${context.index + 1}`);
  const startedAt = timestamp();
  const startedMs = Date.now();
  context.progress(`Starting browser action ${context.index + 1}: ${item.type}`, {
    index: context.index,
    type: item.type,
    selector: item.selector || null,
  });
  let deadlineMs = item.timeoutMs ?? context.defaultActionMs;
  if (item.type === "wait" && item.timeoutMs === undefined && deadlineMs !== null) {
    deadlineMs = Math.max(deadlineMs, Number(item.ms ?? 0) + 5_000);
  }
  await withDeadline(performAction(client, item, context), deadlineMs, `Action ${context.index + 1} (${item.type})`);
  const result = {
    index: context.index,
    type: item.type,
    status: "pass",
    startedAt,
    completedAt: timestamp(),
    durationMs: Date.now() - startedMs,
  };
  context.progress(`Completed browser action ${context.index + 1}: ${item.type}`, result);
  return result;
}

function remoteText(value) {
  return "value" in value ? String(value.value) : value.description || value.type || "";
}

export function bindRuntimeEvidence(client, runtime) {
  const requests = new Map();
  client.on("Runtime.consoleAPICalled", (event) =>
    runtime.console.push({
      at: timestamp(),
      level: event.type,
      text: event.args.map(remoteText).join(" "),
    }),
  );
  client.on("Runtime.exceptionThrown", (event) =>
    runtime.exceptions.push({
      at: timestamp(),
      text: event.exceptionDetails?.exception?.description || event.exceptionDetails?.text,
    }),
  );
  client.on("Log.entryAdded", (event) => {
    const value = {
      at: timestamp(),
      level: event.entry.level,
      text: event.entry.text,
      url: event.entry.url,
    };
    (event.entry.level === "warning" ? runtime.warnings : runtime.console).push(value);
  });
  client.on("Network.requestWillBeSent", (event) =>
    requests.set(event.requestId, event.request.url),
  );
  client.on("Network.loadingFailed", (event) =>
    runtime.networkFailures.push({
      at: timestamp(),
      url: requests.get(event.requestId),
      errorText: event.errorText,
      blockedReason: event.blockedReason,
    }),
  );
  client.on("Network.responseReceived", (event) => {
    if (event.response.status >= 400) {
      runtime.httpErrors.push({
        at: timestamp(),
        url: event.response.url,
        status: event.response.status,
        statusText: event.response.statusText,
      });
    }
  });
}

export function classifyRuntime(runtime, patterns = []) {
  const matchers = patterns.map((pattern) => new RegExp(pattern, "i"));
  const candidates = [
    ...runtime.console.filter((entry) => entry.level === "error"),
    ...runtime.exceptions,
    ...runtime.networkFailures,
    ...runtime.httpErrors,
  ];
  const errors = [];
  const ignored = [];
  for (const entry of candidates) {
    const target = matchers.some((matcher) => matcher.test(JSON.stringify(entry)))
      ? ignored
      : errors;
    target.push(entry);
  }
  return { errors, ignored };
}

