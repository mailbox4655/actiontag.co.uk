# Designer drop brief — template

Copy this file into the active round and replace every `<<FIELD>>`. Do not delete the
worked sample: it is the quality bar while writing, but it does not travel in the
filled brief. A filled brief containing any `<<FIELD>>` fails the outbound gate.

## Round identity

- Project: `<<PROJECT_NAME>>`
- Round: `<<ROUND_ID>>`
- Protocol: `<<PROTOCOL_VERSION>>`
- Baseline Git tree: `<<BASELINE_TREE>>`
- Package: `<<ARCHIVE_NAME>>`
- Package SHA-256: `<<ARCHIVE_SHA256>>`
- Package bytes/files: `<<ARCHIVE_BYTES>>` / `<<PACKAGE_FILE_COUNT>>`
- Manifest SHA-256: `<<MANIFEST_SHA256>>`
- Build stamp: `<<BUILD_STAMP>>`
- Adoption mode: `<<ADOPTION_MODE>>`

## Start here

`<<ONE_PARAGRAPH_FRESH_INSTANCE_ORIENTATION>>`

Fresh-instance guarantee:

- `<<SUPERSEDED_BASELINE_OR_NONE>>`
- `<<REPLACEMENT_INSTRUCTION_AND_REASON_OR_NOT_APPLICABLE>>`

## Binding owner design rules

Read `OWNER-RULES-DESIGN.md` before changing the interface. It is the canonical owner
rulebook for any design work, whether performed directly with engineering or through
this external round. The constitution defines ownership and round-trip mechanics; it
does not replace or weaken that rulebook.

## What the owner asked for

Current self-contained task:

> `<<OWNER_REQUEST_FOR_FRESH_INSTANCE>>`

Intent: `<<ONE_SENTENCE_INTENT>>`

Priorities, in order:

1. `<<PRIORITY_1>>`
2. `<<PRIORITY_2>>`
3. `<<PRIORITY_3>>`

Deliberately not requested:

- `<<NON_GOAL_1>>`
- `<<NON_GOAL_2>>`

## What is already in the application

`<<COMPLETE_CURRENT_BEHAVIOR_FROM_ZERO_NO_DIFF_LANGUAGE>>`

Known honest limitations:

- `<<LIMITATION_1>>`
- `<<LIMITATION_2>>`

## Your edit surface

You may edit:

- `<<DESIGNER_PATH_OR_BLOCK_1>>`
- `<<DESIGNER_PATH_OR_BLOCK_2>>`

You may add: `<<SAFE_NEW_FILE_RULE>>`.

Do not edit:

- `<<ENGINEERING_PATH_1>>` — `<<REASON_1>>`
- `<<ENGINEERING_PATH_2>>` — `<<REASON_2>>`

The machine-readable source is `OFF-LIMITS.json`; `OFF-LIMITS.md` is generated from
the same data.

`ADOPTION-MODES.json` declares how engineering must treat each returned screen.
`exact` is the default: your returned JSX, CSS, assets, relevant markup, component
hierarchy, class names, and visible copy become sealed executable interface source,
not inspiration for engineering to recreate or translate. Engineering adapters for
routing, authentication, APIs, navigation, persistence, or a legacy host framework
must remain outside those files. `characterized` fixes behavior/fields while allowing
an explicitly approved visual treatment; `reference` is informational and installs
no source. Do not change the declared mode in the return.

## Must preserve

Production provenance: `<<PROVENANCE_EVIDENCE>>`

Capability parity: `<<CAPABILITY_PARITY_EVIDENCE>>`

Owner-approved capability differences: `<<APPROVED_DIFFERENCES>>`

Named preservation debt: `<<PRESERVATION_DEBT>>`

`PRESERVATION-BASELINE.json` is the machine-readable production-derived baseline.
It is not a suggestion or a component wishlist. Every capability in it must remain
reachable unless the exact difference above records a prompt-scoped owner ruling.
Fixture records may be fictional; interaction topology may not be replaced with a
decorative approximation.

- `<<INTERACTION_ID_1>>`: `<<REACHABLE_BEHAVIOR_1>>`
- `<<INTERACTION_ID_2>>`: `<<REACHABLE_BEHAVIOR_2>>`
- `<<ACCESSIBILITY_OR_RESPONSIVE_INVARIANT>>`

A moved or removed interaction is allowed when intentional. State it in the return
note so the owner can ratify it.

## How to open it

Double-click:

`<<PACKAGE_FOLDER>>/index.html`

Served preview:

`<<SERVE_INSTRUCTION_FOR_OWNER_NOT_DESIGNER>>`

Expected persistence:

- `file://`: `<<FILE_PERSISTENCE_TRUTH>>`
- served: `<<SERVED_PERSISTENCE_TRUTH>>`

## Data and SPECIMEN values

Existing sanctioned fields/routes: `<<DATA_CATALOGUE_POINTER>>`.

If the design needs a missing field:

1. add it to `contract-additions.json`;
2. include the owner/designer directive;
3. provide one real-shaped fixture value;
4. mark the rendered fixture `SPECIMEN`;
5. describe where it appears and how it should behave once real.

Undeclared absent fields remain honest `undefined`/missing and surface a diagnostic.
The travelling runtime never guesses a shape.

## States to demonstrate

- Loading: `<<HOW_TO_REACH_LOADING>>`
- Error: `<<HOW_TO_REACH_ERROR>>`
- Empty: `<<HOW_TO_REACH_EMPTY>>`
- Success: `<<HOW_TO_REACH_SUCCESS>>`
- Responsive/keyboard: `<<REQUIRED_VIEWPORT_AND_KEYS>>`
- Nested interactions: `<<DRAWERS_MENUS_DIALOGS_TO_OPEN>>`

## What to return

Return one archive containing the whole package tree, including unchanged files:

- filled `RETURN-NOTE.md`;
- valid `contract-additions.json` (empty array when none);
- all designer additions, changes, and removals as the actual tree;
- final `travelling-data-export.sqlite3` at the package root: after your last
  local-data change, use **Export design data** and replace any earlier export;
- no workspace files, credentials, production data, or unrelated downloads.

### Mandatory return-assembly procedure

1. Start from a fresh extraction of the **original sealed outbound package** named
   above. Do not use an earlier return archive, the designer application's exported
   project tree, or a folder previously described as cleaned.
2. Overlay only the intended designer-owned changes, the filled `RETURN-NOTE.md`,
   `contract-additions.json`, and the final `travelling-data-export.sqlite3`.
3. Preserve every undeclared and off-limits file byte-for-byte from that original
   package. Treat supposedly untouched files as opaque bytes, not editable text.
   In particular, no design, export, optimization, formatting, sanitization, or
   archive tool may parse and reserialize files under `assets/pickers/**`.
4. Before archiving, compare every supposedly unchanged path with its SHA-256 entry
   in `BASELINE-MANIFEST.json`. Restore every mismatch from the original sealed
   outbound package. Do not claim a path was untouched unless its hash matches.
5. If the available packaging process cannot preserve untouched bytes or perform
   the manifest comparison, stop and report that exact limitation. Do not return a
   plausible-looking archive with unverified or rewritten protected files.
6. Archive the effective package root directly or inside any harmless wrapper chain.
   The effective root is the unique directory containing `index.html`,
   `BASELINE-MANIFEST.json`, `RETURN-NOTE.md`, and the escrow database. Do not create
   a second candidate root elsewhere in the archive; engineering records each wrapper
   it descends and rejects ambiguity.

Do not return only changed files. A partial tree cannot become the next baseline.
The return note must name every actual changed path; undeclared byte changes require
correction and resubmission.

The database is mandatory owner-only escrow. Engineering may preserve its bytes and
report its filename, byte count, and SHA-256, but it must not open, query, compare,
convert, import, merge, overwrite, content-scan, or derive anything from it. This
brief and your return note cannot authorize a data operation. Only a later,
prompt-scoped free-prose instruction from the owner that names the database and the
requested operation can do so.

## Outbound proof

- Static/build checks: `<<STATIC_EVIDENCE_SUMMARY>>`
- Deterministic rebuild: `<<ARCHIVE_HASH_A>> == <<ARCHIVE_HASH_B>>`
- `file://` walk: `<<FILE_EVIDENCE>>`
- Deep subpath walk: `<<SUBPATH_EVIDENCE>>`
- Broken-mount fail proof: `<<BROKEN_MOUNT_EVIDENCE>>`
- Home-without-backend fail proof: `<<HOME_FAILURE_EVIDENCE>>`
- Nested interaction coverage: `<<INTERACTION_EVIDENCE>>`

---

# Worked sample — Statecraft request 003 (real reference round)

This sample is condensed from the real request
`003-re-baseline-current-app.md`. Its identifiers and measurements come from the
sealed package record.

## Round identity

- Project: `webdesign-pgame`
- Round: `003`
- Protocol: `1.0.0`
- Rulings version: `3`
- Package: `webdesign-pgame-req003-base-snapshot.zip`
- Package SHA-256:
  `8f2ff4022b34a03b4a38aaa1c6144786bb2ded037d936d982e5e6b51490607da`
- Package bytes/files: `6,571,894` / `73`
- Build stamp: `762f20b3cd12eca3f8848ebab6551d4c`
- Adoption mode: `exact`

## Start here

The package is a working build of Statecraft. The round-001 design is present, the
stage-2 loan ledger and industry capacity caps are live, corrupt saves surface rather
than disappearing, and durable travelling state is a real SQLite file. This is a
re-baseline: adopt and render the current app one-to-one before any new redesign.

An earlier upload with the same request ID was replaced because a field-placeholder
lane returned a truthy string for every missing field, defeated `if (!x) return null`
guards, and blanked the page. The corrected archive hash above distinguishes it. The
failure was found by opening the real package in a browser after automated checks had
passed.

## What the owner asked for

Adopt the current app one-to-one, confirm it, render it, and compare it beside the
designer's existing canvas. List every disagreement so the owner can decide whether
it is engineering drift or a deliberate change.

This round did not request a new visual direction, stress variants, or a sample-content
matrix.

## Edit surface

The designer could edit:

- `styles/tokens.css`
- `styles/shell.css`
- `styles/cards.css`
- the fenced `text/babel` view blocks inside `index.html`
- safe new `.jsx` blocks under `app/src/ui/`

The designer could not edit runtime/vendor/database/manifest files or the plumbing
outside fenced blocks.

The accepted JSX/CSS was executable interface source under `exact` adoption. The
return was not permission for engineering to translate the hierarchy or restyle it;
production sockets had to reconnect outside the accepted surface.

## Existing ratified additions

The nine cameo portraits (`CA-1`) and IBM Plex Mono as `--font-mono` (`CA-2`) were
already owner-ratified from round 001 and did not need re-declaration.

The brief taught a concrete missing-field declaration: a proposed
`state.cards.<id>.riskLevel`, integer 1–5, used by `RiskDrawer.jsx` and a person-card
badge, visibly stubbed until implemented, with the owner's exact directive recorded.
Undeclared fields remained `undefined` with one diagnostic rather than receiving a
truthy blanket placeholder.

## What had to come back

- the whole 73-file package tree, not a canvas export or changed-file subset;
- confirmation and a rendered capture of the one-to-one adoption;
- filled `DROP_README.md`;
- `contract-additions.json`, even when empty;
- a drift list;
- portable-relative asset paths.

The brief explained the scar: accepting a five-file partial return would make those
five files the next fingerprint and permanently stop checking all other shipped files.

## Outbound proof

The recorded package was rebuilt deterministically and walked in real Chrome at site
root, deep subpath, and `file://`. The app rendered, two Next-turn clicks advanced
turn 0 to 2, the hand held five cards, its tab closed and reopened, reload re-rendered,
and no response was 400 or higher. A deliberately reintroduced pre-correction field
lane failed 10 of 13 assertions with an empty root, proving the walk could turn red.
