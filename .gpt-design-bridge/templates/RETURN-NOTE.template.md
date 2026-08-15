# Designer return note — template

Fill every `<<FIELD>>`. A return note is a claim checked against the full returned
tree. If the note and diff disagree, the verifier reports the mismatch for owner
ruling; it does not silently repair either.

## Mandatory packaging preflight

Build the return from a fresh extraction of the original sealed outbound package,
never from an earlier return, a designer-application export, or a previously cleaned
folder. Overlay only the intended designer-owned changes, this filled note,
`contract-additions.json`, and the final `travelling-data-export.sqlite3`.

Every undeclared and off-limits file must remain byte-for-byte identical to the
original sealed outbound package. Treat supposedly untouched files as opaque bytes.
Do not let design, export, optimization, formatting, sanitization, or archive tooling
parse and reserialize files under `assets/pickers/**`.

Before creating the archive, compare every supposedly unchanged path with its
SHA-256 entry in `BASELINE-MANIFEST.json`. Restore every mismatch from the original
sealed package. If the available tooling cannot preserve the bytes or verify the
hashes, stop and report the limitation instead of returning an unverified archive.
The archive may contain the effective package root directly or beneath any harmless
wrapper chain. There must be exactly one directory containing `index.html`,
`BASELINE-MANIFEST.json`, `RETURN-NOTE.md`, and
`travelling-data-export.sqlite3`; do not duplicate those root markers elsewhere.

## Identity

- Project: `<<PROJECT_NAME>>`
- Round: `<<ROUND_ID>>`
- Built from build stamp: `<<BUILD_STAMP>>`
- Built from package SHA-256: `<<OUTBOUND_ARCHIVE_SHA256>>`
- Adoption mode copied from `ADOPTION-MODES.json`: `<<ADOPTION_MODE>>`
- Designer: `<<DESIGNER_NAME_OR_TOOL>>`
- Return date: `<<ISO_DATE>>`

## Owner request and final intent

Owner request:

> `<<OWNER_REQUEST_VERBATIM>>`

Final design intent: `<<FINAL_INTENT>>`

If direction evolved:

- `<<ITERATION_1_AND_WHY>>`
- Final choice: `<<FINAL_CHOICE>>`

## Scope

Files/blocks changed:

- `<<CHANGED_PATH_1>>`
- `<<CHANGED_PATH_2>>`

Files added:

- `<<ADDED_PATH_OR_NONE>>`

Files removed:

- `<<REMOVED_PATH_OR_NONE>>`

Deliberately left untouched: `<<UNTOUCHED_SCOPE>>`.

## Changes

### 1. `<<CHANGE_TITLE>>`

- Request: `<<REQUEST_EXCERPT>>`
- Intent: `<<INTENT>>`
- Built: `<<PLAIN_DESCRIPTION>>`
- Files: `<<FILES>>`
- Existing data/routes used: `<<EXISTING_DATA_OR_NONE>>`
- Contract additions: `<<CA_IDS_OR_NONE>>`
- Assumptions: `<<ASSUMPTIONS_OR_NONE>>`
- Moved/removed interactions: `<<INTERACTION_CHANGES_OR_NONE>>`
- Breaking changes: `<<BREAKING_CHANGES_OR_NONE>>`

Repeat this complete block for every change.

## Contract additions

The machine-readable source is `contract-additions.json`.

For each addition:

- ID/kind/path: `<<CA_ID_KIND_PATH>>`
- Why it exists: `<<RATIONALE>>`
- Directive: `<<BY_DATE_VERBATIM_NOTE>>`
- Surfaces: `<<SURFACES>>`
- Real behavior: `<<BEHAVIOR_ONCE_ENGINEERED>>`
- SPECIMEN value/marking: `<<SPECIMEN_SHAPE_AND_LOCATION>>`

If none: `contract-additions.json contains an empty additions array`.

## New Lucide icons

`<<LUCIDE_ICON_NAMES_OR_NONE>>`

## Debug-only additions

`<<DEBUG_PATHS_OR_NONE>>`

## How to review

1. `<<ACTION_1>>`
2. `<<ACTION_2>>`
3. `<<ACTION_3>>`
4. `<<NESTED_OR_KEYBOARD_ACTION>>`

Expected loading/error/empty behavior: `<<STATE_REVIEW>>`.

## Return completeness

- Whole outbound tree included: `<<YES_NO>>`
- `contract-additions.json` included: `<<YES_NO>>`
- Final owner-only data escrow included at package root:
  `<<EXACT_ESCROW_FILENAME>>`
- Workspace/secret/production-data files excluded: `<<YES_NO>>`
- Fresh original sealed outbound package used as the assembly base:
  `<<ORIGINAL_SEALED_BASE_USED>>`
- Every supposedly unchanged path matches `BASELINE-MANIFEST.json`:
  `<<UNTOUCHED_HASHES_MATCH>>`
- Protected files, including `assets/pickers/**`, were not parsed or reserialized:
  `<<NO_PROTECTED_RESERIALIZATION>>`
- Archive contains exactly one unambiguous effective package root; wrapper chain:
  `<<WRAPPER_CHAIN_OR_DIRECT_ROOT>>`
- Known uncertainties: `<<UNCERTAINTIES_OR_NONE>>`

This note does not authorize engineering to inspect or act on the escrow database.
The bridge must preserve it and report its identity only. Any query, comparison,
conversion, import, merge, overwrite, or other semantic operation requires a later
prompt-scoped free-prose instruction from the owner that names the database and the
requested operation.

---

# Worked sample — Statecraft accepted round 001 (real record)

The original prose return note was not retained in the accepted record. This worked
sample is therefore explicitly reconstructed from the accepted full-tree snapshot,
its baseline manifest, and
`design-bridge/responses/001/contract-additions.ratified.json`. It does not pretend to
quote a missing document.

## Identity

- Project: `webdesign-pgame`
- Round: `001`
- Built from build stamp: `req-001-js-a995bc63fe274801`
- Designer surface: the returned `design/` canvas tree
- Adoption mode: `exact`
- Ratification date recorded in both owner directives: `2026-07-28`

## Final design intent

The accepted direction established the deep-green felt table, gold/brass accents,
cream card faces, and IBM Plex Mono numerals. It added a portrait library for people
surfaces and made IBM Plex Mono the mono/numeric token.

## Scope evidenced by the accepted tree

The baseline manifest recorded the returned design components and styles, including
`design/components/PersonCard.jsx`, `MinistryBoard.jsx`, `Portrait.jsx`,
`design/tokens.css`, `design/shell.css`, and `design/cards.css`.

The accepted additions were:

### CA-1 — portrait assets

- Kind/path: `asset`, `design/portraits/`
- Actual addition: 9 cameo PNGs
- Naming contract:
  `<ethnicity>_<gender>_<nnn>_<stage><age>.png`
- Target render: `212x140`
- Surfaces: person-card portrait slot, ministry tiles, people roster
- Intended behavior: build-time folder glob into ethnicity × gender × stage buckets;
  prefer within-game uniqueness; procedural portrait only for an empty bucket
- Owner directive: “Yes. I would like to wire in the portrait library and we'll start
  populating it slowly.”

### CA-2 — IBM Plex Mono

- Kind/path: `asset`, `--font-mono: IBM Plex Mono`
- Package: `@fontsource/ibm-plex-mono`, bundled like existing fonts
- Surfaces: numerals across dials, meters, and treasury figures
- Intended behavior: point `--font-mono` at IBM Plex Mono after adoption
- Owner directive: “Ratified with the drop-1 design direction.”

Neither addition was silently treated as authority. Both were reconstructed from the
diff, routed to the owner, ratified, and recorded durably.

## Interaction/change disclosure

The accepted artifacts make the portrait and typography additions observable. The
later request 003 also records the round-001 scar: the designer returned a canvas
export while the package itself came back byte-identical. The next re-baseline
therefore required editing and returning the package's whole tree, not a separate
canvas.

The accepted exact-mode JSX/CSS and visible copy were the executable interface
baseline. Engineering was not authorized to recreate or translate that source;
production integration belonged in adapters outside the accepted surface.

## How to review the accepted direction

1. Open the returned design preview.
2. Inspect people cards and ministry tiles for the cameo portrait treatment.
3. Inspect dials, meters, and treasury figures for IBM Plex Mono.
4. Compare the returned full tree against `BASELINE-MANIFEST.json`.

## Return completeness

The accepted snapshot contains the package manifest and design tree used to reconstruct
this note. The two additions are present in the ratified machine-readable record. No
claim is made here about prose that is absent from the record. The effective package
root was direct in the accepted snapshot, so its wrapper chain was `direct root`.
