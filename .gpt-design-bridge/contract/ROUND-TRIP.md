# End-to-end designer round trip

This is the operational procedure. The project-local command owns lifecycle state and
exclusive mutation locks. Individual package scripts own build mechanics. Never
advance a state by editing `state.json` manually.

## 1. Four preservation lanes

The tracked `.gpt-blackbox-lite-policy.json` enables four engineering lanes without
weakening the shared preservation core. Every lane keeps the exact Git baseline,
allowlist, protected anchors, preflight, actual-diff inspection, anti-deletion and
anti-rewrite checks, current-tree evidence, and mechanical gate.

| Lane | Intended use | Immediate depth | Release status |
|---|---|---|---|
| `patch` | Tiny internal correction | Senior Engineer plan/implementation; risk domains may add review | Provisional |
| `standard` | Bounded internal feature or fix | Senior Engineer plan/implementation/audit; risk domains may add review | Provisional |
| `surface` | Bounded visible UI, copy, layout, style, or interaction work | Standard depth plus mandatory owned-Chromium evidence and surface-path enforcement | Provisional |
| `full` | Foundation, protected, uncertain, cross-cutting, database, deployment, security, or designer-round work | Senior Engineer throughout plus roles mechanically required by actual risk domains | Final |

The owner may select a lane only for the current prompt. Record that selection with
`--lane`; never turn it into a standing preference. Without a prompt-scoped owner
pick, the classifier chooses the smallest lane that contains the declared estimate
and interface flag. The estimate controls initial ceremony, not implementation
authority or deliverable size. If actual work outgrows a cheap lane, the harness
promotes the same task/tree monotonically to `full`, records the causes and risk
domains, and asks only for missing deeper reviews. It never requires a valid patch,
test, or handover to be reversed, split, condensed, or redone to preserve a cheaper
label. Protected or owner-boundary paths remain hard stops; an exact in-goal supporting
path may use BlackBox's audited `discover` command.

The generated application foundation and first broad MVP implementation are
`foundation` work and therefore `full`. Later small internal changes may classify as
`patch` or `standard`. Later bounded direct-design changes may classify as `surface`.
Opening, adopting, sealing, or deploying a designer round remains force-full.

A passing `patch`, `standard`, or `surface` gate is explicitly provisional. It queues
the exact immutable task tree for the settle roles printed by BlackBox from the real
risk domains. Only `settle-gate` changes that item to confirmed. Deferred depth has
moved; it has not been skipped. Do not manufacture fixed persona prose when a risk
domain does not require that lens.

The project-local Bridge invokes `release-gate` automatically before `stage-prepare`,
`drop-release`, `adopt-apply`, `adopt-integrate`, `round-seal`, and native deployment.
That gate blocks while any cheap task is provisional or has a finding, and while any
started full task lacks its current-contract final gate. Do not bypass or reproduce
these release checks by hand.

Installing an already verified external-designer surface is a separate lifecycle
boundary, not an engineering-authored implementation. `adopt-apply` is governed by
the Bridge's sealed baseline, full-tree comparison, declared surface, off-limits and
security findings, owner rulings, source-drift refusal, and transactional recovery
copy. Do not start a BlackBox task before `adopt-apply` and do not count the recovery
copy as coding churn. Start a fresh BlackBox task immediately afterward, using the
adopted tree as its baseline, before any engineering integration or correction.
Capture a second production provenance record on that exact adopted baseline and
verify it after integration. `adopt-integrate --provenance P-NNN` binds the verified
record, compares its capabilities with the immutable outbound record, and refuses
unruled loss. The outbound record stays historical; it is never silently refreshed.

A Bridge check that physically cannot run may separately be recorded as `deferred`,
with a reason, compensating check, owner, target phase, and mandatory discharge gate.
This lifecycle deferral is not BlackBox lane settlement. Deferred is neither pass nor
skip. The round cannot seal while a seal-required obligation remains.

### 1.1. Cap and progress doctrine

Work size is measured for understanding, never used to make the owner receive less.
There is no source-line, task-line, document-length, wrapper-depth, browser-action,
or finding-count ceiling. Churn and historical numeric tripwires are observations
that deepen review on the current tree. Structured evidence is judged by what it
proves, not by a minimum prose length.

Hard boundaries remain hard: unauthorized paths, owner exclusions, unapproved
deletion/replacement, unsafe archive paths/links, unknown binaries, secrets,
production data, stale hashes, and missing required proof. Where a safe exception is
possible, the blocked object stays intact and the command reports its exact measured
condition and narrow override route.

Child commands stream output to a durable project log. After roughly one second the
operator sees activity; long work reports elapsed time and its last observed output or
phase every few seconds. Healthy operations have no implicit deadline. A caller may
choose a task-specific total deadline or no-output stall window; either one preserves
partial output and names the termination reason.

## 2. State machine

```text
initialized/building
        |
   outbound_open
        |
   awaiting_return
        |
   return_received
        |
      adopting
        |
      proving
        |
       sealed
```

Only one active round exists. Every mutation takes the lifecycle lock. The active
round ID, baseline Git tree, package manifest hash, archive hash, and evidence paths
are recorded before the package leaves.

## 3. Prepare the app from the first change

Before feature work:

- read `OWNER-RULES.md` and apply `OWNER-RULES-GENERAL.md` to all work;
- apply `OWNER-RULES-DESIGN.md` to every visible interface change, whether the owner
  designs directly with engineering or later requests an external designer;
- select SQLite or PostgreSQL through the database gate;
- establish the pure route table and one state port;
- establish the travelling/home socket switch;
- declare the designer surface and engineering-owned paths;
- keep production secrets and real data outside the UI source;
- establish native deployment configuration without Docker.

Do not postpone the route seam until handoff. Packaging an app that directly reads
production state causes a rewrite at the most expensive moment.

These preparation rules do not make an external round compulsory. Open one only after
the owner asks for external designer participation.

### 3.1. Capture product identity before designer-facing edits

Every external round begins with a full BlackBox task and a production baseline. The
task protects the real production entrypoints and capability sources, requires owned
browser proof, and declares designer/preservation risks (plus architecture for an
adapter seam). Put the declaration and proof inputs in ignored runtime/evidence paths:

```powershell
python .gpt-design-bridge/tools/gpt_design_bridge.py provenance-template `
  --task <blackbox-task-id> > .gpt-design-bridge/runtime/provenance-declaration.json

# Fill the strict declaration, then capture before UI edits.
python .gpt-design-bridge/tools/gpt_design_bridge.py provenance-capture `
  --declaration .gpt-design-bridge/runtime/provenance-declaration.json `
  --proof normal=evidence/production-baseline/normal/proof.json `
  --proof responsive=evidence/production-baseline/responsive/proof.json
```

The declaration covers all nine standard states. The example supplies two proofs only
because its filled declaration must source-anchor honest `not-applicable` decisions
for the other seven; supply another `--proof STATE=...` for every applicable state.

After implementation, run current production proofs, finish reviews/checks, pass the
same BlackBox final gate, then verify:

```powershell
python .gpt-design-bridge/tools/gpt_design_bridge.py provenance-verify `
  --id P-001 `
  --proof normal=evidence/production-final/normal/proof.json `
  --proof responsive=evidence/production-final/responsive/proof.json
```

Missing controls, labels, workflows, states, map/export/storage tripwires, unreachable
designer files, altered compatibility records, and stale application trees fail. A
deliberate difference needs an exact owner decisions file and its matching factual
`--user-approved-*` flag; general permission to build is not approval to delete a
capability.

## 4. Open a round

The lifecycle command validates:

- Git and project kit are healthy;
- no active round or mutation lock exists;
- the working tree status is recorded exactly;
- the requested designer surface paths exist or are declared additions;
- the owner brief is complete;
- prior accepted interaction coverage is loaded;
- no seal-required deferred obligation is outstanding;
- every entrypoint has an explicit adoption mode; an omitted mode is `exact`;
- `--provenance P-NNN` names a passing record for the current guarded application
  tree, exact designer surface, and exact travelling entrypoints.

The round receives a zero-padded ID and a content-derived baseline identity. The
baseline is immutable for that round.

Failure modes:

| Failure | Required response |
|---|---|
| Active round exists | Stop; resume or explicitly abandon it |
| Stale lock exists | Stop; inspect owner/process and record resolution |
| Git root mismatch | Stop; run at the recorded root |
| Missing owner decision | Record a numbered question; do not guess |
| Designer surface unclear | Narrow it before build |
| Adoption mode unclear | Use `exact`; ask only if the owner intends a weaker contract |
| Missing/stale provenance | Stop; capture and verify the actual product under a full task |
| `brownfield-seam-required` | Prove direct source or a forward/inverse React-island seam first |

## 5. Derive the route inventory

The route inventory comes from real UI call sites and the route table. Each entry
records:

- method and path;
- literal source pattern;
- source file and derived line;
- travelling handler;
- production adapter;
- request/response schema;
- interactions that exercise it.

The verifier re-searches every literal pattern. Zero hits means the inventory is stale
and the build stops. Line numbers are output, never remembered.

## 6. Build deterministic fixture data

Fixture state is produced by running the real engine and migration ladder from
deterministic inputs. It is not hand-authored to make the UI look rich.

- no wall clock;
- no unseeded randomness;
- obviously fictional identities;
- every requested view state represented;
- fixture marker stored inside the database;
- schema version recorded;
- real database bytes and embedded image hash-equal.

Where the design needs a field the engine lacks, use the declared `SPECIMEN` path. Do
not silently add an authoritative production field in fixture code.

## 7. Verify reversible designer files

Run the embed verifier before assembling:

- every declared designer file forward-converts;
- closure scan finds no live import/export;
- inverse output equals source bytes;
- marker paths are safe;
- existing marker text is refused;
- a controlled safe new-file case round-trips.

If this gate fails, no outbound folder is written.

## 8. Build runtime and package

The package build:

1. builds the engineering IIFE with inline programmatic config;
2. vendors exact UMD dependencies;
3. verifies the lowercase React alias and Babel classic output;
4. embeds database/WASM byte images and compares hashes;
5. writes one inline block per designer file and the mount last;
6. copies declared styles/assets and design-of-record;
7. generates human and machine ownership declarations from one source;
8. scans real asset references and secrets;
9. creates a content-derived manifest;
10. creates a deterministic ZIP;
11. builds twice and compares hashes.

Every existing outbound build directory is replaced only after the new build passes in
a staging directory. A failed build never leaves a folder that looks shippable.

## 9. Write the brief

The brief is complete for a designer who remembers nothing:

- binding pointer to the exact shipped `OWNER-RULES-DESIGN.md`;
- owner goal and priorities;
- current application behavior;
- exact designer surface;
- explicit off-limits surface with reasons;
- must-preserve interactions;
- requested screens/states;
- known constraints and real limitations;
- how to open `file://` and served mode;
- how to reset or export travelling data;
- the exact final-export rule for mandatory owner-only returned data escrow;
- how to mark `SPECIMEN`;
- what full return contains;
- current baseline and round identifiers.

Use the filled worked sample in the template as the quality bar. Delete sample content
from the real brief; do not leave a placeholder or instructions masquerading as a
brief.

## 10. Prove outbound in visible Chrome/Chromium

Build evidence drives the artifact it just built, never a convenient dev server. Use
the copied project-local controller, which owns a visible pinned Chrome for Testing
process, throwaway profile, debugging port, and cleanup:

```powershell
node .gpt-design-bridge/tools/controlled_chrome.mjs doctor
node .gpt-design-bridge/tools/controlled_chrome.mjs template > proof-plan.json
node .gpt-design-bridge/tools/controlled_chrome.mjs run `
  --plan proof-plan.json `
  --output evidence/designer-round-<round-id>/<empty-proof-directory>
```

Use `install` once if `doctor` names the absent pinned binary. Never fall back to the
owner's normal Chrome, profile, tabs, cookies, extensions, or the in-app browser.
Never disable web/file security. The owned browser navigates `file://` directly, so a
generic controller's refusal is not an owner-manual proof step.

Prove all four independent contexts:

1. direct `file://` double-click mode;
2. deep HTTP subpath mode;
3. a broken-mount probe that makes the boot self-check fail;
4. home mode without a backend, where the real request must fail loudly.

Record for all four contexts:

- exact package/manifest/archive hashes;
- browser binary and version;
- viewport;
- console errors;
- failed requests and external requests;
- content types;
- screenshot paths;
- route/screen coverage;
- interaction coverage;
- persistence state.

Native actions must open every nested interaction named in the coverage ledger. A
drawer existing in the DOM or becoming CSS-visible is not proof that the overlaid tab
can be clicked.

The broken-mount and wrong-content-type probes must themselves fail. A failure test
that stays green is not a test.

Every artifact referenced by browser proof must be a regular file under
`evidence/designer-round-<round-id>/`. Scratch output under
`.gpt-design-bridge/runtime/` is not release-eligible evidence.

If a generic browser controller refuses the local URL, use the project-local owned
controller above. If its `doctor`, launch, navigation, or proof fails, preserve
`proof.partial.json`, Chrome stdout/stderr, and the exact diagnostic; keep the round
unsealed and repair that environmental boundary. Do not ask the owner to take over a
routine direct-file check, and do not substitute served HTTP. Both direct-file and
deep-HTTP contexts remain independently required. The four required contexts are a
minimum set; named supplemental contexts are welcome and never make a valid proof
schema fail.

Before `drop-release`, extract the exact candidate ZIP (not the prepared stage), run
owned Chrome state proofs against that extracted tree, and bind explicit nested
workflows to passing action indices and selector identities in a strict parity
bindings file. Generate the exact active-round inventory first; do not invent its
schema or omit an explicit workflow:

```powershell
python .gpt-design-bridge/tools/gpt_design_bridge.py parity-template `
  > evidence/designer-round-<round-id>/parity-bindings.json

# Replace every marked selector with a stable identity present in the captured DOM,
# and replace the placeholder action index with the actual passing action(s).
python .gpt-design-bridge/tools/gpt_design_bridge.py parity-check `
  --package <exact-extracted-package-root> `
  --bindings evidence/designer-round-<round-id>/parity-bindings.json `
  --proof normal=evidence/designer-round-<round-id>/normal/proof.json `
  --proof responsive=evidence/designer-round-<round-id>/responsive/proof.json `
  --output .gpt-design-bridge/evidence/parity/<round-id>.json
```

The extracted tree hash must equal the candidate tree. Production-versus-travelling
capability loss blocks. If the owner deliberately approves a removal, replacement,
or semantic change, record the exact capability ID first with `capability-ruling
--user-approved`; do not use a broad owner ruling or edited brief as a substitute.
Put the parity report path and SHA-256 in the aggregate browser proof's `parity`
object. Release validates it against the current provenance, round, candidate
archive, build stamp, and package tree.

## 11. Seal and send

Outbound seal requires:

- all static/build tests current to the package tree;
- all four browser contexts current to the package hash;
- zero unexplained secret or portability findings;
- no seal-required deferral;
- completed brief and empty declarations file;
- matching human/machine off-limits declarations;
- deterministic rebuild match;
- passing production-to-travelling capability parity for the exact candidate;
- current provenance/build-graph/capability hashes and no unexplained difference;
- recorded archive and manifest hashes.

Sending the file is an external action and requires owner authorization. The platform
may build and seal locally without that authorization; it may not choose a recipient
or transmission channel.

After send, state becomes `awaiting_return`. The sealed baseline never changes.

## 12. Receive the return

Ingest is quarantine only:

- copy the received archive/tree into a new immutable evidence location;
- hash the received bytes before extraction;
- reject unsafe archive paths and links;
- locate exactly one directory containing the required root markers, descend any
  harmless wrapper chain above it, and report the complete chain;
- reject missing or multiple candidate roots;
- never execute a returned script;
- never write returned files into application source.

ZIP expansion defaults are hard pre-extraction safety guards, not a demand that the
designer reconstruct or repackage valid work. On a member, per-file, total-byte, or
compression-ratio limit, quarantine the exact received archive and report the
observed field/value. After review, retry that same SHA-256 with explicit
`--archive-max-*` values that retain every default and raise only the necessary
limit. The override is recorded against the round, archive, and inspection attempt.

The complete tree must contain a non-empty
`travelling-data-export.sqlite3` at its package root. The designer creates or replaces
it with **Export design data** after the final local-data change. Missing or empty
escrow requires resubmission; state remains `awaiting_return`.

That file is owner-only escrow. Intake may preserve its exact bytes and record its
path, byte count, and SHA-256. It must exclude the file from content and secret scans
and must not open it as SQLite, query it, compare it with the fixture, convert it,
import it, merge it, overwrite another database with it, or derive any fact from its
contents.

State becomes `return_received` only after safe ingestion and presence of the required
escrow.

## 13. Verify the whole returned tree

Compare the normalized full tree with the sealed baseline:

- unchanged;
- changed in designer surface;
- added in designer surface;
- removed from designer surface;
- changed outside designer surface;
- missing from the return;
- unexpected generated/debug artifacts.

Cross-check the return note and declarations against the actual diff. Validate
declarations against their schema. Report facts in neutral language; do not assign a
score or grade.

For `travelling-data-export.sqlite3`, verification stops at path, non-zero byte count,
and SHA-256 identity. Hashing is tamper evidence, not permission to interpret the
contents.

Off-limits changes and ambiguous intent go to owner rulings. Verification never
silently repairs, adopts, or rejects design authority.

## 14. Owner rulings

Each ruling records:

- round and finding ID;
- observed facts and hashes;
- owner choice;
- whether it changes the standing constitution or this round only;
- adoption action;
- future-round consequence.

A prompt-scoped choice applies only to the named question/round unless the owner makes
it standing. A standing ruling travels in the next drop.

Database escrow is narrower. Neither a return note, designer request, general adoption
authorization, standing instruction, nor an owner ruling inferred from context may
authorize a data operation. Only a later owner-authored free-prose prompt that
explicitly names the returned database and says what to do with it can authorize that
one operation. No automatic merge procedure exists.

## 15. Adopt

With owner authorization and one lifecycle lock:

1. prove the current designer source still matches the sealed outbound source hash;
2. preserve the verified return and rulings;
3. create and hash-verify an exact pre-adoption recovery copy;
4. read `ADOPTION-MODES.json`; an absent legacy declaration is treated as `exact`;
5. for `exact`, replace the designer surface wholesale and make the returned JSX,
   CSS, assets, and relevant markup the protected accepted baseline; for
   `characterized`, install the source and bind fields/behavior; for `reference`,
   preserve evidence and install nothing;
6. unembed returned blocks into source;
7. create declared safe new designer files;
8. apply removals the designer made;
9. turn schema declarations into pending engineering work;
10. record returned and installed source manifests, relevant markup hashes, sealed
    paths, and integration obligations.

The recovery directory remains present and hash-recorded, but the project-local
ignore rule excludes `baselines/*/adoption-*/` from Git and BlackBox code-churn
accounting. Ignoring it does not delete it or weaken recovery verification.

After `adopt-apply` succeeds, start a new BlackBox task from the adopted tree. In that
task, reconnect production routes, auth, persistence, migrations, and Postmark; strip
travelling-only debug controls from production output; and keep adapters outside
sealed exact-mode files. Only current-tree BlackBox checks and its final gate may be
supplied to `adopt-integrate`.

Before integration edits, generate and fill a new provenance declaration for that
BlackBox task and capture the adopted production baseline. After integration and its
current-tree final gate, run `provenance-verify`, then bind both integration evidence
and that exact verified identity in one mutation:

```powershell
python .gpt-design-bridge/tools/gpt_design_bridge.py adopt-integrate `
  --evidence evidence/adoption/integration.json `
  --provenance P-002
```

The command compares the outbound and post-adoption capability manifests. Added
capabilities are recorded. Missing or changed capabilities fail unless a
`capability-ruling` names that exact ID and owner decision. Compatibility/readiness
records must remain byte-identical across the transition. The authoritative
production entrypoints and direct/adapter-seam mode must also remain identical; a new
mock entrypoint cannot become the post-adoption oracle. Every post-adoption
`drop-build` revalidates P-002 against the current production tree and refuses P-001
as stale; this is a provenance chain, not a stale-gate exemption.

An accepted return is executable interface source, not inspiration. Do not recreate,
translate, restyle, simplify, or convert its component hierarchy. Production uses
React/ReactDOM. In an existing Vue, Angular, PHP, or legacy application, use a
framework island: legacy route → empty host → mount accepted React screen unchanged →
inject the application adapter through props → unmount when the route is destroyed.
The bridge must not convert the return into Vue.

Exact-mode integration requires source/hash parity, DOM-structure parity, screenshot
parity at agreed widths, and interaction-trace parity. The source-preservation report
names every file, hash, and changed line range, and conservatively flags whether JSX
hierarchy, classes, visible copy, or CSS declarations may have changed. Any
exact-source change blocks; an import-path or asset relocation is not silently
declared mechanical. The owner must explicitly change the contract if an edit inside
the sealed UI is genuinely necessary.

Do not cherry-pick visual preferences. Do not restore old copy. Do not delete a new
element because it currently uses `SPECIMEN`.

Ordinary adoption leaves `travelling-data-export.sqlite3` byte-identical in quarantine
and takes no semantic action on it. Its presence is reported to the owner; it is not a
source file, fixture update, migration input, or production database candidate.

## 16. Triage absent-field reads

Every read of a field that production does not record is classified:

- dead: unreachable/unused after adoption;
- awaiting mechanics: designed and intentionally `SPECIMEN` until a named engine task;
- genuine bug: intended to be real now but missing.

Anything not confidently classifiable remains awaiting owner/mechanics. The output is
a derived list with source locations, not an intuitive count.

## 17. Rebuild and re-prove

Adoption is not sealed on a successful merge. Re-run the entire outbound ladder:

- route inventory;
- freshly generated outbound fixture database, never the returned owner-only escrow;
- reversible transform;
- runtime/package build;
- deterministic double build;
- static/security/asset checks;
- all four browser contexts;
- full nested interaction ledger;
- production socket parity;
- application test/build checks.

If any correction changes a shipped file, discard the staged package and its evidence,
rebuild, and repeat. Never edit the archive in place or reuse browser proof from the
previous hash.

## 18. Seal the adopted round

Seal records:

- adopted Git tree;
- return and ruling hashes;
- migrations;
- direct/HTTP parity evidence;
- app checks;
- rebuilt package, manifest, and archive hashes;
- visible browser evidence;
- discharged and remaining non-seal deferrals;
- next-round baseline identity.

Then clear `active_round`, append the round to `sealed_rounds`, increment the state
generation, and publish the fresh baseline locally. Commit, push, deploy, or send only
when separately authorized.

## 19. Completeness audit

After the final milestone seals:

1. enumerate actual sealed work records;
2. list each technique the work required;
3. map each technique to a contract, tool, test, or worked sample;
4. treat anything a new agent must derive as a gap;
5. sweep concrete stack nouns for zero hits;
6. run the project checker;
7. close documented gaps and rerun.

If the evidence record is still in flight, use `no known gaps`. Reserve `complete` for
a passing audit against sealed records.

## 20. Reproducible artifact cleanup

Builds replace stale output instead of accumulating versioned debris. The generated
`scripts/prepare-build-output.mjs` removes only the resolved `dist/server` directory
immediately before its replacement and refuses links/non-directories. Deployment
keeps the current and immediately previous proved release. Older marked releases are
eligible only when their source commit exists and is proven reconstructable from the
configured remote ref.

For registered local artifact roots use:

```powershell
python .gpt-design-bridge/tools/artifact_lifecycle.py report --output evidence/artifact-report.json
python .gpt-design-bridge/tools/artifact_lifecycle.py preview --output evidence/artifact-preview.json
python .gpt-design-bridge/tools/artifact_lifecycle.py prune `
  --preview evidence/artifact-preview.json `
  --preview-sha256 <recorded-preview-sha256> `
  --receipt evidence/artifact-prune-receipt.json `
  --apply
```

Never prune databases, uploads, secrets, designer return archives, returned travelling
SQLite escrow, adoption baselines, manifests, required screenshots/proof, current
deployment, or sole rollback. Unknown/unmarked content is retained. Git existence
alone is insufficient; remote reconstruction must be mechanically proved when the
project relies on GitHub to rebuild.
