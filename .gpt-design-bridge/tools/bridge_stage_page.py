"""Engineering-owned HTML shell for a no-build travelling designer stage."""

from __future__ import annotations

import html
import json

from bridge_core import BridgeError, safe_relative_path
from bridge_surface import REGION_BEGIN, REGION_END, REGION_PLACEHOLDER


STYLE_SOURCE_ROOT = "app/src/styles/"


def _script_json(value: str) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _stylesheet_links(stylesheet_paths: list[str]) -> str:
    if not stylesheet_paths:
        raise BridgeError("travelling stage requires at least one stylesheet")
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source_path in stylesheet_paths:
        normalized = safe_relative_path(source_path, label="designer stylesheet")
        folded = normalized.casefold()
        if folded in seen:
            raise BridgeError(f"travelling stage stylesheet is duplicated: {normalized}")
        seen.add(folded)
        if not normalized.startswith(STYLE_SOURCE_ROOT) or not normalized.endswith(".css"):
            raise BridgeError(
                "travelling stage stylesheet must be a .css file beneath "
                f"{STYLE_SOURCE_ROOT}: {normalized}"
            )
        relative = normalized.removeprefix(STYLE_SOURCE_ROOT)
        href = html.escape(f"./styles/{relative}", quote=True)
        links.append((folded, f'  <link rel="stylesheet" href="{href}">'))
    return "\n".join(link for _path, link in sorted(links))


def render_stage_page(
    project_name: str,
    stage_stamp: str,
    prelude: str,
    stylesheet_paths: list[str],
) -> bytes:
    title = html.escape(project_name, quote=True)
    project_json = _script_json(project_name)
    stylesheet_links = _stylesheet_links(stylesheet_paths)
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="gdb-stage-stamp" content="{stage_stamp}">
  <title>{title} — travelling designer stage</title>
{stylesheet_links}
  <link rel="stylesheet" href="./assets/pickers/icon-picker/icon-picker.css">
  <style>
    .gdb-runtime-bar {{ align-items:center; background:#111827; color:#f9fafb; display:flex;
      flex-wrap:wrap; font:600 13px/1.4 system-ui,sans-serif; gap:10px; padding:10px 16px; }}
    .gdb-runtime-bar button {{ background:#fff; border:1px solid #d1d5db; border-radius:6px;
      color:#111827; cursor:pointer; font:inherit; padding:7px 10px; }}
    .gdb-runtime-bar button:focus-visible, .gdb-reset-dialog button:focus-visible {{
      box-shadow:var(--focus); outline:none; }}
    .gdb-runtime-status {{ background:#7f1d1d; border-radius:5px; padding:7px 10px; }}
    .gdb-boot-spinner {{ align-items:center; display:inline-flex; height:20px; width:20px; }}
    .gdb-boot-spinner svg {{ animation:gdb-spin 900ms linear infinite; height:20px; width:20px; }}
    @keyframes gdb-spin {{ to {{ transform:rotate(360deg); }} }}
    @media (prefers-reduced-motion:reduce) {{
      .gdb-boot-spinner svg {{ animation-duration:1800ms; }}
    }}
    .gdb-boot-error {{ background:#7f1d1d; color:#fff; margin:0; padding:16px;
      white-space:pre-wrap; }}
    .gdb-reset-dialog {{ background:var(--surface-raised); border:1px solid var(--line);
      border-radius:var(--radius-medium); box-shadow:var(--shadow); color:var(--ink-strong);
      font:400 16px/1.5 var(--font-sans); max-width:min(440px,calc(100vw - 32px));
      padding:0; width:100%; }}
    .gdb-reset-dialog::backdrop {{
      background:color-mix(in srgb,var(--ink-strong) 72%,transparent); }}
    .gdb-reset-dialog form {{ margin:0; padding:24px; }}
    .gdb-reset-dialog h2 {{ font-size:21px; line-height:1.25; margin:0 0 10px; }}
    .gdb-reset-dialog p {{ margin:0; }}
    .gdb-reset-progress {{ color:var(--danger); font-weight:650; min-height:24px;
      padding-top:12px; }}
    .gdb-reset-actions {{ display:flex; flex-wrap:wrap; gap:10px;
      justify-content:flex-end; margin-top:20px; }}
    .gdb-reset-actions button {{ background:var(--surface); border:1px solid var(--line-strong);
      border-radius:var(--radius-small); color:var(--ink-strong); cursor:pointer;
      font:600 14px/1.4 var(--font-sans); padding:9px 13px; }}
    .gdb-reset-actions .gdb-reset-danger {{ background:var(--danger);
      border-color:var(--danger); color:var(--surface-raised); }}
  </style>
</head>
<body>
  <aside class="gdb-runtime-bar" aria-label="Travelling data controls">
    <span>Local travelling SQLite</span>
    <span>Picker: 30 Lucide + 30 Twemoji samples, plus used assets</span>
    <span id="gdb-boot-spinner" class="gdb-boot-spinner" aria-hidden="true"></span>
    <span id="gdb-storage-status" class="gdb-runtime-status" role="status">
      Opening local data…
    </span>
    <button id="gdb-export-data" type="button">Export design data</button>
    <button id="gdb-reset-data" type="button">Reset SPECIMEN data</button>
  </aside>
  <pre id="gdb-boot-error" class="gdb-boot-error" role="alert" hidden></pre>
  <dialog id="gdb-reset-dialog" class="gdb-reset-dialog" aria-modal="true"
    aria-labelledby="gdb-reset-title" aria-describedby="gdb-reset-description">
    <form id="gdb-reset-form">
      <h2 id="gdb-reset-title">Reset SPECIMEN data?</h2>
      <p id="gdb-reset-description">
        This permanently replaces local travelling-stage changes with the marked SPECIMEN
        values. It never changes production data.
      </p>
      <p id="gdb-reset-progress" class="gdb-reset-progress" role="status"
        aria-live="polite"></p>
      <div class="gdb-reset-actions">
        <button id="gdb-reset-cancel" type="button">Cancel</button>
        <button id="gdb-reset-confirm" class="gdb-reset-danger" type="submit">
          Reset SPECIMEN data
        </button>
      </div>
    </form>
  </dialog>
  <div id="root"></div>

  <script src="./vendor/react.production.min.js"></script>
  <script src="./vendor/react-dom.production.min.js"></script>
  <script>globalThis.react = globalThis.React;</script>
  <script src="./vendor/lucide-react.min.js"></script>
  <script src="./runtime/stage-data.js"></script>
  <script src="./runtime/gdb-runtime.js"></script>
  <script src="./vendor/babel.min.js"></script>
  <script type="module">
    import * as picker from "./assets/pickers/icon-picker/src/index.js";
    globalThis.GdbDesignPicker = Object.freeze({{ ...picker }});
    document.dispatchEvent(new CustomEvent("gdb-picker-ready"));
  </script>

  <script>
  (() => {{
    "use strict";
    const errorNode = document.getElementById("gdb-boot-error");
    const statusNode = document.getElementById("gdb-storage-status");
    const spinnerNode = document.getElementById("gdb-boot-spinner");
    const resetTrigger = document.getElementById("gdb-reset-data");
    const resetDialog = document.getElementById("gdb-reset-dialog");
    const resetForm = document.getElementById("gdb-reset-form");
    const resetCancel = document.getElementById("gdb-reset-cancel");
    const resetConfirm = document.getElementById("gdb-reset-confirm");
    const resetProgress = document.getElementById("gdb-reset-progress");
    let resetInProgress = false;
    const bootStarted = performance.now();
    const bootWarnAfter = Number.isFinite(globalThis.__GDB_BOOT_WARN_AFTER_MS__)
      ? Math.max(1000, globalThis.__GDB_BOOT_WARN_AFTER_MS__) : 10000;
    const bootProgressEvery = Number.isFinite(globalThis.__GDB_BOOT_PROGRESS_INTERVAL_MS__)
      ? Math.max(1000, globalThis.__GDB_BOOT_PROGRESS_INTERVAL_MS__) : 3000;
    let bootPhase = "loading local runtime";
    let bootProgressTimer = null;
    function reportBootProgress() {{
      const elapsed = Math.max(1, Math.round((performance.now() - bootStarted) / 1000));
      statusNode.hidden = false;
      statusNode.textContent = `Still opening… ${{elapsed}}s elapsed; ${{bootPhase}}.`;
      bootProgressTimer = setTimeout(reportBootProgress, bootProgressEvery);
    }}
    let bootTimer = setTimeout(reportBootProgress, bootWarnAfter);
    function describe(reason) {{
      if (reason instanceof Error) return `${{reason.name}}: ${{reason.message}}`;
      return `Non-Error failure: ${{String(reason)}}`;
    }}
    function showError(reason) {{
      clearTimeout(bootTimer);
      clearTimeout(bootProgressTimer);
      spinnerNode.hidden = true;
      errorNode.hidden = false;
      errorNode.textContent = describe(reason);
    }}
    globalThis.__GDB_BOOT_OK__ = () => {{
      clearTimeout(bootTimer);
      clearTimeout(bootProgressTimer);
      bootTimer = null;
      bootProgressTimer = null;
      spinnerNode.hidden = true;
    }};
    globalThis.__GDB_BOOT_PROGRESS__ = (phase) => {{
      if (typeof phase === "string" && phase.trim()) bootPhase = phase.trim();
    }};
    globalThis.__GDB_STORAGE_STATUS__ = (status) => {{
      if (!status || typeof status !== "object") {{
        showError(new Error("STORAGE STATUS: travelling runtime returned an invalid status."));
        return;
      }}
      if (status.mode === "degraded") {{
        statusNode.hidden = false;
        statusNode.textContent = status.reason || "Durable browser storage is unavailable.";
      }} else {{
        statusNode.hidden = true;
        statusNode.textContent = "Local data is durable in this browser.";
      }}
    }};
    addEventListener("error", (event) => showError(event.error || event.message));
    addEventListener("unhandledrejection", (event) => showError(event.reason));
    document.getElementById("gdb-export-data").addEventListener("click", () => {{
      try {{
        const bytes = globalThis.__GDB_TRAVELLING__.exportDb();
        const url = URL.createObjectURL(new Blob([bytes], {{ type: "application/vnd.sqlite3" }}));
        const link = document.createElement("a");
        link.href = url;
        link.download = "travelling-data-export.sqlite3";
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 0);
      }} catch (error) {{
        showError(error);
        throw error;
      }}
    }});
    resetTrigger.addEventListener("click", () => {{
      try {{
        if (typeof resetDialog.showModal !== "function") {{
          throw new Error("RESET DIALOG: this browser does not support modal dialogs.");
        }}
        resetProgress.textContent = "";
        if (!resetDialog.open) resetDialog.showModal();
        resetCancel.focus();
      }} catch (error) {{
        showError(error);
        throw error;
      }}
    }});
    resetCancel.addEventListener("click", () => {{
      if (!resetInProgress) resetDialog.close("cancel");
    }});
    resetDialog.addEventListener("cancel", (event) => {{
      if (resetInProgress) event.preventDefault();
    }});
    resetDialog.addEventListener("close", () => {{
      if (!resetInProgress) resetTrigger.focus();
    }});
    resetForm.addEventListener("submit", async (event) => {{
      event.preventDefault();
      if (resetInProgress) return;
      resetInProgress = true;
      resetForm.setAttribute("aria-busy", "true");
      resetCancel.disabled = true;
      resetConfirm.disabled = true;
      resetConfirm.textContent = "Resetting…";
      resetProgress.textContent = "Resetting local SPECIMEN data…";
      try {{
        if (typeof globalThis.__GDB_TRAVELLING__?.resetToSpecimen !== "function") {{
          throw new Error("RESET CONTRACT: travelling reset is unavailable.");
        }}
        await globalThis.__GDB_TRAVELLING__.resetToSpecimen();
        resetProgress.textContent = "Reset complete. Reloading…";
        location.reload();
      }} catch (error) {{
        resetInProgress = false;
        resetForm.removeAttribute("aria-busy");
        resetCancel.disabled = false;
        resetConfirm.disabled = false;
        resetConfirm.textContent = "Reset SPECIMEN data";
        resetProgress.textContent = `Reset failed. ${{describe(error)}}`;
        showError(error);
        resetConfirm.focus();
        throw error;
      }}
    }});
    globalThis.__GDB_SHOW_ERROR__ = showError;
    if (globalThis.React && globalThis.ReactDOM && globalThis.LucideReact?.LoaderCircle) {{
      ReactDOM.createRoot(spinnerNode).render(
        React.createElement(LucideReact.LoaderCircle, {{ "aria-hidden": true, size: 20 }})
      );
    }} else {{
      showError(new Error("BOOT CONTRACT: Lucide LoaderCircle is unavailable."));
    }}
  }})();
  </script>

{REGION_BEGIN}{REGION_PLACEHOLDER}{REGION_END}

  <script id="gdb-mount-source" type="text/plain">
(async () => {{
  if (!globalThis.React || !globalThis.ReactDOM || !globalThis.LucideReact) {{
    throw new Error("BOOT CONTRACT: React, ReactDOM, or LucideReact is missing.");
  }}
  globalThis.__GDB_BOOT_PROGRESS__("opening travelling SQLite");
  const bridge = await globalThis.__GDB_TRAVELLING__.open({{
    onStatusChange: globalThis.__GDB_STORAGE_STATUS__,
  }});
  globalThis.__GDB_ACTIVE_BRIDGE__ = bridge;
  globalThis.__GDB_BOOT_PROGRESS__("mounting the accepted React interface");
  ReactDOM.createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <App projectName={project_json} socket={{bridge.socket}} onMounted={{globalThis.__GDB_BOOT_OK__}} />
    </React.StrictMode>,
  );
}})().catch((error) => {{
  globalThis.__GDB_SHOW_ERROR__(error);
  setTimeout(() => {{ throw error; }}, 0);
}});
  </script>
  <script>
  (() => {{
    "use strict";
    try {{
      if (!globalThis.Babel || typeof globalThis.Babel.transform !== "function") {{
        throw new Error("BOOT CONTRACT: Babel standalone is missing.");
      }}
      const blocks = [...document.querySelectorAll(
        'script[type="text/babel"][data-gdb-source][data-presets="react"]'
      )];
      if (blocks.length === 0) throw new Error("BOOT CONTRACT: no designer source blocks found.");
      globalThis.__GDB_BOOT_PROGRESS__("compiling pinned local JSX");
      const compiled = blocks.map((block) => {{
        const filename = block.getAttribute("data-gdb-source");
        block.type = "application/gdb-processed";
        return globalThis.Babel.transform(block.textContent, {{
          filename,
          presets: ["react"],
          sourceType: "script",
        }}).code;
      }});
      compiled.push({_script_json(prelude)});
      compiled.push(globalThis.Babel.transform(
        document.getElementById("gdb-mount-source").textContent,
        {{ filename: "gdb-mount.jsx", presets: ["react"], sourceType: "script" }},
      ).code);
      const executable = document.createElement("script");
      executable.textContent = compiled.join("\\n");
      document.head.appendChild(executable);
    }} catch (error) {{
      globalThis.__GDB_SHOW_ERROR__(error);
      setTimeout(() => {{ throw error; }}, 0);
    }}
  }})();
  </script>
</body>
</html>
"""
    return page.encode("utf-8")
