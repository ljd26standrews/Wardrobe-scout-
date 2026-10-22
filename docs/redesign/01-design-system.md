# Wardrobe Scout design system (first redesign)

One visual language for every screen: calm, confident and photo-first. Navigation is clear, actions are
immediate, and the evidence behind any claim is easy to inspect. This document defines the system first; the
screens implement it (`frontend/src/app/globals.css`, `tailwind.config.ts`, `src/components/ui/*`).

## 1. Research notes and their limits

The brief named four references. None of them could be opened directly from the build environment:

- linear.app, help.pinterest.com and ssense.com do not resolve (DNS) from the sandbox;
- Apple's Human Interface Guidelines page is rendered by script and returned only its title.

What follows comes from search-engine extracts of those organisations' own pages where available. Points taken
only from third-party write-ups are marked *(secondary)*. Nothing here is a claim about how popular any of these
products are.

| Reference | What the available material says | What Wardrobe Scout takes from it | What it does not take |
|---|---|---|---|
| Linear, "How we redesigned the Linear UI" (2024) and changelog | The redesign adjusted the sidebar, tabs, headers and panels to reduce visual noise, keep alignment, and increase the hierarchy and density of navigation. Themes are generated from a few base colours (background, text, accent), with shades derived for borders and raised surfaces. Default themes got more contrast; a later refresh made headers, navigation and view controls consistent and the sidebar slightly dimmer. The rollout was staged: stress-test the main views, document component behaviour, ship the chrome behind a flag. | A quiet, slightly dimmer sidebar. One page-header pattern used everywhere. Colour tokens derived from a few roles (surface, text, one accent). More contrast for secondary text than the current UI. Keyboard-first actions. | Its dense issue-tracker layout and its look: this is a shopping app, and photos lead. |
| Pinterest Help: "Save Pins", "All about Pinterest" | Saving happens from the close-up view: one Save action, with a board chooser beside it (or a new board). On mobile, Save sits at the bottom of the close-up. Saved items live in boards the person names. | One-tap Save with an optional list chooser. The detail view is where you inspect and save. Lists are named, plain collections. | Decorative boards, cover collages, social features. |
| SSENSE (could not be observed; *(secondary)* design critiques) | Critiques describe a near-monochrome, minimal site: a stark white product grid, garments photographed on plain backgrounds, short top-level navigation, serif headlines with a utilitarian sans. The main criticism is limited, hard-to-use filtering. | The photo dominates on a plain neutral ground. Product metadata is quiet, small and consistent (seller, title, price). | Its weak filtering: here filters are always one tap away and show what is applied. Its typefaces and editorial layouts. |
| Apple Human Interface Guidelines (Apple design tips; HIG figures *(secondary)*) | Controls at least 44 × 44 pt so they can be tapped accurately. Honour Reduce Motion. Check contrast in light, dark and increased-contrast modes. Tab bars on compact screens, sidebars on larger ones. | 44 px minimum touch targets. Bottom tabs on phones (four destinations plus More); a sidebar from 1024 px. `prefers-reduced-motion` turns movement off. Contrast targets in §3. | Platform-specific controls and materials (blur and glass effects). |

Sources:
- [Linear: How we redesigned the Linear UI](https://linear.app/now/how-we-redesigned-the-linear-ui)
- [Linear changelog, 2024-03-20 new UI](https://linear.app/changelog/2024-03-20-new-linear-ui)
- [Pinterest Help: Save Pins](https://help.pinterest.com/en-gb/article/save-pins-on-pinterest)
- [Pinterest Help: All about Pinterest](https://help.pinterest.com/en-gb/guide/all-about-pinterest)
- [Pratt IXD: Design critique of SSENSE](https://ixd.prattsi.org/2023/09/design-critique-ssense/) *(secondary)*
- [Apple: UI design dos and don'ts](https://developer.apple.com/design/tips/)
- [Apple HIG: Buttons](https://developer.apple.com/design/human-interface-guidelines/buttons)

## 2. Principles

1. **The photo leads.** Garments are shown whole: `object-fit: contain` in a neutral image well with a stable
   aspect ratio, never cropped to fill. Size charts and text graphics are never the lead image; they live under
   Sizing.
2. **One decision at a time, instantly.** Like, not now, skip and dislike are single taps or keys. Reasons are
   optional and come after the decision. Undo is always offered. No animation delays the next decision.
3. **Say what is known, and how.** A calibrated estimate appears only when there is one. "Learning pick",
   "taste ranking" and "qualified" are distinct and named in plain words. Quality evidence stays separate from
   taste and is inspectable, with sources grouped and linked.
4. **Quiet chrome.** One page-header pattern, hairline borders instead of shadows, one accent colour. No
   gradients, hero banners, nested cards, "AI" badges or crowded score chips.
5. **No jargon in shopping flows.** The words are "matches your taste", "learning pick" and "evidence". Model
   terms (calibration, Brier, logit) stay on Insights and Settings.

## 3. Tokens

All colours are HSL triplets in CSS variables (`globals.css`), redefined for dark mode (system preference, or
`data-theme`).

### Colour roles

| Token | Role | Light | Dark |
|---|---|---|---|
| `--background` | page | `40 20% 98.5%` | `30 6% 7%` |
| `--surface` | sidebar, panels, toolbars (slightly dimmer than the page) | `40 14% 96%` | `30 5% 9.5%` |
| `--image-well` | behind every product photo | `36 10% 93%` | `30 4% 13%` |
| `--foreground` | primary text | `30 10% 10%` | `38 20% 92%` |
| `--muted-foreground` | secondary text (≥ 4.5:1 on background and surface) | `30 6% 34%` | `34 8% 68%` |
| `--border` | hairlines | `34 12% 86%` | `30 5% 20%` |
| `--primary` | the one accent: primary actions, selection, focus ring, links | `222 47% 31%` | `218 60% 72%` |
| `--primary-foreground` | text on the accent | `40 33% 98%` | `222 47% 12%` |
| `--secondary` / `--accent` | neutral fills: secondary buttons, hover, selected rows | `36 12% 92%` / `36 12% 90%` | `30 5% 15%` / `30 5% 18%` |
| `--destructive` | errors and irreversible actions only | `4 55% 42%` | `4 60% 64%` |
| `--caution` | warnings (for example, "estimates look optimistic") | `32 70% 32%` | `34 65% 62%` |

Status colours (`--destructive`, `--caution`, and `--positive` for "evidence supports") are semantic, not
decorative. They appear only where something needs attention. Love uses the accent, not a second hue. (`--accent`
keeps its Tailwind/shadcn meaning, a neutral hover fill; the brand accent is `--primary`.) Contrast targets: text ≥ 4.5:1 and large text or UI marks ≥ 3:1 against the surface
they sit on. They are checked in `frontend/scripts/contrast.mjs`.

### Typography

One family, Inter (variable), with tabular figures for prices and counts. The serif display face is retired:
it was decorative.

| Style | Size / line height | Weight | Use |
|---|---|---|---|
| `title` | 28/34 (desktop), 22/28 (phone) | 600 | Page titles |
| `heading` | 18/24 | 600 | Section headings |
| `body` | 15/22 | 400 | Text |
| `ui` | 14/20 | 500 | Controls, product titles |
| `meta` | 13/18 | 400 | Seller, price, secondary lines |
| `caption` | 12/16 | 500 | Labels (sentence case, no letter-spaced capitals) |

### Spacing, size and shape

- **Spacing:** 4 px grid: 4, 8, 12, 16, 20, 24, 32, 40, 48, 64.
- **Touch targets:** at least 44 × 44 px on touch layouts. Pointer layouts may show 32–36 px controls, but
  their hit areas still reach 40 px.
- **Radii:**
  - 6 px: controls and images;
  - 10 px: panels and menus;
  - 16 px: sheets;
  - full: chips and segmented controls.
- **Borders:** 1 px `--border`. Separation comes from borders and space, not shadows.
- **Elevation:** two levels. Content is flat. Overlays (menus, dialogs, sheets, the side panel) get one shadow,
  `0 12px 32px -12px rgb(0 0 0 / .25)`, plus a border.
- **Images:**
  - tiles: aspect ratio 4:5;
  - detail view: 4:5 up to 3:4;
  - always `contain` in `--image-well`;
  - a photo that fails to load shows a plain "Photo unavailable" well, never a stand-in image.
- **Icons:** Lucide, stroke 1.75: 16 px inline, 20 px in navigation and actions.

### Focus, density and motion

- **Focus:** `:focus-visible` draws a 2 px `--primary` outline with a 2 px offset on every interactive element.
  It is never removed without a replacement.
- **Density:**
  - phone: comfortable (44 px targets, one or two grid columns);
  - desktop: compact lists and up to five grid columns.
- **Motion:** 120–180 ms ease-out, for state changes only (hover, open, close). There are no entrance animations
  on decisions: the next piece is shown at once. With `prefers-reduced-motion: reduce`, transitions and
  transforms are off.

## 4. Components

- **AppShell.**
  - Desktop (≥ 1024 px): a sidebar of 232 px on `--surface`. It holds the wordmark (text only); primary
    navigation (Discover, Browse, Saved, Compare); a "Your data" group (Wardrobe, Sellers, Insights, Evaluate);
    and, at the foot, Settings, the theme switch and the scan status.
  - Below 1024 px: a compact top bar with the page title, and a bottom tab bar (Discover, Browse, Saved,
    Wardrobe, More). More opens a sheet with the remaining routes.
  - Every existing route keeps its URL.
- **PageHeader.** Title, an optional one-line description, and actions on the right (wrapping on phones).
- **ProductTile.**
  - An image well (4:5, contain), then the title (two lines at most), then seller · price (with ≈ £ when
    converted).
  - At most one quiet status line: "Learning pick", "Saved to Later" or "Matches your taste ≈80%". The last
    appears only with a calibrated estimate, never next to quality.
  - Hover or focus shows quick actions (Like, Not now, Dislike, Open). On touch they sit in the tile's
    footer row.
- **ActionBar** (focused rating view): Dislike · Skip · Not now · Like · Love, plus Open and Undo.
  - Keyboard: D / → / W / S / L / Enter / Z.
  - It is fixed at the bottom on phones, beside the details on desktop.
- **SidePanel.** A right-hand panel (desktop) with the product details. It is opened from any grid with
  `?piece=<id>` in the URL, so the panel is shareable and Back closes it. It always offers "Open full page".
- **Sheet / Dialog.** For reasons, filters and the More menu. On phones the sheet slides up and keeps a
  visible close button.
- **StatusLine.** A single sentence on what the feed is and why: qualified, taste ranking, learning, empty,
  stale or failed.
- **EmptyState.** What happened, why, and the one or two actions that help. Never placeholder products.

## 5. Screens

- **Discover.** "For you" and "Learning picks" are tabs, Browse All is its own route, and the selectivity mode
  is a compact segmented control. Two views:
  - **Focus:** one piece, its photo large, its details and the ActionBar;
  - **Grid:** ProductTiles with quick actions.

  Decisions remove the piece at once. A response fetched before a decision never brings the piece back. The
  scroll position and filters survive opening and closing a piece.
- **Product details** (`/product/[id]` and the SidePanel):
  - a gallery with view thumbnails, arrow keys and click-to-zoom (Esc closes zoom);
  - title, seller and price (original, the conversion with its rate, and landed cost separately);
  - actions;
  - "Why it's suggested" (taste, in plain words, with the calibrated estimate when there is one);
  - Sizing, with sizes and the size-chart images;
  - Materials (claims labelled as the seller's);
  - quality evidence grouped by source, with links;
  - other offers of the same garment;
  - source links.
- **Saved.** Lists as filter chips with counts (Loved, Shortlist, Later, Maybe, Wishlist), plus category and
  search filters.
  - Large photos, each with its list label.
  - Purchase notes: the size to check and a note, editable in place.
  - Select pieces to Compare. Move between lists. Remove, with Undo.
  - No decorative boards.
- **Supporting screens** (Browse, Compare, Wardrobe, Sellers, Insights, Evaluate, Settings, Unlock) use the same
  shell, header, tokens and components. Scan states are unchanged. Evaluate stays blinded: no seller, ranking
  reasons, scores or quality.

## 6. Checks

- Contrast: `node frontend/scripts/contrast.mjs` (token pairs, light and dark).
- Layout and behaviour: Playwright at 390, 768 and 1440 px, in light and dark, with reduced motion; keyboard
  paths; focus visibility; touch-target sizes; the edge-case catalogue (long titles, missing prices, unknown
  currency, broken photos, landscape photos, many offers, nothing to recommend).
- Before and after screenshots: `docs/redesign/screens/{before,after}`.
