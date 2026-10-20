# Accessibility

NearShop's main customer and owner flows meet WCAG 2.2 level AA, and CI fails any change that breaks them.
Scorecard check 1.5; plan section 8.15; prompt P2.10. P7.10 re-runs every check here in Kannada. Admin
screens are out of scope.

## Target and screens

WCAG 2.2 level AA, at 360 by 800 CSS pixels (an entry-level Android phone), in every shipped language
(English today; P7.10 adds Kannada), on these screens and their dialogs:

| Who | Screen | Path | Dialogs opened by the CI check |
|---|---|---|---|
| Customer | Home | / | |
| Customer | Search | /search?q=umbrella | Filters sheet |
| Customer | Product | /product/{id} (signed out and signed in) | Reserve dialog, delivery dialog |
| Customer | Shop | /shops/{slug} | |
| Customer | Sign-in | /login | |
| Customer | Register | /register | |
| Customer | Reservation detail | /account/reservations/{id} | Cancel dialog |
| Customer | Order detail | /account/orders/{id} | Cancel dialog |
| Customer | Account | /account | |
| Owner | Overview | /shop | Menu sheet |
| Owner | Reservation queue | /shop/reservations | Decline dialog |
| Owner | Order queue | /shop/orders | Decline dialog |
| Owner | Inventory | /shop/inventory | New product dialog, edit product dialog |
| Owner | Import | /shop/import | (the preview of an uploaded stock file is audited too) |

## Rules every screen follows

- **Text contrast**: 4.5:1 or more for normal text, 3:1 for large text (24 px, or 18.7 px bold) and for
  icons, borders and focus indicators that carry meaning (WCAG 1.4.3, 1.4.11). The colour tokens in
  frontend/src/app/globals.css are chosen for this in both themes; use the tokens, not new colours. Brand
  text on the page and white text on a brand button were 3.7:1 before P2.10; the brand colour is now darker
  (oklch 0.54 0.19 36), giving 5.3:1 and 5.2:1. Destructive text on its own tint went from 4.1:1 to 5.0:1.
- **Touch targets**: at least 24 by 24 px (WCAG 2.5.8), and 44 by 44 px for primary actions: reserve,
  request delivery, sign in, submit, the main call to action of a dialog. frontend/src/components/ui/button.tsx
  sizes: xs and icon-xs are exactly 24 px (the minimum), default 32 px, lg and icon-lg 44 px. Give primary
  actions size lg (or icon-lg).
- **Focus**: every focusable element shows a visible focus indicator. Buttons draw a solid 2 px ring in the
  ring colour, 2 px away from the button (button.tsx); every other element without its own style gets a 2 px
  outline in the ring colour from globals.css. Never use outline-none without a replacement focus style.
- **Focus not hidden**: the sticky header and the phone's bottom bars never cover the focused element
  (scroll-padding in globals.css). Rows that scroll sideways call revealFocused (frontend/src/lib/utils.ts) so
  a focused chip or card scrolls fully into view.
- **Dialogs**: focus moves into a dialog when it opens and returns to its trigger when it closes. The phone
  bottom sheet (components/common/responsive-dialog.tsx) focuses the sheet itself, so the on-screen keyboard
  does not open over it.
- **Reflow**: nothing scrolls sideways at 360 px (WCAG 1.4.10). Grid columns use minmax(0, 1fr) (Tailwind
  grid-cols-1 on phones) so long content wraps.
- **Names**: icon-only buttons have an aria-label; decorative images and icons are hidden from screen readers;
  form fields have a label.
- **Motion**: animations follow the phone's reduced-motion setting (MotionConfig and the globals.css media rule).

## Automated checks

All run in the frontend-quality CI job (.github/workflows/frontend-quality.yml) on every pull request that
touches frontend/, against the production build on a seeded throwaway database. The job runs every spec in
frontend/e2e, once per language listed in E2E_LANGUAGES.

1. **axe** (frontend/e2e/accessibility.spec.ts, @axe-core/playwright 4.13.0): every screen and dialog above,
   with the rule tags wcag2a, wcag2aa, wcag21a, wcag21aa, wcag22a and wcag22aa. Any serious or critical
   violation fails the job; moderate and minor ones are logged and attached to the report. The same spec
   fails a screen that scrolls sideways at 360 px, which axe does not test. The job summary lists every screen
   with its counts.
2. **Keyboard-only reserve** (frontend/e2e/keyboard-reserve.spec.ts): signed in as the demo customer, with the
   keyboard only, Tab to the home page search box, type a query, Enter, Tab to the first result, Enter, Tab to
   "Reserve for pickup", Enter, Tab to the reserve button in the dialog, Enter; the shop confirms (through the
   API, as the demo owner) and the pickup code must show. At every Tab stop the focused element must match
   :focus-visible, must look different from the same element without focus (the pixels in and around it change
   when it loses focus), and must not be entirely hidden by other content.
3. **ESLint** (frontend/eslint.config.mjs): the whole jsx-a11y recommended rule set is an error for
   frontend/src; make lint runs it.

Both specs sign in as the seeded accounts priya@nearshop.demo and owner@nearshop.demo with the demo password
the CI job generates for its run (NEARSHOP_DEMO_PASSWORD). Before the screens, accessibility.spec.ts makes sure
the customer has an open reservation and an open delivery order at the demo owner's shop.

### Running them locally

Against the local database only (never backend/.env's database): start MongoDB, seed it with make seed and a
NEARSHOP_DEMO_PASSWORD of your own, start the API (make api), build and start the frontend (make build, then
npx next start in frontend/ with NEARSHOP_API_ORIGIN=http://127.0.0.1:8000). Then, in frontend/:

- npx playwright install chromium (once), or set NEARSHOP_E2E_CHANNEL=chrome to use an installed Google Chrome;
- NEARSHOP_DEMO_PASSWORD=<the password you seeded with> npx playwright test

Settings are listed at the top of frontend/playwright.config.ts. Reports and traces go to the system's
temporary folder (nearshop-e2e), not into the repository.

### ESLint rules that stay off, and the exceptions

The recommended set leaves three rules off, and so does NearShop:

| Rule | Why it stays off |
|---|---|
| jsx-a11y/anchor-ambiguous-text | Off in the plugin's recommended set: it flags any link text such as "more" by word list, without context. NearShop's links name their target ("All 17 reviews", "Find it at another shop"), and the screen-reader walkthrough below checks link names in context. |
| jsx-a11y/control-has-associated-label | Off in the recommended set: it cannot see labels given through components (Label, aria-label on a wrapped control) and reports false errors on every shadcn/ui control. axe's button-name, label and link-name rules check the rendered page instead, in CI. |
| jsx-a11y/label-has-for | Deprecated by the plugin in favour of label-has-associated-control, which is on. |

label-has-associated-control is on with one option: the shadcn/ui components Input, Textarea, Checkbox,
Switch, RadioGroupItem and SelectTrigger count as controls, since each renders a native control.

Three lines disable jsx-a11y rules (the input-group line disables two), each with its reason beside it:

| File | Rule | Why |
|---|---|---|
| src/app/(auth)/login/login-form.tsx | no-autofocus | The authentication-code field appears only after the user submits the form; moving focus to it is what the user expects. |
| src/components/site/search-box.tsx | click-events-have-key-events | The suggestion list follows the WAI-ARIA combobox pattern: options are not focusable, and the input handles the arrow keys, Enter and Escape. |
| src/components/ui/input-group.tsx | click-events-have-key-events, no-noninteractive-element-interactions | A click on an input's icon focuses the input, for the mouse; keyboard users Tab to the input itself. |

## Manual checks on the reference phone

The reference phone is the one in plan section 8.14 (B21): an entry-level Android phone with 3 GB of RAM or
less, named in docs/perf/frontend-budget.md, with the Ballari prepaid SIM. Use Chrome on the phone, against
staging (with seeded demo data) or production with a test customer account.

### TalkBack walkthrough: search, reserve and the code

Settings: Accessibility, TalkBack on; speech rate default; Chrome zoom 100 %. Navigate by swiping right (next
item) and left (previous), double-tap to activate; do not touch the screen to explore unless a step says so.
Each step passes when TalkBack says what the "Hear" column describes, and the action works.

| Step | Do | Hear (pass) |
|---|---|---|
| 1 | Open the home page. | The page title, then "Skip to content, link". |
| 2 | Swipe right to the search box, double-tap. | "Search products, combo box" (or "edit box"), and the keyboard opens. |
| 3 | Type "umbrella", press the keyboard's search key. | The results page loads; TalkBack reads the new title. |
| 4 | Swipe right to the first result. | The product name and "from" price as one link; no "image" or "unlabelled" stops for the picture. |
| 5 | Double-tap it. | The product page; the heading is the product name. |
| 6 | Swipe right to "Reserve for pickup", double-tap (sign in first if asked: the email and password fields are read with their labels, and errors are announced). | "Reserve for pickup, button", then the sheet: "Reserve for pickup, dialog" and its description. |
| 7 | Swipe through the sheet. | Quantity: "Decrease quantity, button, disabled", "1", "Increase quantity, button"; the three steps; "Note for the shop (optional), edit box"; the reserve button with the price. Swiping never leaves the sheet. |
| 8 | Double-tap the reserve button. | The reservation page: "Waiting for the shop". |
| 9 | When the shop confirms (staff confirm it from the owner screens), swipe to the code. | "It's held for you", then "Pickup code" and the code's characters. |

### Largest Android font size at 360 px

Settings: Display, Font size to the largest step, and Display size to the largest (Android 13 and later:
"Font size and style"). In Chrome, Settings, Accessibility, text scaling 100 % and "Force enable zoom" off.

On each customer screen in the table at the top, and in the reserve and delivery dialogs: nothing is cut off or
overlapping, no text is hidden behind the sticky header or the bottom bar, and nothing scrolls sideways (drag
the page left: it must not move). Rows of chips and cards that scroll sideways on purpose are fine.

## Results log

Newest first. Automated results come from the frontend-quality job's summary; the counts are axe's failing
nodes (elements), serious or critical.

| Date | Check | Where | Result |
|---|---|---|---|
| 2026-10-20 | axe, 26 screens and dialogs | Local production build, seeded database (the CI job's setup) | Pass: 0 serious or critical, 0 moderate or minor, nothing scrolls sideways. |
| 2026-10-20 | Keyboard-only reserve | Same | Pass. |
| 2026-10-20 | ESLint, jsx-a11y recommended | frontend/src | Pass: 0 errors. |
| 2026-10-20 | The job fails on a violation | Local: the sign-in page's "Show password" button with its aria-label removed | Pass: accessibility.spec.ts failed on axe's button-name (critical). The throwaway pull request that shows the same in CI is still to be opened (below). |
| 2026-10-20 | Baseline before the fixes | Same | Fail; details below. |
| Not yet | TalkBack walkthrough | Reference phone | Not done: the reference phone is not bought yet (B21, due 30 Nov 2026). Record the date, the phone, the Android and TalkBack versions, the tester, and pass or fail for each step. |
| Not yet | Largest font size at 360 px | Reference phone | Not done, as above. Record the date, the phone, the settings and the screens that failed. |

### Baseline before the fixes (2026-10-20)

axe, 24 screens and dialogs reached (the inventory product dialogs could not be opened: at 360 px the inventory
page's "Add from catalogue" button lay over "New product"): 176 serious or critical nodes on 21 of them.

| Rule | Impact | Nodes | Screens | Cause | Fix |
|---|---|---:|---:|---|---|
| role-img-alt | serious | 104 | 10 | Product picture tiles marked role img with an empty name when they are decoration | Decorative tiles are hidden from screen readers (product-thumb.tsx) |
| color-contrast | serious | 41 | 16 | Brand text and white text on brand at 3.7:1; the home page's step numbers at 1.3:1 | Darker brand token (globals.css); step numbers at 3.3:1 and hidden from screen readers (home-view.tsx) |
| aria-prohibited-attr | serious | 21 | 3 | Star ratings: aria-label on a span with no role | role img (review-list.tsx) |
| button-name | critical | 6 | 2 | Select boxes without a name: inventory filters, import preview rows | aria-label (inventory-view.tsx, import/page.tsx) |
| aria-valid-attr-value | critical | 4 | 4 | Filter tabs pointing aria-controls at panels that are never rendered | No aria-controls unless given (ui/tabs.tsx) |

Found after the baseline, by the checks added in P2.10:

- Sideways scrolling at 360 px: shop page (44 px), order detail (136 px), owner overview (248 px), owner
  orders (87 px), owner inventory (the header buttons). Grid columns with minmax(0, 1fr); inventory buttons
  wrap.
- Inventory product dialog: the category select had no name (product-form-dialog.tsx).
- Delivery dialog: when a free-delivery threshold applies, a paragraph sat inside the price list (axe
  definition-list, serious); it is now a dd (delivery-dialog.tsx).
- Keyboard: the logo link and the search result cards showed no focus indicator (outline-none); the search
  box's indicator was too faint to see (a 10 % brand ring); the bottom sheets (reserve, delivery, cancel,
  decline) opened without moving focus into them, leaving focus behind the overlay; a category chip one pixel
  inside the edge of its row took focus without scrolling into view.
- ESLint, jsx-a11y recommended, with the plugin's default options: 8 errors in 5 files: login-form (1),
  catalog-view (2), review-dialog (1), search-box (2) and input-group (2). The two in catalog-view
  (label-has-associated-control) were false: each label wraps a shadcn/ui Input, which renders a native input.
  With the committed config, which counts those components as controls, the baseline is 6 errors in 4 files:
  login-form (1, no-autofocus), review-dialog (1, interactive-supports-focus), search-box (2, no-autofocus and
  click-events-have-key-events) and input-group (2, click-events-have-key-events and
  no-noninteractive-element-interactions).

## Fixes outside P2.10's file list

P2.10 may change only its listed files (frontend/package.json, playwright.config.ts, the two specs, the
frontend-quality workflow, eslint.config.mjs, globals.css, ui/button.tsx and this file). The violations above
that can only be fixed elsewhere are fixed in a follow-up change under the same prompt ID, and acceptance is
judged after it. The follow-up changes these files in frontend/src:

- components/common/product-thumb.tsx, components/shop/review-list.tsx, app/(site)/home-view.tsx
- components/ui/tabs.tsx, app/shop/inventory/inventory-view.tsx, app/shop/import/page.tsx,
  components/owner/product-form-dialog.tsx
- app/(site)/account/orders/[id]/order-view.tsx, app/(site)/account/reservations/[id]/reservation-view.tsx,
  app/(site)/shops/[slug]/shop-view.tsx, app/shop/page.tsx, app/shop/orders/page.tsx,
  app/shop/reservations/page.tsx
- components/brand/logo.tsx, app/not-found.tsx, components/search/result-card.tsx, components/site/search-box.tsx,
  components/common/responsive-dialog.tsx, components/product/delivery-dialog.tsx, lib/utils.ts (revealFocused), app/(site)/search/search-view.tsx,
  app/(site)/shops/shops-view.tsx, app/shop/catalog/catalog-view.tsx
- app/(auth)/login/login-form.tsx, components/fulfillment/review-dialog.tsx, components/ui/input-group.tsx
- app/(site)/product/[id]/product-view.tsx (with home-view and shop-view: the anchors' scroll-mt-20, now that
  globals.css sets scroll-padding-top for the whole page)

frontend/package-lock.json changes with frontend/package.json.

The two commits must land together, in one pull request: the first turns the jsx-a11y rules on, and on its own
it fails `make lint` until the follow-up fixes the files that break them.

## Still to do

- Open a throwaway pull request that removes an icon button's accessible name, confirm the frontend-quality
  job fails on it, close it unmerged, and add the run's link to the results log.
- The TalkBack walkthrough and the largest-font check on the reference phone, once it is bought (B21).
- P7.10: add Kannada to E2E_LANGUAGES in the workflow and re-run everything here in Kannada.
