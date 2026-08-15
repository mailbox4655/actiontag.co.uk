# G. Owner rules — general

## G1. Scope

**G1.1.** These are the owner's reusable product laws for every task. They remain
active whether the work is backend-only, directly designed with an agent, or later
handed to an external designer.

**G1.2.** Project additions may make these rules stricter. Only an explicit, scoped
owner ruling may create an exception.

## G2. Loud truth

**G2.1.** No silent fallback and no swallowed exception are permitted.

**G2.2.** Do not turn missing configuration or missing required data into a plausible
default.

**G2.3.** Do not render a failed fetch or failed query as an honest empty result.

**G2.4.** Do not report success until the real server-side or durable operation has
verifiably succeeded.

**G2.5.** Do not invent an identifier, IP address, timestamp, metric, count, price,
status, or API result.

**G2.6.** Do not create a random render-time fixture.

**G2.7.** Preserve the real cause of a failure. Do not replace it with a convenient
but false explanation, success state, empty state, or default value.

### G2.8. Fallback log

**G2.8.1.** If a fallback or degraded mode is genuinely necessary, it must be a
distinct, visible, testable state registered in the project's canonical debug file,
`docs/FALLBACK-LOG.md`, before the fallback code is accepted.

**G2.8.2.** Every fallback-log entry must name a stable fallback ID; the exact trigger;
the affected operation and safe resource; why that operation needs the resource; the
unavailable data or capability; the visible behavior; the runtime diagnostic
signature; the test; the owner; the removal condition; and the current status.

**G2.8.3.** Every runtime activation of a registered fallback must emit its stable
fallback ID through the project's normal diagnostic logging path. When safe and useful,
the visible degraded state must expose the same ID so evidence can be correlated.

**G2.8.4.** Troubleshooting begins by reading `docs/FALLBACK-LOG.md` and comparing the
observed diagnostic ID and trigger with registered fallbacks. A symptom that matches no
registered fallback is investigated as a genuine error, not retroactively relabeled as
degradation.

**G2.8.5.** An unrecorded fallback or degradation is a defect. A log entry does not
legalize a silent fallback or a false success state.

**G2.9.** Deferral records named debt; it does not convert the deferred obligation into
a pass or a skip.

## G3. Data, persistence, and privacy

**G3.1.** User-entered application data persists server-side unless it is explicitly
documented as an ephemeral UI preference.

**G3.2.** Mock fixtures are deterministic and obviously fictional. They never imitate
a real person, credential, tenant, or production record.

**G3.3.** Corrupt data is preserved for diagnosis and surfaced as corruption; it is
never silently replaced, reseeded, or treated as absence.

**G3.4.** Production data and credentials never enter a designer drop, test fixture,
source file, screenshot, log, or evidence artifact.

**G3.5.** Secrets stay in their declared server environment. Client code, travelling
packages, fixtures, and proof artifacts must not receive them.

## G4. Engineering practice

**G4.1.** Derive counts, inventories, paths, and hashes with commands. Do not eyeball
or remember them.

**G4.2.** Prefer a specific refusal to a guessed workaround.

**G4.3.** Preserve working behavior outside the declared change surface.

**G4.4.** Use the exact configured dependency, route, database, and deployment
contract; do not silently substitute an easier mechanism.

**G4.5.** Run proof against the exact artifact being handed over or deployed. Evidence
from a development server, previous build, different archive, or earlier tree is not
transferable.

**G4.6.** When a required check cannot run, name the unavailable dependency or
boundary, record the obligation and its discharge gate, and keep any affected claim
open.

## G5. Authority and records

**G5.1.** The owner decides product meaning and ambiguity.

**G5.2.** Record durable owner rulings where the next task can find them.

**G5.3.** Never convert an assumption, fixture, fallback, or earlier prompt-scoped
choice into standing authority.

**G5.4.** Do not discard owner work, accepted behavior, or evidence merely because it
is unfamiliar or inconvenient to integrate.
