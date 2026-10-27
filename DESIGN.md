---
name: Mineval historical workspace
description: Compact evidence-led benchmark operations
colors:
  paper: "#0b0d10"
  sidebar: "#0a0b0e"
  surface: "#101216"
  soft: "#171a20"
  ink: "#f5f5f6"
  muted: "#a6aab5"
  line: "#292d35"
  accent: "#98abff"
  success: "#59d9b0"
  danger: "#f58c98"
  warning: "#f1c875"
typography:
  headline:
    fontFamily: "'Geist Sans', Geist, Inter, system-ui, sans-serif"
    fontSize: "25px"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "-0.025em"
  body:
    fontFamily: "'Geist Sans', Geist, Inter, system-ui, sans-serif"
    fontSize: "13px"
    lineHeight: 1.55
  label:
    fontSize: "11px"
    fontWeight: 500
  mono:
    fontFamily: "'Geist Mono', ui-monospace, monospace"
    fontSize: "12px"
    lineHeight: 1.6
rounded:
  status: "5px"
  control: "6px"
  panel: "7px"
spacing:
  compact: "12px"
  field: "16px"
  section: "24px"
  workspace: "32px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "7px 12px"
  button-secondary:
    backgroundColor: "{colors.soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "7px 12px"
  input:
    backgroundColor: "{colors.soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "7px 10px"
---

# Design System: Mineval historical workspace

## Overview

The historical workspace is dark, compact and restrained, following the user-pinned direction of Linear, Vercel, Better Auth and Resend. Its first viewport prioritizes evaluated configuration comparisons and category coverage, with task preparation and benchmark launch easy to find. The supplied reference establishes visual direction; its sample data is never product data.

**Key Characteristics:**

- Persistent navigation and dense, scannable operational tables.
- Quiet tonal surfaces, compact controls and sparse semantic color.
- Evidence, denominators and task coverage remain visible beside rates.

## Colors

Cool near-black neutrals carry the workspace; pale periwinkle marks links, focus and queued work. The frontmatter records the implemented palette from `mineval/web/historical.css`.

### Primary

- **Periwinkle accent:** links, input accents, keyboard focus and queued or running states.

### Neutral

- **Canvas and sidebar:** the darkest layers anchor the persistent shell.
- **Workspace and panel:** surface and soft tones separate tables, toolbars and fields without visual noise.
- **Ink and muted:** primary content and secondary annotations maintain a readable hierarchy.
- **Line:** quiet borders define regions and table structure.

Success mint, failure rose and warning amber communicate outcomes; each status also has a text label. Matrix cells use corresponding tinted backgrounds.

## Typography

Geist Sans and Geist Mono are preferred with local system fallbacks; no external font requests. Body copy uses a compact developer-tool rhythm. Headings establish hierarchy without promotional scale.

- Page headings use the headline role, reducing to (23px) on mobile.
- Section headings use (15px), semibold; tertiary headings use (14px).
- Table cells use (12px); headers and small annotations use the label scale.
- Code and evidence use the mono role. Rates and tables use tabular numerals; paragraphs have a maximum width of (85ch).

## Layout

Desktop uses a fixed sidebar (184px) and a workspace capped at (1800px), with padding (32px) and a bottom inset (72px). Above (1600px), workspace padding grows to (40px 48px). At (800px) and below, navigation becomes a horizontal strip and content uses a consistent inset (24px 16px). A stable scrollbar gutter prevents page-to-page horizontal shifts.

Tables scroll inside bordered containers. Task tables retain their column width and scroll horizontally. Four statistic columns become two on mobile; minimum-zero grid tracks and wrapping values contain long identifiers. Paired columns become a single column. Toolbars and actions wrap without changing the content inset.

**The Contained Table Rule.** Preserve the page inset; wide comparison content scrolls within its own container.

## Elevation & Depth

The historical workspace uses tonal layering and borders, without card shadows. The sticky task-selection toolbar gains functional prominence through position, not decorative depth. Web Arena loads its own gallery styles before the shared historical stylesheet; its preview dialogs and toast retain their specific overlay behavior.

## Shapes

Controls have compact corners using the control radius; tables, fieldsets and statistic panels use the panel radius. Status tags use the smaller status radius. A single-pixel border separates quiet surfaces. Avoid decorative gradients and oversized chart treatments.

## Components

- **Buttons:** a pale primary action with dark text; secondary actions use the soft surface and quiet border. Hover changes brightness and surface tone. Disabled controls reduce opacity and expose an unavailable cursor.
- **Inputs:** dark inset fields with a visible border and accent caret. Native controls preserve server-rendered form behavior under the historical server's restrictive CSP.
- **Navigation:** muted labels and small outlined icons; the active and hovered destination uses a lifted neutral surface with bright text. Mobile navigation scrolls horizontally.
- **Status tags:** compact bordered labels with semantic tinted surfaces. Text remains explicit for success, failure, queued, running and review states.
- **Comparison tables and matrices:** compact row padding (10px 12px), muted headers, hover rows, tabular numerals and inline rate meters. All-evidence comparisons are descriptive; comparable figures use exact shared conditions. Unavailable results remain unavailable.
- **Task selection:** bulk actions appear above the table when checkboxes are selected and remain sticky. Applying task filters clears selection, explicitly described in the UI.
- **Benchmark workspace:** task, model and review steps preserve frozen revision IDs. Selected tasks survive filters. Model search preserves selected configurations, explicit identifiers, explicit effort and the skip choice; effort checkboxes allow several configurations per identity. Review precedes POST submission.
- **Model profile:** a dedicated constrained reading surface retains observations, original evidence, historical runs and compaction provenance behind progressive disclosure. Private deterministic evaluation is distinguished from optional AI assistance and host-controlled review overrides.
- **Activity and preparation:** preparation review precedes submission; activity exposes durable outcomes, result links and retries.

Keyboard focus uses an accent outline (2px) with offset (3px). Text selection has a distinct tinted background. Brief link and button color transitions (0.12s ease) run only when reduced motion is not requested.

## Do's and Don'ts

### Do

- Do use the shared historical stylesheet for workspace tokens and primitives.
- Do keep tables scrollable inside their containers and preserve mobile content insets.
- Do retain sample denominators, task coverage and explicit unavailable states.
- Do preserve model choices, explicit identifiers, effort and skip settings while searching.

### Don't

- Don't add decorative gradients or large charts to operational comparisons.
- Don't load external fonts or introduce client scripts into historical server forms.
- Don't collapse distinct frozen conditions into an implied comparable result.
