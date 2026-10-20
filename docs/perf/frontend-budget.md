# Mobile performance budget

The budgets NearShop's customer pages must meet on a low-end Android phone over slow 4G, how they are
measured, and the measurements. Scorecard check 1.5; prompt P2.9; plan section 8.14. The CI job
`.github/workflows/frontend-quality.yml` checks the lab budgets on every pull request that touches
`frontend/` and fails when one is missed. P2.10 (accessibility) and P7.10 (the Kannada interface audit) add
their checks to the same job.

**Status (20 Oct 2026, after the review fixes):** lab and first-load budgets met in 3 consecutive local runs
of the job's steps (below), under two method choices that still need sign-off (see "Method choices awaiting
sign-off"); the CI job itself has not run yet (it runs on the first pull request that touches `frontend/`).
Reference phone: not measured (B21, the phone, is not bought yet). Field: not measured (needs Sentry switched
on in staging and production, then 28 days of Android traffic after release).

## Budgets

**Lab** (Lighthouse, median of 3 runs per page, in CI). Pages: home (`/`), search with a query
(`/search?q=umbrella`), a product page and a shop page (the first result of that search in the seeded
demo city).

| Metric | Budget |
|---|---|
| Lighthouse performance score | 85 or more |
| Largest Contentful Paint (LCP) | 2.5 s or less |
| Total Blocking Time (TBT) | 200 ms or less |
| Cumulative Layout Shift (CLS) | 0.1 or less |
| First-load JavaScript per route | 200 KB gzip or less |

**Field** (real users, Android, p75 over 28 days, from Sentry): LCP 2.5 s or less, INP 200 ms or less,
CLS 0.1 or less.

**Reference phone** (B21): product page LCP of 3.0 s or less over Ballari 4G, in 5 of 5 cold loads.

## Lighthouse settings (confirmed from the installed Lighthouse)

The job runs Lighthouse CI 0.15.1 through `npx` (never an app dependency), which bundles Lighthouse 12.6.1.
Its defaults, read from `lighthouse/core/config/constants.js` and the Lantern simulation constants
(`@paulirish/trace_engine/models/trace/lantern/simulation/Constants.js`) in that version:

- form factor mobile, screen emulation 412 × 823 CSS px at device scale factor 1.75 (Moto G Power), mobile
  user agent;
- throttling method `simulate` (Lantern) with the `mobileSlow4G` profile: RTT 150 ms, throughput 1.6 Mbps
  (1,638.4 Kbps), which applied in DevTools terms is a request latency of 562.5 ms (150 × 3.75), download
  1,474.56 Kbps and upload 675 Kbps (× 0.9), and a 4× CPU slowdown.

`frontend/lighthouserc.json` keeps these defaults; it sets 3 runs, the performance category, and asserts the
four lab budgets on the median of each value (`aggregationMethod: median`). It changes one gathering setting,
not the emulation or the throttling: `pauseAfterLoadMs` 5,250 ms instead of 1,000 ms (the value Lighthouse
itself uses when it does not simulate throttling), so the trace runs long enough after the load event to hold
the Sentry SDK's download and start-up (below).

## How the CI job measures

1. MongoDB starts as the single-node replica set from `docker-compose.yml` (`make db-up`).
2. The seed builds the demo city (`make seed`) with a demo password and secret key generated for the run.
3. The API starts with development settings, the light embedding provider (no model download) and rate
   limits off (every request comes from one address).
4. The frontend is built for production (`make build`) and served with `next start`.
5. **HTTP/2.** `next start` speaks HTTP/1.1, and Lantern then simulates six connections per origin, so 15
   scripts, 2 stylesheets and a font queue behind each other. The deployed site is served by Vercel over
   HTTP/2, one multiplexed connection. Lighthouse therefore loads the pages through a small HTTP/2 TLS proxy
   (Node's `http2`, a certificate made for the run, Chrome started with `--ignore-certificate-errors`) in
   front of `next start`. The HTTP/1.1 numbers are kept below for reference; they are not the budget. This is
   a method choice awaiting sign-off (below): over plain HTTP/1.1, LCP misses 2.5 s on all four pages.
6. **Error tracking included.** The build has a placeholder `NEXT_PUBLIC_SENTRY_DSN` on port 9, which browsers
   refuse to connect to, so nothing is sent anywhere. The Sentry SDK loads as it does on staging and
   production: 1 s after the load event, when the main thread is idle. Lighthouse waits 5.25 s after the load
   event (`pauseAfterLoadMs`), so every trace holds the SDK's download (152.8 KB gzip) and start-up, and they
   count in Total Blocking Time. The job checks this: it fails if any Lighthouse run did not request the SDK's
   chunk (found by content, `sentry.javascript`, in `.next/static/chunks`), and its summary shows the SDK's
   start-up time per page. (Before the review fixes the SDK waited 3 s after the load event plus up to 5 s of
   idle time, and Lighthouse finished first: the lab measured none of its cost, although this section said it
   did.)
7. **First-load JavaScript** per route: the gzip size (level 9) of every script the route's served HTML
   loads in a modern browser (`<script src>`), summed. The `nomodule` polyfill script (38.6 KB gzip) is not
   counted: browsers that support ES modules, every browser the site targets, neither download nor run it. The
   job's summary shows the total with it beside the budget figure. Plan section 14 says "summed over the
   script tags in each page", which would count it; with it, all four routes are over 200 KB (below). This is
   a method choice awaiting sign-off. The plan's 18 Oct figures (416 to 441 KB, at commit c281705) cannot be
   matched to either method: this method gave 431 to 455 KB at cf98581, and 469 to 494 KB with the
   polyfill, after P2.1 to P2.8 had changed the code.
8. Lighthouse runs 3 times per page; the job asserts the budgets on the medians and writes a table of both
   checks to the run's summary. Any miss fails the job.

To run the same checks locally, follow the job's steps with the local database (never `backend/.env`:
it may point at a deployed database).

## Measurements (local production build, Apple M-series laptop, 20 Oct 2026)

"Before" is commit cf98581 (P2.8). "After" is this change with the review fixes, both built and measured the
same way, with the placeholder DSN. (The first version of this change, without the review fixes, measured
189.4 / 192.8 / 194.9 / 193.1 KB and Lighthouse TBT of 15 to 19 ms, without the SDK in the trace.)

### First-load JavaScript (KB gzip, level 9)

| Route | Before | Before, with the nomodule polyfill | After | After, with the nomodule polyfill | Budget (modern browsers) |
|---|---:|---:|---:|---:|---|
| `/` | 431.1 (24 scripts) | 469.4 | **189.8** (14 scripts) | 228.4 | pass |
| `/search?q=umbrella` | 452.5 (27) | 490.8 | **193.4** (15) | 232.0 | pass |
| `/product/880` | 455.3 (27) | 493.6 | **195.8** (15) | 234.4 | pass |
| `/shops/kalpana-steel-plastics-parvathi-nagar` | 437.1 (25) | 475.4 | **193.7** (15) | 232.3 | pass |

About 134 KB of what is left is Next.js and React DOM; React Query, the site shell (header, search box, tab
bar, Radix Slot, the class merger) and each page's own code make up the rest. The margin is 5 to 11 KB: a
new dependency on a shared screen will need a lazy import.

### Lighthouse, median of 3, through the HTTP/2 proxy (the budget)

| Page | Before: score / LCP / TBT / CLS | After, run 1 | After, run 2 | After, run 3 |
|---|---|---|---|---|
| `/` | 79 / 4,507 ms / 146 ms / 0.121 | 99 / 2,106 / 82 / 0.002 | 99 / 2,106 / 83 / 0.002 | 99 / 2,106 / 83 / 0.002 |
| `/search?q=umbrella` | 66 / 6,543 / 183 / 0.182 | 98 / 2,357 / 46 / 0.023 | 98 / 2,351 / 45 / 0.023 | 98 / 2,350 / 45 / 0.023 |
| product | 89 / 3,456 / 104 / 0.064 | 99 / 2,255 / 57 / 0.005 | 99 / 2,255 / 52 / 0.005 | 99 / 2,255 / 49 / 0.005 |
| shop | 91 / 3,310 / 90 / 0.012 | 99 / 2,107 / 80 / 0.012 | 99 / 2,106 / 83 / 0.012 | 99 / 2,105 / 83 / 0.012 |

All four pages pass every lab budget in 3 consecutive runs, with the Sentry SDK loaded in all 36 Lighthouse
runs (its start-up: a median of 88 to 101 ms of main-thread time at 4x CPU). TBT now includes it: 38 to 123 ms
in single runs, against 200 ms (15 to 19 ms before the review fixes, without it). Lighthouse scales the host's
own task times by 4, so a slower CI machine reads higher.

While fixing the static maps, a version that loaded the shop page's small map as soon as the page was idle
(it is fully on screen at load on a 412 × 823 phone, y 585 to 807) put MapLibre's start-up into the same
seconds as the SDK's: shop page TBT medians of 106 to 170 ms and single runs up to 205 ms. Static maps
therefore wait for the first scroll (below).

Proof that the check bites (first version of this change): with the result card put back on `motion` (the
pre-P2.9 file), the same steps fail on `/search?q=umbrella`, first-load JavaScript 237.1 KB and LCP 2,587 ms.

### Lighthouse through plain HTTP/1.1 (`next start` directly; for reference, not the budget)

Measured on the first version of this change (before the review fixes).

| Page | Before | After |
|---|---|---|
| `/` | 73 / 6,327 ms / 94 ms / 0.121 | 96 / 2,772 / 8 / 0.002 |
| `/search?q=umbrella` | 65 / 9,307 / 180 / 0.182 | 90 / 3,714 / 17 / 0.023 |
| product | 79 / 5,364 / 94 / 0.064 | 93 / 3,224 / 8 / 0.005 |
| shop | 80 / 5,069 / 103 / 0.012 | 94 / 3,074 / 9 / 0.012 |

## What changed to reach the budgets

- **No animation library on shared screens.** The result card, the mobile tab bar, the header search box, the
  home page and the search page use CSS (tw-animate-css and transitions) instead of `motion`; the global
  `MotionConfig` is gone from `providers.tsx`. `globals.css` stops CSS animations and transitions for the
  reduced-motion setting, but that rule does not reach `motion`'s JavaScript animations, so every component
  that still uses `motion` wraps its own output in `MotionConfig reducedMotion="user"` (transform animations
  such as the `y: 8` entrances then jump to their end; opacity still fades). `motion` is still imported on
  these customer routes, as first-load JavaScript of those routes only: `/account/reservations/[id]` and
  `/account/orders/[id]` (their views and `components/fulfillment/progress.tsx`), `/register/shop` and `/ai`;
  and on the owner console (`/shop`, `/shop/reservations`, `/shop/orders`, `/shop/catalog`, through
  `components/owner/queue-cards.tsx` and `shop/catalog/catalog-view.tsx`).
- **Error tracking off the critical path.** The Sentry SDK (152.8 KB gzip, 465 KB uncompressed; about twice
  React DOM) loads 1 s after the load event, when the main thread is idle (at most 2 s later;
  `src/lib/sentry-browser.ts`), and earlier when it is needed sooner:
  - an uncaught error or unhandled promise rejection before then is kept (up to 20), loads the SDK at once,
    and is reported once it has started, unhandled and with the SDK's own mechanism
    (`auto.browser.global_handlers.onerror` / `onunhandledrejection`); an error boundary's error loads it at
    once too;
  - hiding the page before then (switching app or tab, leaving) loads the SDK at once; a phone keeps a hidden
    page running for a while, so it usually still starts, and it then replays the hide it missed, so the visit's
    LCP, CLS, INP and TTFB are sent. For this, Web Vitals are measured per page load only
    (`webVitalsIntegration` with soft navigations and back/forward-cache restores off): in the SDK's default
    mode, LCP is finalised only by a trusted click, key press or visibilitychange, which such a page never
    gets again.
  The pageload span starts at the navigation's time origin and the Web Vitals observers read buffered entries,
  so the vitals are complete. Still lost: errors thrown before the site's own JavaScript runs, a page unloaded
  before the SDK's download finishes, and interactions under 104 ms before the SDK started (the browser
  buffers only longer ones), which can only make INP read low. Checked locally with a capturing endpoint (20 Oct
  2026): an error and a rejection thrown just after load arrive once each, unhandled; a search page hidden
  0.7 s after load, before the SDK, sends LCP, CLS, INP and TTFB, tagged `/search`, with no query string.
- **Menus and panels load on use.** The location picker's popover, the search filters sheet, the account menu
  and notifications (signed in only), the reserve and delivery dialogs and the toast viewport are loaded when
  first needed (or 2 s after load, for toasts) instead of with the page. The reserve and delivery dialogs start
  loading when the pointer or keyboard focus reaches their buttons or a finger touches one, and, for a
  signed-in customer, once the page is idle; a tap before the code has arrived shows an "Opening…" spinner over
  the page at once, which also stops repeated taps. The search page's sort control is a
  native select, the shop page's "in stock only" switch a native checkbox, and the verified badge's
  explanation an SVG title, all without Radix.
- **Catalogue icons hydrate after the page.** The 144 catalogue icons (about 19 KB) are server-rendered and
  hydrate when their chunk arrives; lucide's full icon-name map (about 21 KB) loads only for an icon outside
  the catalogue.
- **The map loads only when wanted.** MapLibre (about 300 KB gzip), its stylesheet (10.5 KB gzip, no longer
  render-blocking on every page), style and tiles load when the map scrolls into view, once the page has
  loaded and the browser is idle, or at once when the customer taps "Show map". An interactive map (search,
  shops), the page's main feature, starts when it comes within 200 px of the screen. A small static map (a
  shop's, a reservation's or an order's location) loads once the customer has scrolled the page and it is on
  screen: one already on screen at load (the shop page's, on a phone) waits for the first scroll or the tap, so
  its start-up never lands on the visitor's first interactions. A placeholder of the same size holds the
  space. Checked locally on a 412 × 823 phone: neither the shop page's nor the product page's map loads
  before a scroll (the product page's is at y 2,952), and both load and render once scrolled into view.
- **One font on the critical path.** Only the body face (Geist) is preloaded. Geist Mono (codes) is not
  preloaded, so it is fetched only where a code is shown. The display face (Bricolage Grotesque, 41 KB, fetched
  on every page for headings) is dropped; headings use Geist.
- **Images.** Thumbnails and shop images use the 320 px image from P2.7 (`thumbnail_url`), with width and
  height set and lazy loading, on every customer screen, the account page's reservation and order lists
  included; only the product page's large image uses the full photo, loaded eagerly with high priority.
- **Server-rendered home hero.** The header search box read the query string on every page, which made the
  whole statically rendered home page render in the browser only; it now reads it only on the search page.
- **Layout shifts.** The home page reserves the city's numbers before they load; two overflows that made a
  phone zoom the page out (the rating's screen-reader text in the shop carousel, and long names in the product
  page's recommendations) are fixed.
- **Less contention on slow links.** The tab bar, header and logo links are not prefetched (on every page they
  fetched four other screens' code and data while the page's own data was loading), and the default
  `favicon.ico` was cut from 26 KB to 2 KB.

## Method choices awaiting sign-off

The lab and first-load results above are PASS only under two choices this change made beyond the prompt's
and plan section 8.14's wording. Someone who owns check 1.5 needs to accept them, or the budgets are not met:

1. **First-load JavaScript leaves out the `nomodule` polyfill script.** Modern browsers skip it, so it is not
   part of what a customer's phone downloads; plan section 14's "summed over the script tags in each page"
   would count it. With it, every route is 28 to 35 KB over 200 KB (228.3 to 234.2 KB). Next.js 16 always
   emits it; removing it would need a framework change. The job reports both figures.
2. **Lighthouse loads the pages over HTTP/2** (a local TLS proxy in front of `next start`), as Vercel serves
   the deployed site, rather than over `next start`'s HTTP/1.1. Emulation and throttling are Lighthouse's
   defaults either way. Over HTTP/1.1, LCP misses 2.5 s on all four pages (2.8 to 3.7 s, table above).

## Field Web Vitals (Sentry)

`src/instrumentation-client.ts`, `src/lib/sentry-browser.ts` and `src/lib/observability.ts`: from each sampled
page load (10%, 25% on search), Sentry's browser tracing sends LCP, CLS and INP as spans and TTFB on the
pageload span, including visits hidden before the SDK started (above). Before they
are sent, `scrubSpan` sets every page name and page URL to the route template (`/product/[id]`, never the id,
the slug or a query string; `nearshop.route` on every span), removes query strings everywhere, keeps only the
safe request headers, masks phone numbers, emails and addresses, and adds the network's effective connection
type (`network.connection.effective_type`). Checked locally against a capturing endpoint: spans for LCP, CLS,
INP and TTFB, tagged `/search` and `/product/[id]`, with no query string and no phone number.

Volume against the free Developer plan (5,000 errors and 5 million spans a month, Sentry pricing page read on
20 Oct 2026): a sampled page load sends about 38 browser spans (the pageload span, its resource and timing
spans, and the vitals). At the pilot's scale, assumed at 3,000 monthly active customers and about 60,000 page
loads a month (about 15% sampled, as a third are searches), that is about 9,000 sampled loads and 340,000
browser spans a month, plus the server's spans: well inside 5 million. At 10 times the pilot it is about
3.4 million browser spans: lower `TRACES_SAMPLE_RATE` in `src/lib/observability.ts` before then.

Reading the field budget: Sentry, Insights > Web Vitals (or Explore > Spans), filter `browser.name` Chrome
Mobile / Android, group by `nearshop.route`, p75 over 28 days.

## Reference phone (B21)

Not measured: the reference phone and the Ballari prepaid SIM are not bought yet (B21, due 30 Nov 2026).
Procedure once they are: Chrome on the phone, mobile data on the Ballari SIM (Wi-Fi off), the production or
staging product page; for each of 5 cold loads clear the site's data and cache, open the URL, and read LCP
from Chrome DevTools remote debugging (Performance panel) or `chrome://inspect`. Record:

| Date | Phone model (RAM) | Network | URL | LCP, 5 cold loads | Pass (all 3.0 s or less) |
|---|---|---|---|---|---|
| not measured | | | | | |

## Open items

- The CI job's first run, and 3 consecutive passing runs, on a pull request.
- Web Vitals from staging in Sentry (needs `NEXT_PUBLIC_SENTRY_DSN` set in Vercel for staging).
- Field budget after release (G3, 15 Apr 2027).
