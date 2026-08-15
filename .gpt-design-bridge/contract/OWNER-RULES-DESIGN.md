# D. Owner rules — design

## D1. Scope

**D1.1.** These are the owner's reusable laws for any UI or design work. They apply to
every person, agent, and tool that creates or changes a visible interface: direct
design with the owner, engineering implementation, internal or designer-facing
tooling, and an optional external designer round. They are not instructions only for
an external designer.

**D1.2.** The general owner rules remain active at the same time.

## D2. UI chrome, iconography, and local icon assets

**D2.1.** Every newly scaffolded project must immediately install its declared Lucide
binding and carry complete local copies of the approved Lucide SVG pack and Twemoji
SVG pack under project-owned assets. Runtime UI and designer tooling must not depend
on a CDN for either pack.

**D2.2.** Use real Lucide icons for UI chrome.

**D2.3.** Do not author or display emoji anywhere in the interface as an icon,
decoration, status marker, empty-state illustration, or visual shorthand unless the
owner explicitly requests emoji for that scoped use or rule D2.4 requires a country
flag.

**D2.4.** Every country selector must show the country's flag. The flag must resolve
from the project's local Twemoji assets; do not use a remote image, operating-system
emoji glyph, Unicode flag text, or an invented substitute.

**D2.5.** Permission to use Twemoji for country flags does not authorize emoji
elsewhere. A broader emoji use still requires an explicit, scoped owner request.

**D2.6.** Do not use hand-drawn SVG, Unicode symbols, styled letters, or text glyphs as
icon substitutes.

**D2.7.** A declared logo or brand illustration is artwork, not UI chrome.

**D2.8.** If the requested Lucide icon name does not exist, fail during development.
Do not silently render a generic icon that changes the meaning.

**D2.9.** Icon-only controls require an accessible name, keyboard reachability, a
visible `:focus-visible` state, and an appropriate designed tooltip.

**D2.10.** The exact Lucide binding is a stack detail. Source authoring may import a
binding; the travelling drop may use its vendored global. Both must resolve to the
same Lucide icon semantics.

**D2.11.** When an icon picker, custom-SVG picker, or associated color picker is
needed, start from the owner-provided premade component carried by the foundation and
adapt it to the current product's tokens, interaction rules, accessibility, and
approved icon types. Do not re-create a competing picker. The raw template's Emoji
tab is asset capability, not owner permission to expose emoji.

**D2.12.** Custom SVG input must retain the supplied sanitization boundary, local-only
asset loading, and bundled third-party attribution. Premade status never exempts a
component from the other numbered owner rules.

## D3. Honest visible states and errors

**D3.1.** Every data-driven surface has separate loading, error, empty, and success
states. A failed request never appears as authentic emptiness, and a missing value
never becomes a plausible default.

**D3.2.** Every visible error states what operation failed; which exact safe resource
was involved, such as the file, field, record, route, service, or configuration key;
why the application needed that resource; the actual known cause; and what the user
can do next or which support/reference identifier to report.

**D3.3.** Do not show bare messages such as “Unable to read the file,” “Data not
found,” or “Something went wrong.” Name the safe file or data, the attempted location
or service, and the purpose of the operation.

**D3.4.** Do not expose secrets, access tokens, private production paths, raw stack
traces, or sensitive record contents while adding diagnostic context.

**D3.5.** A dialog that submits data closes only after the durable server-side
operation is verifiably successful. Failure keeps the dialog, entered values, failed
action, and recovery context visible.

**D3.6.** Missing numeric or money data renders as absent, never as a fabricated zero.
Currency amounts travel as an amount plus currency, not as a bare number with a
relabeled glyph.

## D4. Interaction policy

**D4.1.** Use designed dialogs, not native `alert`, `confirm`, or `prompt`. This
includes internal and designer-facing tools.

**D4.2.** Controls are reachable by keyboard and show a visible `:focus-visible`
state.

**D4.3.** Focus moves deliberately when a dialog, drawer, menu, or comparable layer
opens and returns to a sensible origin when it closes.

**D4.4.** State is communicated by text or shape as well as color.

**D4.5.** Busy actions prevent duplicate submission immediately.

**D4.6.** Disabled actions explain why when the reason is not obvious.

**D4.7.** Destructive actions require an explicit designed confirmation that names
the action and its consequence.

**D4.8.** Test nested interactions, not only route-level screens or DOM presence.

### D4.9. Busy feedback and long-running progress

**D4.9.1.** Set the busy state immediately when an action begins and include useful
action text; do not wait for the spinner threshold before acknowledging the input.

**D4.9.2.** If the action remains pending for roughly one second, show a website-style
spinner with the action text. Prefer an animated Lucide loader such as
`LoaderCircle`; the spinner is never the only status signal.

**D4.9.3.** If the action is likely to take substantially longer, normally around ten
seconds, show the spinner plus real progress or current-phase updates.

**D4.9.4.** Refresh long-running status when verified information changes and
otherwise about every two to five seconds. Do not flood announcements.

**D4.9.5.** Never fabricate a percentage, completed step, phase, progress message, or
estimated arrival time. When granular progress is unavailable, show elapsed time and
the last verified phase instead.

**D4.9.6.** Progress text is accessible to assistive technology, and motion respects
the user's reduced-motion preference.

**D4.9.7.** On failure, stop the busy indicator, retain relevant context, and show the
actionable error. On success, show completion only after success is verified.

**D4.9.8.** The one- and ten-second values are interaction thresholds, not promises of
exact timing. Use observed duration and the nature of the operation.

### D4.10. Tooltips

**D4.10.1.** Use designed tooltips where they add meaning: icon-only controls,
unfamiliar or ambiguous controls, truncated content, and controls whose consequence
is not immediately clear.

**D4.10.2.** A tooltip supplements rather than replaces an accessible name, a
necessary visible label, or a critical explanation.

**D4.10.3.** Tooltips open from keyboard focus as well as pointer hover, are
dismissible, do not trap focus, and have a usable touch equivalent.

**D4.10.4.** Do not hide safety-critical information or a disabled reason only inside
a tooltip.

**D4.10.5.** Do not rely on a browser `title` attribute as the only tooltip
implementation.

### D4.11. Stable pop-up geometry

**D4.11.1.** A dialog, popover, picker, menu, drawer, or comparable choice layer must
not grow or shrink merely because the user changes an expected choice, tab, family,
palette, option group, validation state, or nested choice.

**D4.11.2.** Size the outer layer for its largest expected normal state or reserve a
stable content region from first render. Revealing an expected sub-palette or option
set must not push the outer frame larger and smaller.

**D4.11.3.** When variable content exceeds the reserved region, scroll inside that
bounded region or use a deliberately anchored overlay. Preserve labels, focus,
keyboard reachability, touch access, and assistive-technology announcements.

**D4.11.4.** Responsive resizing caused by a viewport or supported breakpoint is
allowed. Incidental resizing caused by moving between choices at the same viewport is
not.

**D4.11.5.** Browser proof for a layered choice control must exercise its expected
choice states and compare the outer layer's dimensions before and after the changes.

### D4.12. Layer, form, and narrow-viewport composition

**D4.12.1.** A dialog, picker, tooltip, menu, or other overlay launched from a native
modal must render in that modal's active top layer or in a deliberately newer top
layer. Appending it to `document.body` behind an open native modal is a defect even
when the overlay exists in the DOM.

**D4.12.2.** An adapter for an imperative or premade overlay must accept or derive the
owning layer, place the overlay there, and fail with the exact layer operation when
placement cannot be proved. It must not silently fall back to a visually obscured
body mount.

**D4.12.3.** Layer proof uses rendered dimensions and native hit testing on a real
launcher path. DOM presence, a component snapshot, or a screenshot alone does not
prove that an overlay is visible, clickable, focusable, or above the correct layer.

**D4.12.4.** A reusable field, picker, or control must not create an internal
`<form>` when it may be composed inside a product form. Form ownership is explicit;
Enter and button behavior remain available through valid event handling. Nested forms
are prohibited.

**D4.12.5.** A browser console error, warning, invalid-DOM or nesting warning,
unhandled rejection, or uncaught exception on the exercised path blocks release.

**D4.12.6.** At every supported narrow viewport, the document itself must not gain
horizontal overflow. A deliberately bounded table, code surface, or comparable
region may scroll internally when its design requires it; that scroll must not leak
to the page.

## D5. Styling policy and designed controls

**D5.1.** Build every interface in a dark theme from the first render. Initialize
tokens, browser color-scheme metadata, loading states, and error states for dark mode
rather than adding dark mode after a light interface is complete. Add or default to a
light theme only when the owner or product requirement explicitly calls for it.

**D5.2.** Define design tokens once and use them consistently.

**D5.3.** Do not hide a missing token behind a literal fallback.

**D5.4.** Check contrast, legibility, focus, and behavior in every supported theme.

**D5.5.** New styles must not erase responsive behavior or accessibility.

**D5.6.** Status colors supplied by data remain data; do not hard-code them as theme
accents.

**D5.7.** Information, status, selection, and validation never depend on color alone.

**D5.8.** Do not expose browser-stock dropdown lists, calendars, date pickers, time
pickers, visible file controls, or scrollbars as the product interface. Provide
custom-designed controls that match the current theme. A visually hidden native file
input may open the browser or operating-system file-selection dialog when activated
by the designed Browse control required by D8.

**D5.9.** A custom control must preserve or improve the native control's keyboard,
focus, labeling, validation, touch, reduced-motion, and assistive-technology behavior.
Visual customization is not permission to reduce accessibility.

## D6. Design questions to the owner

**D6.1.** When a design decision requires owner input, present it under exactly these
four labels: `Problem`, `Proposed solution`, `Benefits of implementing`, and
`Pitfalls of NOT implementing`.

**D6.2.** State the concrete decision and recommendation under those labels. Do not
ask a vague aesthetic question or hide the cost of declining the proposal.

## D7. Searchable and filterable lists

**D7.1.** Every multi-item data list, table, or management collection must be
searchable and filterable by default. This is a standing layout requirement and does
not need a separate design discussion.

**D7.2.** The standard list header contains a basic global search field and a visible
matching-versus-total count.

**D7.3.** Every data-column label is a clickable sorting action with an explicit
ascending, descending, or unsorted state. Every data column also has its own filter
control directly beneath its label.

**D7.4.** Global search, all active column filters, and the selected sort compose
together. The matching count updates from that combined result; no control silently
resets another.

**D7.5.** Responsive layouts may change the presentation but must retain the same
search, per-field filtering, sorting, count, keyboard access, labels, and active-state
visibility.

## D8. File attachment fields

**D8.1.** Every file attachment field provides three coherent input paths: drag and
drop; a designed Browse control that opens the browser or operating-system file
selector; and clipboard paste when D8.2 applies.

**D8.2.** When the field accepts an image type and the clipboard contains a compatible
image file, pressing `Ctrl+V` or the platform-equivalent paste shortcut while that
upload target is active immediately attaches the clipboard image.

**D8.3.** Clipboard handling must not intercept ordinary text paste in text inputs,
editors, or unrelated page areas. The image attachment target must be active and the
clipboard payload must pass the declared type rules.

**D8.4.** All three input paths use the same type, size, count, malware/security,
duplicate, authorization, progress, cancellation, and error pipeline. No input path
may bypass validation or persistence.

**D8.5.** Rejection errors name the safe file, the validation that failed, why the
application needs that validation, and the allowed next action.

## D9. Copyable values

**D9.1.** Values a user is reasonably likely to reuse—such as tracking numbers, links,
reference IDs, quoted values, and generated outputs—include a nearby copy-to-clipboard
control.

**D9.2.** The control uses the Lucide `Copy` icon and remains visually secondary to
the value. It has an accessible name and a designed tooltip that identifies the exact
value type being copied.

**D9.3.** Report copied state only after the clipboard operation verifiably succeeds.
On failure, preserve the value and show the actionable cause and a manual-copy path.

## D10. Copy, design authority, and absent fields

**D10.1.** Preserve user-visible copy exactly unless the owner or the currently
authorized design source explicitly changes it. Engineering does not silently rewrite
designer copy.

**D10.2.** Do not delete an unfamiliar designed element because its engine field is
absent.

**D10.3.** If the real field exists, display the real value.

**D10.4.** If no field exists, use a discreetly and visibly marked `SPECIMEN` value
and declare the missing shape.

**D10.5.** A `SPECIMEN` value is a non-authoritative placeholder. It never becomes
schema, production truth, or evidence merely because it appears in a design.

**D10.6.** The owner decides ambiguity about visible meaning, copy authority, or
whether a design change is intentional.
