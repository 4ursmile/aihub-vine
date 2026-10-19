---
name: AI Hub
description: A calm, Apple-HIG-inspired package hub with translucent chrome, inset-grouped lists and one themable accent.
colors:
  primary: "#006edb"
  primary-hover: "#0066cc"
  primary-dark: "#0a84ff"
  success: "#17803a"
  danger: "#d70015"
  warning: "#b35000"
  canvas: "#f5f5f7"
  surface: "#ffffff"
  surface-quiet: "#fafafc"
  ink: "#1d1d1f"
  ink-secondary: "#636366"
  ink-tertiary: "#6b6b70"
  canvas-dark: "#000000"
  surface-dark: "#1c1c1e"
  surface-quiet-dark: "#2c2c2e"
  ink-dark: "#f5f5f7"
  ink-secondary-dark: "#a1a1a6"
typography:
  display:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Helvetica Neue', Helvetica, Arial, sans-serif"
    fontSize: "clamp(36px, 5.4vw, 60px)"
    fontWeight: 700
    lineHeight: 1.07
    letterSpacing: "-0.04em"
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Helvetica Neue', Helvetica, Arial, sans-serif"
    fontSize: "clamp(26px, 4vw, 34px)"
    fontWeight: 700
    lineHeight: 1.12
    letterSpacing: "-0.035em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', Helvetica, Arial, sans-serif"
    fontSize: "21px"
    fontWeight: 600
    letterSpacing: "-0.025em"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', Helvetica, Arial, sans-serif"
    fontSize: "17px"
    fontWeight: 400
    lineHeight: 1.47
    letterSpacing: "-0.022em"
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', Helvetica, Arial, sans-serif"
    fontSize: "13px"
    fontWeight: 500
    letterSpacing: "0"
  mono:
    fontFamily: "ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "14px"
    lineHeight: 1.5
rounded:
  sm: "10px"
  md: "14px"
  pill: "980px"
spacing:
  xs: "6px"
  sm: "12px"
  md: "16px"
  lg: "22px"
  xl: "40px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "#ffffff"
    rounded: "{rounded.pill}"
    padding: "0 20px"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "rgba(120,120,128,0.12)"
    textColor: "{colors.primary}"
    rounded: "{rounded.pill}"
    padding: "0 20px"
    height: "44px"
  field:
    backgroundColor: "rgba(120,120,128,0.12)"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0 14px"
    height: "44px"
  card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.md}"
    padding: "22px"
  badge:
    backgroundColor: "rgba(120,120,128,0.12)"
    textColor: "{colors.ink-secondary}"
    rounded: "{rounded.pill}"
    padding: "2px 9px"
---

# Design System: AI Hub

## Overview

**Creative North Star: "The Glass Ledger"**

AI Hub reads like a well-kept ledger seen through frosted glass: neutral, precise and number-forward, with the packages and usage figures as the only things that shout. The look borrows Apple's Human Interface language wholesale: the system font, big confident tight-tracked headings, pill buttons, iOS inset-grouped lists and a translucent blurred navigation bar. Chrome recedes so a mixed audience (engineers, managers, admins) can scan a catalog or a dashboard without learning the interface.

Density is generous. Content sits in a single 1024px column on a pale grey canvas, in white rounded surfaces with a diffuse shadow. Color is almost absent: grey ink, white and grey surfaces, and one accent used for links, primary actions, active states and bar fills. Semantic green, red and orange appear only for status, never decoration.

Motion is short and physical (a 3px card lift, a sliding nav underline, a 10px page fade-in) and under `prefers-reduced-motion` movement and animation are removed but colour, opacity and shadow changes remain (0.15s), so state is still visible.

**Key Characteristics:**
- System font stack only; no webfonts to load.
- One accent (`--blue` in CSS), admin-themable from eight choices, default Apple blue.
- Inset-grouped lists and pill controls; 14px surface radius, 10px field radius.
- Light and dark follow the OS (`prefers-color-scheme`); dark is true black.
- Translucent sticky nav with backdrop blur and a 1px hairline.

## Colors

A grey-and-white neutral field with one accent and three status hues.

### Primary
- **Apple Blue** (#006edb light, #0a84ff dark): links, primary buttons, active ranks, bar fills, focus rings and selection. It is the CSS variable `--blue`; the admin accent setting rewrites it, so never hard-code it.
- **Blue Lift** (#0066cc light, #409cff dark): hover state of the primary button only.

### Secondary (admin accents)
`data-accent` on `:root` swaps `--blue` for indigo (#5856d6), purple (#a03fd0), pink (#cc2d65), red (#d70015), orange (#b35000), green (#17803a) or teal (#087c7c), each with a brighter dark-mode twin. Components must work with any of them.

### Tertiary (status)
- **Go Green** (#17803a / #30d158): success, live indicator, switch on-state, high-strength meter.
- **Stop Red** (#d70015 / #ff453a): errors, danger actions, low-strength meter.
- **Caution Orange** (#b35000 / #ff9f0a): warnings, mid meter.

### Neutral
- **Paper Grey** (#f5f5f7 / #000000): page canvas.
- **White Glass** (#ffffff / #1c1c1e): cards, lists, the feed.
- **Quiet Surface** (#fafafc / #2c2c2e): code blocks.
- **Ink** (#1d1d1f / #f5f5f7): primary text.
- **Secondary Ink** (#636366 / #a1a1a6): descriptions, table headers.
- **Tertiary Ink** (#6b6b70): meta text, placeholders.
- **Hairline** (rgba(60,60,67,.14) light, rgba(84,84,88,.5) dark): separators.
- **Fill** (rgba(120,120,128,.12 / .24)): control backgrounds, tracks, badges.

### Named Rules
**The One Voice Rule.** The accent marks what is interactive or selected and nothing else; status colors mark status and nothing else.

**The Tokens-Only Rule.** Colors come from the CSS variables so light, dark and every accent theme work together. A literal hex in a component is a bug.

**The On-Accent Rule.** Text on an accent fill uses `--on` (white on light accents, black on the bright dark-mode accents), never a literal white. Every accent and theme must reach 4.5:1; tertiary ink (#6b6b70) is 4.9:1 on the canvas. Badge text is the hue mixed 65% toward ink, so it clears 4.5:1 on its own tint.

## Typography

**Display Font:** the system UI stack (-apple-system, SF Pro Display, Helvetica Neue, Arial)
**Body Font:** the same stack (SF Pro Text)
**Label/Mono Font:** ui-monospace / SF Mono / Menlo for commands, versions and code

**Character:** One family at many weights. Hierarchy comes from size, weight 600/700 and strongly negative tracking on headings, not from font pairing.

### Hierarchy
- **Display** (700, clamp(36px, 5.4vw, 60px), 1.07): home hero only, max 11ch.
- **Headline** (700, clamp(26px, 4vw, 34px), 1.12): page titles.
- **Title** (600, 21px; 19px in package cards): section and card headings.
- **Body** (400, 17px, 1.47, tracking -0.022em): default text; README prose runs at 1.6.
- **Label** (500, 13px): table headers, segmented controls, stat captions; 12px for badges.
- **Mono** (14px): command boxes, code blocks, versions (inline code at 0.88em).

### Named Rules
**The Tight Tracking Rule.** Tracking tightens as size grows (-0.01em small to -0.04em display). Never apply it to monospace or all-caps.

**The Tabular Rule.** Every number that can change or be compared (counts, ranks, KPIs) uses `font-variant-numeric: tabular-nums`.

## Layout

A single centered column, `max-width: 1024px`, with 22px side padding (16px under 700px), 40px top and 96px bottom. Sections breathe on a 16px grid gap and 40px section gaps. Card grids use `repeat(auto-fill, minmax(min(100%, 300px), 1fr))`; KPI rows `auto-fit` at 130 to 170px; two-up panels `auto-fit` at 340px.

The home hero is asymmetric: copy and search on the left (1.15fr), a live activity feed on the right (.85fr), collapsing to one column at 860px. Side padding, the nav top, the page bottom and the toast respect `env(safe-area-inset-*)`, so nothing sits under a notch or the home indicator. On touch screens (`hover:none`) cards do not lift on hover; they scale to .985 while pressed. Under `prefers-contrast: more` glass turns solid, separators strengthen and tertiary ink becomes secondary. Headings use `text-wrap: balance`, body text `pretty`, and the footer and README blocks use `content-visibility: auto`. At 700px the nav links scroll horizontally instead of wrapping and the user name collapses to its avatar. Touch targets are 44px on coarse pointers for every control (buttons, chips, segments, filters, switch); on fine pointers compact buttons are 32px and filter fields 34px.

## Elevation & Depth

Hybrid: ambient shadow in light mode, tonal layering plus a hairline in dark mode. A white surface floats a few pixels above the grey canvas through a soft two-layer shadow. In dark mode the shadow is dropped for a 1px rgba(255,255,255,.04) ring and the lighter surface color does the lifting. The nav gets depth from translucency and blur, not shadow.

### Shadow Vocabulary
- **Surface** (`0 0 0 .5px rgba(60,60,67,.07), 0 1px 2px rgba(0,0,0,.04), 0 6px 24px rgba(0,0,0,.06)`; dark: a 1px ring plus a faint inner top light): cards, lists, command box, feed.
- **Lift** (`0 2px 4px rgba(0,0,0,.05), 0 14px 36px rgba(0,0,0,.1)`): hovered package card, with a 3px upward move.
- **Popover** (`0 10px 40px rgba(0,0,0,.2)`): dropdown picker; tooltips use `0 4px 20px rgba(0,0,0,.18)`.
- **Glass** (`backdrop-filter: blur(16px) saturate(170%)`, fill `--glass` at 74 to 80% opacity, plus the `--rim` highlight: a 1px inner top light and a faint inner outline): the sticky nav, dropdown, tooltip and toast, the only layers that float over content. Segmented selection, secondary buttons and chips take the rim alone, with no blur. Falls back to solid surfaces without `backdrop-filter` or under `prefers-reduced-transparency`.

### Liquid Glass
Glass needs something behind it, so the page carries one fixed, static backdrop: three soft radial washes (accent, violet, green; 8 to 15% strength) that follow the active accent. Content surfaces (cards, lists, the feed, tiles, command boxes) are 76 to 80% opaque with the `--rim` specular edge and no blur, because the backdrop behind them is smooth. The navigation is a floating capsule (pill, 980px) inset 8px from the top; it firms up from 78% to 90% opacity once the page scrolls. Contrast was computed at the strongest wash under the most transparent surface: secondary ink holds 5.6:1 light and 5.9:1 dark.

### Performance budget
Exactly four blur layers exist (the nav capsule, dropdown, tooltip, toast), and only the nav is on screen at rest. The tinted backdrop is one static `position:fixed` layer, no animation. The nav's scrolled state comes from an IntersectionObserver on a 1px sentinel (`#top`), not a scroll listener. There is no pointer-driven or scroll-driven repaint: the dot field is gone. Hover on cards and tiles moves them with transform only (no animated shadow). The backdrop and nav capsule are promoted to their own compositor layers, so scrolling never repaints them; blur is 10px.

### Named Rules
**The Float-Only Glass Rule.** Blur is reserved for layers that overlap scrolling content; it is never applied to cards, lists or page sections. Keep each glass fill at 74% opacity or more so text holds 4.5:1.

**The Rest Is Calm Rule.** Surfaces are quiet at rest; the stronger shadow appears only in response to hover or an open popover.

## Shapes

Soft, continuous corners. Large surfaces use 14px, fields and code blocks 10px, inline code and small controls 6 to 9px. Anything you press or filter by (buttons, badges, search, tags) is a full pill (980px). Avatars and rank markers are circles. Lists clip their contents to the rounded container, with separators inset 18px from the left.

## Components

### Buttons
- **Shape:** full pill (980px), 44px tall, compact variant 32px.
- **Primary:** accent fill, white text, 15px, padding 0 20px.
- **Hover / Focus:** fill shifts to the lighter accent; press scales to .97 to .98; focus ring is 3px accent at 55% with 2px offset. Disabled drops to 40% opacity.
- **Secondary:** grey fill with accent text mixed 60% toward ink (so it holds 4.5:1 on every page surface). **Danger:** grey fill with red text mixed 60% toward ink, never a red fill.

### Chips and Badges
- **Style:** pill, 12px/500, grey fill and secondary ink; tinted variants (blue, green, red, orange) use the hue at about 15% fill with solid hue text.
- **State:** segmented control selected state is a white (dark: #636366) raised segment inside a grey track.

### Cards / Containers
- **Corner Style:** 14px. **Background:** white (dark: #1c1c1e). **Shadow:** Surface. **Border:** none. **Padding:** 22px.
- Package cards add a 150px minimum height, a two-line clamped description and meta in tertiary ink.

### Inputs / Fields
- **Style:** grey fill, no visible border, 10px radius, 44px tall, 17px text.
- **Focus:** background turns to surface, border to accent, plus a 4px accent halo at 22%. Search is pill-shaped with a leading icon.

### Navigation
- On phones (700px and below) the links and account area collapse behind a 44px burger inside the capsule. It opens a glass dropdown with 48px rows, closes on route change, link tap or Esc, and the three bars morph into a cross. The sliding underline is hidden there; the active page is a filled row.
- Floating glass capsule, not a full-width bar. Keyboard: `/` or Cmd/Ctrl+K focuses search from anywhere (and opens Browse if no search is on the page); Esc leaves it. The document title follows the page heading, and focus moves to it on route change.
- Sticky 52px capsule, translucent with blur and a hairline. Brand at 19px/600, links 14px secondary ink that turn to ink when active, with a 2px accent underline that slides between links.

### Knowledge (notes)
A two-pane glass notes app. The left pane (300px, sticky) holds a new-note field, a filter, the pinned violet-tinted Persona row, Your notes and Shared with me; rows are 44px, the open note is accent-tinted. The right pane is the editor: a bar with the file name, access badges, a live "Saved" or "Unsaved changes" state, Delete and Save (disabled until something changed), a borderless 15px mono textarea with a word and character count, and a Sharing panel of person chips with an add row. Ctrl/Cmd+S saves and unsaved edits ask before being discarded. At 760px and below it shows one pane at a time, with an "All notes" button to go back.

### Package page
A hero, an install card, a facts strip, then two columns. The hero is App Store style: a flat 120px squircle icon (22% radius, solid type colour, no gradient or glow, 88px on phones; a white line glyph per type instead of a letter: spark skill, person agent, plug MCP, wrench tool, sliders setup, robot bot; coloured by package type: blue skill, green agent, orange MCP, red setup, violet bot) beside a 34px bold name, the description as a grey 19px subtitle, then a row of type/private badge, a blue license badge with a document glyph, and tag chips, and the authors line. The install card is a glass panel with an accent wash holding the copy-able command. The facts strip shows Version, Downloads, Rating and Updated as four equal accent-coloured figures. Spacing follows an 8pt scale: 8 between title and lead, 16 inside a group (lead to tags to byline, install to facts), 32 between hero, install and body, 48 before Related. Below, the tabs and their content sit on the left, and a sticky 300px sidebar on the right lists requirements as pills (commands, dependencies linking to their packages, supported systems) and the latest release. Under 900px the panel stacks to one column and the sidebar moves above the tabs.

### Groups
Same two-pane glass shell as Knowledge. Left: a New group action in the page header (the create form opens at the top of the list, hidden until asked), a filter once there are more than five groups, and one row per group with a tinted gradient initial tile (four accent tints chosen by a hash of the name), member and repo counts, and a "member" badge when the viewer can't manage it. Right: the group header with its description, two large accent counts (members, shared repos), an Add people block (search plus bulk paste), a member grid of avatar chips with a 44px-on-touch remove button, and a red-tinted danger zone where deletion needs the group name typed. One pane at a time on phones.

### Numbers Panel (home)
One glass panel with four equal cells (Packages, Releases, Downloads, People). Every figure gets the same treatment: up to 76px, bold, in the accent colour, with a 600-weight ink label beneath and a soft accent wash across the top of the panel. On phones it becomes a 2 by 2 grid. Numbers are tabular and count up once when scrolled into view.

### Inset Grouped List (signature)
White container, 14px radius, rows 52px minimum with 13px/18px padding, inset hairline separators, optional rank circle (26px, accent when top), tabular numbers and a 4px proportional bar.

### Switch, Meter, Toast
- iOS switch at 51x31 with green on-state. Strength meter is a 4px track that fills red, orange, green. Toast is a dark pill at the bottom center, red when an error.

## Do's and Don'ts

### Do:
- **Do** use CSS variables for every color so dark mode and the eight accents keep working.
- **Do** keep controls at 44px and visible 3px accent focus rings (WCAG AA).
- **Do** animate only transform and opacity, and respect `prefers-reduced-motion`.
- **Do** use pills for actions and filters, 14px radius for containers.
- **Do** let numbers be tabular and let the live feed and dashboard carry the energy.

### Don't:
- **Don't** add a second accent or use status colors as decoration.
- **Don't** load webfonts or add a build step; the system stack and no-build SPA are fixed.
- **Don't** put borders around cards; separation comes from shadow in light mode and tone in dark.
- **Don't** convey state by color alone (add text, icon or position).
- **Don't** put a literal white on an accent fill, or tint badge text with the raw status hue.
