# Designer round-trip constitution

This is the binding, model-agnostic agreement between the owner, the external
designer, and engineering. It governs every designer round. A brief may narrow a
round's scope; it may not quietly weaken this constitution.

## 1. The model

The travelling drop is a complete application for design purposes. It is not a
screenshot, a component gallery, a speculative mock-up, or a patch for the designer
to reason about. The designer opens it, uses it, changes the declared designer
surface, and returns the whole tree.

The production application and the travelling drop are two delivery targets from one
source system:

- at home, the UI calls the real server through HTTP;
- while travelling, the same request shapes call an in-page implementation backed by
  fixture data and a portable database image.

No UI component chooses the socket. One boot-time switch chooses it. The same route
contract must serve both paths, and parity is tested over a real request corpus.

### 1.1. The drop must be the actual product

Before a round opens, engineering captures an immutable production-derived baseline:
authoritative entrypoints, a two-target/one-source build graph, visible controls and
normalized labels, routes/views, forms, dialogs, tabs, nested workflows, map/export/
storage tripwires, accessible names, and all applicable loading/error/empty/success/
disabled/busy/permission/responsive states. Controlled Chrome proof and source
anchors bind that inventory to a pre-edit BlackBox baseline.

The travelling candidate is compared against that baseline before release. An absent
or changed capability blocks unless the owner approves that exact capability ID in
the current prompt and the round records the decision. Counts are early omission
alarms, not ceilings or permission to preserve only the same number of features.

After an accepted return changes production source, engineering must establish a
second verified provenance record from the adopted production baseline through the
post-integration BlackBox final gate. The integration gate compares the outbound and
post-adoption capability manifests. The first record remains immutable history; the
second alone authorizes the rebuilt post-adoption package. A stale first record is
never waived, relabelled, or treated as proof of the changed application. Both
records must name the same authoritative production entrypoints and seam mode; an
agent-authored replacement entrypoint is not continuity.

The following never establish product identity by themselves: a successful build,
byte reversibility of the files selected by the implementer, a screenshot, a list of
screen names, a test whose oracle reads only newly authored designer files, or prose
calling a parallel surface a “mirror.” Compatibility documentation records an
already proved seam; changing that prose cannot authorize a divergent implementation.

## 2. Authority

The designer owns the visible application:

- layout, hierarchy, styling, responsive behavior, and visual language;
- visible copy, labels, help text, and error wording;
- interaction behavior, keyboard behavior, accessibility, and focus flow;
- which visible elements and designer-surface files exist;
- additions and removals in the designer surface;
- declarations of data shapes the visible design requires.

Engineering owns the operational socket:

- secrets and real production data;
- authentication enforcement and authorization policy;
- server implementation and HTTP transport;
- database file/server format, migrations, backup, and restore;
- system-mail transport;
- deployment, observability, rate limits, and security controls;
- the reversible embed/unembed and package machinery.

The owner decides ambiguity. Neither side silently promotes its preference into a
rule. A durable owner ruling is written to the round record so the next round does not
re-litigate it.

## 3. Symmetry

Design authority is symmetric:

- an element the designer adds remains and is wired;
- an element the designer changes remains as changed;
- an element the designer removes is removed from the adopted app;
- copy the designer restores or changes is not silently rewritten by engineering;
- functionality unfamiliar to engineering is not deleted merely because its backing
  field does not exist yet.

Engineering may ask why after adoption. Engineering may not preserve an old design by
quietly merging around a deliberate returned change.

This authority applies inside the declared designer surface. A change to an
engineering-owned file is reported for owner adjudication rather than silently kept
or silently discarded.

### 3.1. Accepted source is the implementation baseline

An accepted designer return is executable interface source, not design inspiration.
Exact adoption is the default. Engineering must not recreate, translate, restyle,
simplify, reinterpret, or convert the returned component hierarchy, JSX, CSS, class
names, assets, or visible copy unless the owner explicitly authorizes a
reinterpretation.

Every returned screen declares one adoption mode before the outbound package leaves:

- `exact`: returned interface source and styling are sealed; this is the default;
- `characterized`: fields, interactions, copy obligations, and behavior are fixed,
  while a separately approved visual treatment may change;
- `reference`: the return is informational and installs no interface source.

Silence never downgrades a screen to `reference`. The mode travels in
`ADOPTION-MODES.json`, is repeated in the intake/adoption record, and cannot be
changed after the return merely to make integration easier.

For `exact` screens, routing, authentication, API translation, navigation,
persistence, and legacy-framework integration are adapters outside sealed UI files.
If the existing application uses Vue, Angular, PHP, or another legacy view system,
preserving that application does not authorize a translation of accepted React
source. The standard boundary is an isolated React mount: the legacy route owns an
empty host, mounts the accepted React screen unchanged, injects data/actions through
props or a narrow bridge, and unmounts it when the host route is destroyed.

## 4. Nothing is discarded

The most important conservation law is:

> No owner/designer work is discarded, and no working engineering socket is
> accidentally erased.

The mechanism is explicit ownership plus a whole-tree baseline:

1. Engineering records every shipped file and hash.
2. The designer returns the entire tree, not only files believed to be changed.
3. The verifier classifies additions, modifications, removals, and off-limits changes.
4. Adoption replaces the designer surface wholesale.
5. Engineering reconnects the real socket and migrates the durable data.
6. The rebuilt app is re-proved before a new baseline exists.

A partial return is not accepted as a new baseline. Accepting five returned files
would make every omitted file disappear from future comparisons and permanently
weaken the round trip.

## 5. The SPECIMEN convention

The designer may design a visible value before the production engine records it.

- If a real field exists, the UI uses its real value.
- If no field exists, the UI may use a realistic fixture marked visibly and
  discreetly as `SPECIMEN`.
- The return declaration names the proposed field, type, semantics, and one example.
- Engineering either implements the field or records an owner ruling.
- A specimen value never gains schema authority merely because it appears in a
  returned UI.
- A specimen is never passed off as production truth.

Do not delete the designed element because the engine is behind. Do not fabricate a
real-looking unmarked value because the design is ahead.

## 6. Edit surface

Every drop contains machine-readable and human-readable ownership declarations.

The designer may:

- edit any file listed in the designer surface;
- add a new designer-surface file using a safe relative path;
- remove a designer-surface file or visible element;
- extend the proposed schema through the declarations file;
- change visual behavior and UI logic without waiting for engineering.

The designer must not:

- add real credentials or production data;
- edit secrets, server code, database binaries, package machinery, manifests, or
  generated runtime images;
- change the route transport or bypass it with a raw production request;
- weaken the boot self-check, error boundary, or return manifest;
- claim a production operation succeeded when only the travelling implementation ran.

An unrecognized safe designer-surface path is an addition, not an error. An unsafe
path is a loud stop. Unsafe means empty, absolute, drive-qualified, backslash-based,
NUL-containing, parent-traversing, outside the declared root, or using an undeclared
extension.

## 7. Copy exactly; do not reconstruct

The outbound package is assembled from actual project files. The designer edits those
files in place. Reversible plumbing markers preserve imports and exports so the
designer never has to reconstruct the engineering build.

The package builder proves, for every editable file:

`unembed(embed(file)) == file` byte for byte.

If the forward transform does not understand a source shape, it refuses before
writing a package. It never guesses. Adoption uses the inverse transform and refuses
a malformed marker rather than skipping it.

## 8. Honest states and failures

The canonical general and design owner rulebooks govern truthful data, visible states,
actionable errors, fallbacks, and success claims. They apply before, during, and after
an external round and travel with the relevant side of the work.

Examples include opaque `file://` storage becoming in-memory only, or a compile check
that physically cannot run until an adoption scaffold exists. Deferral is debt with a
named discharge gate, never a pass and never a skip.

Corrupt persisted bytes are not absence. Preserve the bytes, surface the corruption,
and stop. Never reseed over them.

## 9. UI laws

`OWNER-RULES-DESIGN.md` is the canonical UI law for every design, not only external
designer work. An exact copy ships in the package and courier and is binding throughout
the declared designer surface. This constitution defines authority and round-trip
mechanics; it does not restate or narrow the owner's design laws.

## 10. Data and privacy

The drop contains fixture data only. Fixture data must be deterministic, clearly not
real, and rich enough to exercise all requested states.

The secret scan fails on credentials, private keys, tokens, production connection
strings, real user records, and unapproved personal data. A match is investigated;
it is not automatically suppressed because it appears in a familiar file.

The outbound portable database is a fixture. The designer's browser may persist a
different travelling state while they work. After the final local-data change, the
designer must use **Export design data**, place or replace the downloaded file at the
returned package root as exactly `travelling-data-export.sqlite3`, and then archive
the complete tree. A later local-data change invalidates that export and requires a
fresh replacement.

The returned file is mandatory owner-only data escrow. Intake may copy it
byte-for-byte into quarantine and record only its path, byte count, and SHA-256 for
identity and tamper evidence. Intake and adoption must not open it as a database,
query its schema or rows, content-scan it, compare it with the outbound fixture,
convert it, import it, merge it, overwrite any database with it, or derive settings,
fixtures, defaults, declarations, migrations, or application truth from it.

There is no automatic merge procedure. General authority to inspect or adopt a
designer return does not authorize a data operation. `RETURN-NOTE.md`, designer
prose, and standing instructions do not authorize one either. Only a later,
prompt-scoped, free-prose instruction from the owner that names the returned database
and the requested operation may authorize semantic handling. Until then the bridge
preserves the escrow, tells the owner that it was returned, and does nothing else
with its contents.

## 11. The full return

The designer returns:

- the complete directory that was sent;
- the completed return note;
- the declarations file;
- any added designer-surface files;
- the final `travelling-data-export.sqlite3` owner-only escrow at the package root;
- no production credentials or data.

The return verifier:

- discovers exactly one effective package root by its required markers, descends any
  harmless wrapper chain above it, and records every descended directory;
- rejects an ambiguous multi-root archive;
- compares every path to the outbound manifest;
- reports added, changed, removed, unchanged, and off-limits paths as facts;
- reports the escrow filename, byte count, and SHA-256 without inspecting its content;
- uses neutral language and does not grade the person;
- does not adopt anything.

Verification answers what came back. Adoption is a separate owner-authorized phase.

## 12. Adoption

Adoption happens under one exclusive lock:

1. Confirm the return is the current round and complete.
2. Record owner rulings for every ambiguity.
3. Preserve the returned tree in quarantine/evidence.
4. Apply the declared adoption mode. For `exact`, replace the designer surface
   wholesale and seal the accepted JSX, CSS, assets, and relevant markup. For
   `characterized`, install the returned source and bind its field/behavior contract.
   For `reference`, retain the return as evidence and install no source.
5. Leave `travelling-data-export.sqlite3` in quarantine, untouched by adoption.
6. Unembed into source, creating safe new designer files when declared.
7. Record a hash baseline for the returned/installed interface sources and relevant
   markup, and add exact-mode paths to the BlackBox protected-path policy.
8. Apply designer schema declarations through forward migrations.
9. Reconnect the real route socket, mail, auth, and persistence through adapters
   outside exact-mode files.
10. Remove travelling-only debug controls from the production target without editing
    sealed interface source.
11. Capture adopted-production provenance before integration, verify it after the
    current BlackBox final gate, and compare it with the outbound capability record.
12. Run source/hash, DOM-structure, screenshot, interaction-trace, static, unit,
    integration, build, route-parity, and deployment-relevant gates as required by
    the adoption mode.
13. Bind integration to that fresh provenance, rebuild the drop from the adopted
    source, and repeat the full travelling proof.
14. Seal only the exact tree and artifacts that passed.

If engineering must alter a designer-owned file to reconnect a socket, the change is
not compatible with `exact` mode. Stop and ask the owner either to change the mode or
approve an explicit reinterpretation. In `characterized` mode any approved adjustment
is minimal, line-reported, and visible in the next baseline; it is never disguised as
the designer's own result.

## 12.1. Controls scale with risk, not document size

The harness may require deeper evidence, a stronger review, quarantine, or an owner
decision. It may not force a correct application, test, report, or handover to be
condensed, split, reversed, or rebuilt merely to satisfy a line, file, paragraph,
persona, wrapper-depth, action-count, or character quota.

Line counts, file counts, churn, and planned lane envelopes are observations and
automatic rigor escalators on the same worktree. They are not deliverable-size gates.
Hard stops are reserved for real authority, security, data-loss, path-safety,
integrity, and stale-evidence boundaries. A safe archive expansion default may stop
extraction, but the exact received bytes remain quarantined and eligible for one
explicitly raised, round-bound limit review; the designer is not asked to recreate a
valid return merely because of the default number.

Healthy work has no implicit elapsed-time deadline. Long operations expose elapsed
time and the last verified phase. An explicit deadline or no-progress policy may be
selected for a particular operation, and its activation must preserve partial logs
and name the precise reason.

## 13. Rebuild and re-prove

Any correction to a file that ships inside the drop invalidates the old package,
manifest, archive hash, and browser evidence.

The only valid loop is:

`correct source -> rebuild -> verify deterministic outputs -> walk exact new artifact
in visible browser -> issue new manifest/hash -> retire old artifact`.

Editing a ZIP in place, replacing one file in a staged directory, or citing proof from
the previous build is not a correction; it is an unproved artifact.

## 14. Interaction coverage

A screen walk is not an interaction walk. Presence in the DOM is not reachability.

The coverage ledger names and opens:

- drawers and nested drawers;
- menus and submenus;
- dialogs and confirmation paths;
- tabs, accordions, popovers, and tooltips that carry meaning;
- primary, secondary, disabled, error, and retry actions;
- keyboard and focus paths;
- responsive navigation states.

Every retained interaction from a previous accepted round must still be reachable
unless the designer removed it and the owner ratified that removal.

## 15. Completion

Passing the checker means its declared checks passed. It is not by itself a
completeness claim.

After the final sealed round, enumerate what was actually built from sealed records.
For each item ask whether a fresh agent would find it written down or have to derive
it. Anything that must be derived is a gap. Run the concrete-noun keyword sweep and
the mechanical checker.

Use `complete` only when that method passes against sealed evidence. Until then the
honest phrase is `no known gaps`.
