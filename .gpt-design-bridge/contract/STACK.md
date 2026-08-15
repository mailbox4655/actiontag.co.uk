# Prescribed application and travelling-drop stack

This is a capability contract, not a technology menu. The designer cannot run a
build, connect to the production server, receive secrets, or be expected to understand
an arbitrary repository. The stack exists to make the application usable and editable
under those limits.

## 1. Two targets, one source

The source tree produces:

| Target | Purpose | Runtime |
|---|---|---|
| Production app | Real users, auth, durable data, mail, deployment | Browser UI plus Node server |
| Travelling drop | External design work | One offline-capable browser package |

The drop is a second output, never a second codebase. Designer-owned JSX/CSS is lifted
from the same source that production builds. Engineering-owned runtime code becomes a
single IIFE bundle. The reversible transform is proved byte-identical.

Production interface source is React JSX and CSS, rendered by React and ReactDOM.
Vite is the canonical scaffold and the preferred development server/mature production
builder. A deliberately lean project may use esbuild after an explicit foundation
decision, but that does not change the designer-return contract. Babel Standalone is
pinned and local in the courier only; it compiles editable browser JSX for the
designer and is not the production runtime/build architecture.

Do not introduce Vue. Do not translate an accepted designer return into Vue, Angular,
PHP templates, or another component system. Accepted React JSX/CSS is executable
source. In a brownfield application with another view framework, preserve both sides
with a React island: existing route → empty host element → mount accepted React screen
unchanged → inject data/actions through props or a narrow adapter → unmount on route
destruction. Routing, authentication, API translation, navigation, and persistence
live outside the sealed UI files.

### 1.1. Product provenance is part of the stack

“Two targets, one source” is a mechanically checked graph, not a prose promise. Before
`round-open`, a verified provenance record names:

- authoritative production entrypoints;
- exact travelling-source entrypoints;
- every designer-owned surface root and source file;
- automatic local import/HTML/CSS edges;
- any explicit brownfield adapter edge with real source anchor, forward transform,
  inverse wholesale-adoption path, and `preserves_topology: true`;
- compatibility records whose bytes cannot be rewritten during that task; and
- a production-derived capability/state manifest from owned visible Chrome.

Production and travelling entrypoints must both reach every designer-surface file.
A separately authored “designer mirror”, component gallery, CSS illustration, or
parallel mock fails even when it renders, round-trips byte-for-byte, and passes tests
written only against itself.

Raw fixture **data** may be replaced with deterministic marked SPECIMEN records.
Fixture **topology** may not: a real map remains a real locally driven map with its
pan/zoom/draw/layer interactions; a workflow retains its controls, nested states, and
exports. A simplification requires an exact prompt-scoped owner capability ruling,
not an architectural convenience or edited compatibility explanation.

## 2. Source layout

The generic layout is:

```text
app/
  src/
    api/                 pure route table, request/response types, socket switch
    engine/              domain rules and migrations
    state/               state port and persistence adapters
    ui/                  designer-owned JSX and UI helpers
    styles/              designer-owned tokens, shell, and view styles
    package-runtime.js   drop-only IIFE entry
  public/                vendored/static assets
  scripts/               route, seed, package, return, and proof tools
server/
  src/                   Hono adapter, auth, database adapter, Postmark, health
  migrations/
shared/
  contracts/             schemas used by both sockets
```

The project may add domain folders, but it preserves these boundaries. A view never
imports the production database or mail client. A route handler never imports UI.

The application scaffold also expands a provenance-checked owner-supplied picker core
and the complete approved Lucide and Twemoji SVG sets under
`public/assets/pickers/icon-picker/`. The archive stays inside the project-local
foundation so a fresh machine needs no knowledge of its source project. Its compact
manifest binds the archive hash, full inventory, selected public inventory, library
counts, versions, required files, exclusions, and license material.

The raw legacy demo and README remain in the internal provenance archive but do not
enter the public application. Product code uses the project adapter described by
`docs/ICON-PICKER-TEMPLATE.md`; the vendored source is a starting component, not an
exception to the numbered owner rules.

The foundation also carries the owner-supplied color interaction as
`app/src/ui/ColorPicker.jsx` and `app/src/styles/color-picker.css`. It is a controlled
product component, not a Tailwind runtime dependency: 22 local family swatches expose
their 500 shade normally; primary selection immediately switches an always-present
horizontal tray to all eleven 50–950 shades for that family, while the context action
can switch the tray without changing the value or resizing its outer layer; custom
hex values pass exact validation; and opacity remains an explicit 0–100 field. Read
`docs/COLOR-PICKER-TEMPLATE.md` before integration; the host owns persistence.

## 3. Designer runtime versions

The proven starting set is:

| Package | Version contract | Role |
|---|---|---|
| `react` | `18.3.1` | UMD global and production source dependency |
| `react-dom` | `18.3.1` | UMD global with `createRoot` |
| `lucide-react` | `0.525.0` | generated IIFE global containing only UI-imported icons |
| `@babel/standalone` | `7.29.7`, pinned to major 7 | browser compilation of editable JSX |
| `@sqlite.org/sqlite-wasm` | `3.53.0-build1` | travelling database engine |
| `vite` | `6.4.3` | production build and programmatic IIFE build |
| `@vitejs/plugin-react` | `4.6.0` | source authoring build |

Do not upgrade or substitute these drop dependencies as routine maintenance. A change
requires a measured compatibility task that rebuilds and re-proves every context.

Fonts and brand art are project-design choices, not capability constraints. Once
ratified, they are version-pinned and vendored. A drop never depends on a font CDN.

The canonical icon and interface laws live in `OWNER-RULES-DESIGN.md`.
`lucide-react` here names the measured subset packaging used by the travelling target;
it is an implementation of that owner rulebook, not a second source of product rules.

## 4. Why Babel stays on major 7

Babel standalone 7 emits `React.createElement` for the classic React preset. Babel 8
can emit a bare import from `react/jsx-runtime`. A classic script loaded from a
double-clicked page cannot resolve that package import, so the entire designer surface
can become blank.

Every package build executes the vendored Babel file as a classic script and compiles
a fixture. It fails on:

- any throw;
- `import` in the output;
- `react/jsx-runtime`;
- absence of a callable classic output.

Pinning the package without running this probe is insufficient.

## 5. Load order

The generated page loads in this order:

```html
<link rel="icon" href="runtime/favicon.svg">
<link rel="stylesheet" href="runtime/assets/app.css">
<link rel="stylesheet" href="styles/tokens.css">
<link rel="stylesheet" href="styles/shell.css">
<link rel="stylesheet" href="styles/views.css">

<script src="vendor/react.production.min.js"></script>
<script src="vendor/react-dom.production.min.js"></script>
<script>window.react = window.React;</script>
<script src="vendor/lucide-react.min.js"></script>
<script src="db/app-db-image.js"></script>
<script src="runtime/sqlite3-wasm-image.js"></script>
<script src="runtime/package-runtime.js"></script>
<script src="vendor/babel.min.js"></script>

<!-- one text/babel block per designer file -->
<!-- the mount block is last -->
```

This order is load-bearing:

- the Lucide UMD factory reads lowercase `window.react`, which React does not publish;
- the database and WASM byte images exist before the runtime reads them;
- the runtime resolves icon names after Lucide exists;
- all view declarations exist before the mount executes.

A probe proves the alias is necessary and sufficient. Do not replace this with a
comment saying the order matters.

## 6. Engineering-owned IIFE

The programmatic Vite library build bundles:

- engine and migration code;
- the route table and direct-call adapter;
- state port and travelling persistence;
- non-view helpers;
- asset indexing that relies on build-time features.

React, ReactDOM, and Lucide are external and mapped to their globals. There is one
inline programmatic configuration, not a second long-lived Vite config that can drift.

Bundler-only constructs such as `import.meta.glob` remain in the IIFE. An inline
designer block may not contain them.

## 7. Designer-owned inline files

Each declared `.jsx` designer file becomes one block:

```text
//@embed-begin app/src/ui/Example.jsx
<reversibly transformed source>
//@embed-end app/src/ui/Example.jsx
```

The forward transform has a closed rule set:

- preserve leading imports as marked inert plumbing;
- preserve exports through marked reversible forms;
- refuse input that already contains either marker;
- scan after conversion and refuse any surviving bare import/export;
- validate every marker path before reading or writing.

The inverse restores the original bytes. A new, safe declared path creates a new
designer source file. An unknown syntax or unsafe path fails loudly.

Every outbound screen also declares `exact`, `characterized`, or `reference` in
`ADOPTION-MODES.json`. `exact` is the default and seals returned JSX, CSS, assets, and
relevant markup as the implementation baseline. `characterized` seals fields and
behavior while permitting a separately approved visual treatment. `reference`
installs no source. A missing declaration never implies permission to reinterpret.

## 8. Globals contract

An inline view may resolve a name from exactly:

1. the framework/Lucide UMD globals;
2. a symbol deliberately published by the runtime IIFE;
3. a top-level declaration in another inline designer block.

The builder derives all three sets and fails with the unresolved identifier. Checking
only the IIFE would falsely reject legitimate view-to-view references.

## 9. Database and WASM images

On opaque `file://` origins, JavaScript cannot fetch the `.wasm` or `.db` files. Both
binaries are therefore base64-encoded into ordinary JavaScript assignments:

```js
window.__APP_SQLITE_WASM_BASE64__ = "...";
window.__APP_DB_BASE64__ = "...";
```

The package also carries the real `.db` file for served mode. The builder compares the
decoded bytes and real artifacts by SHA-256.

The travelling store:

- rehydrates before first paint;
- seeds only when absent;
- namespaces IndexedDB with a content-derived build stamp including the data-image
  hash;
- mirrors database bytes after writes;
- migrates rather than clobbers;
- visibly reports when persistence is unavailable and memory-only;
- quarantines and surfaces corrupt stored bytes instead of reseeding.

`file://` memory-only operation is an explicit degradation, not a hidden fallback.

## 10. Production server

The production server uses a supported Node LTS, TypeScript, Hono, and Zod. Postmark
is server-only. The selected database adapter is:

- local SQLite under the database policy; or
- PostgreSQL under the same route contract.

Production SQLite never lives on a network filesystem.

The HTTP adapter decodes a request into the route table's request object, calls the
same handler used by the travelling direct socket, then encodes the result. A corpus
test compares normalized direct and real-HTTP results.

Production dependency versions are locked in the generated package lock. Node and the
linked SQLite version are checked at boot and deployment; a package-lock claim does
not prove the server binary.

## 11. Assets

All travelling references are portable-relative and resolve from the file that
contains the reference. Host-absolute `/assets/...` paths are prohibited because they
break on deep preview subpaths.

The engineering project keeps all 1,952 Lucide and 3,720 Twemoji SVGs locally. A
travelling stage carries every mechanically discovered or explicitly declared used
asset plus exactly 30 Lucide and 30 Twemoji picker samples. The generated
`TRAVELLING-ASSETS.json` binds reasons, paths, sizes, and hashes. The full packs never
enter the drop, and the limited picker never claims to be a full catalog.

The asset graph scans actual references:

- HTML `src` and `href`;
- CSS `url()`;
- bundled `new URL(relativeName, base)` emissions, including names with subdirectories.

It does not reject every `http`-looking string inside vendored code; libraries contain
namespaces and documentation strings that make no request. Runtime network evidence
is the final proof.

Vendored trailing `sourceMappingURL` comments are removed only when they occupy the
expected final line. The build reports every removal and refuses an unexpected shape.

## 12. Deterministic package

The drop includes:

```text
index.html
vendor/
runtime/
styles/
db/
design-of-record/
BASELINE-MANIFEST.json
OFF-LIMITS.json
OFF-LIMITS.md
contract-additions.schema.json
contract-additions.json
DROP_README.md
RETURNING-THIS-DROP.md
WHAT-ENGINEERING-CHANGED.md
serve-local.mjs
```

The build reads no clock. Manifest values derive from content. ZIP entries are sorted
by UTF-8 path and use fixed timestamps, permissions, no comments, and no host metadata.
Two builds under the same locked toolchain must have identical archive bytes.
Uncompressed per-file manifest hashes remain the cross-toolchain comparison surface.

The design-of-record is a verbatim reference surface and is not loaded by the app. If
its own standalone canvas has external references, the portability report excludes it
by a named declaration, never a silent skip.

## 13. Browser proof

The exact built artifact is opened with the project-local
`.gpt-design-bridge/tools/controlled_chrome.mjs`, which launches visible pinned Chrome
for Testing as its own child with a fresh owned profile. It never opens or automates
the owner's Chrome, uses the in-app browser, imports cookies, or disables file
security. The controlled browser proves:

1. `file://`;
2. a deep HTTP subpath with correct content types and `nosniff`;
3. a deliberately broken mount that proves the boot self-check becomes visible;
4. home mode without a backend, proving a real request occurs and fails loudly.

Interaction coverage uses native hit testing. It opens nested drawers, menus, dialogs,
tabs, popovers, retries, and keyboard paths. Querying the DOM for a present node is not
proof that a user can reach it.

The in-app browser is not an acceptable substitute for this gate.

The four contexts are a required subset, not a maximum. Supplemental viewports and
flows are valid when each has a summary and retained artifact. Exact adoption later
combines returned-source hashes, DOM-structure comparison, screenshots at agreed
widths, and interaction traces; approximate visual similarity alone is insufficient.

## 14. Build and artifact lifecycle

The scaffold build removes stale `dist/server` immediately before replacement and
lets Vite replace its own `dist/client` output. It never versions intermediate build
directories merely because another build starts. Reproducible artifacts outside that
single build path are governed by tracked `.gpt-artifact-lifecycle.json` and the
project-local `artifact_lifecycle.py` report → preview → hash-bound prune sequence.

Only explicitly registered, marked artifacts whose source commit and rebuild inputs
are proven—and whose source commit exists on the configured remote when remote
reconstruction is required—may be pruned. Current/previous releases, databases,
uploads, secrets, designer couriers/returns, travelling database escrow, adoption
baselines, and required evidence are protected. Unknown or unmarked content is
retained with a precise reason.
