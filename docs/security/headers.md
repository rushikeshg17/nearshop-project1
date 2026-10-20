# Security headers and content security policy

Every site page and every API response carries the headers below. Scorecard check 2.4; plan section 8.13;
prompt P2.6. The site's headers are set in frontend/next.config.ts, the API's in
backend/app/core/security_headers.py. When a value changes, change this file in the same pull request: the
release check compares the live headers of the home page, a product page and /api/auth/me, fetched through
Vercel, with the tables here.

## Site pages

Every path served by Next.js on Vercel: pages, /_next static files, /maplibre, the icons. Not /api and
/media, which Vercel passes to the API and which carry the API's headers (next section).

| Header | Value | Why |
|---|---|---|
| Content-Security-Policy-Report-Only, then Content-Security-Policy | The policy below | Limits where scripts, styles, images, connections and workers may come from, and stops the site being framed. Report-only first, so nothing breaks while real traffic shows what the policy would block. |
| X-Content-Type-Options | nosniff | The browser uses each file's declared type and never guesses a script or a page from its contents. |
| Referrer-Policy | strict-origin-when-cross-origin | Other sites see only the origin, never the path or query (a search, a reservation id). |
| X-Frame-Options | DENY | No other site may frame NearShop (clickjacking). Kept for browsers that ignore frame-ancestors. |
| Permissions-Policy | camera=(), microphone=(), geolocation=(self) | Location only for NearShop's own pages ("near me"); camera and microphone for nobody. |
| Cross-Origin-Opener-Policy | same-origin | A page NearShop opens, or that opens NearShop, gets no handle on its window. |
| Reporting-Endpoints | csp="<the Sentry security-report URL>" | Where browsers that use report-to send violation reports. Sent only when the build has NEXT_PUBLIC_SENTRY_DSN. |
| Strict-Transport-Security | max-age=63072000; includeSubDomains; preload | Sent by Vercel itself, not by NearShop (decision below). |

### The policy

One directive per row, in the order the header sends them, separated by "; ".

| Directive | Value | Why |
|---|---|---|
| default-src | 'self' | Anything not named below comes from NearShop's own origin only. |
| script-src | 'self' 'unsafe-inline' | The site's own scripts, plus the inline scripts Next.js writes for hydration (no nonces, decision below). Never 'unsafe-eval' outside development: next dev adds it, a production build never does. |
| style-src | 'self' 'unsafe-inline' | The site's own styles, plus inline styles (React style props, and the chart style block in frontend/src/components/ui/chart.tsx). |
| img-src | 'self' data: blob: <media domain> <map tile host> | Photos from /media or the media domain, inline and generated images, and map images. |
| connect-src | 'self' <map tile host> <Sentry ingest host> | API calls (same origin, through Vercel), the map's style, tiles, sprites and glyphs, and error reports. |
| worker-src | 'self' blob: | The map's worker, served from /maplibre (frontend/scripts/copy-maplibre-worker.mjs). |
| font-src | 'self' | next/font serves the fonts from the site itself. |
| object-src | 'none' | No plug-ins. |
| base-uri | 'self' | An injected base tag cannot point relative links elsewhere. |
| form-action | 'self' | Forms submit to NearShop only. |
| frame-ancestors | 'none' | No other site may frame NearShop. |
| upgrade-insecure-requests | (no value) | Any http subresource is fetched over https. Sent only when enforcing: browsers ignore it in a report-only policy, with a console warning. Never in next dev, which is plain http. |
| report-uri | <the Sentry security-report URL> | Violation reports go to Sentry. Sent only when the build has NEXT_PUBLIC_SENTRY_DSN. |
| report-to | csp | The same, for browsers that use the Reporting API (the Reporting-Endpoints header names csp). |

Where the hosts come from, all read when the site is built (Vercel > the project > Settings > Environment
Variables, per environment):

| Placeholder | Variable | Value today |
|---|---|---|
| <media domain> | NEARSHOP_MEDIA_PUBLIC_BASE_URL (its origin) | Left out while unset: photos then come from /media, which 'self' covers. |
| <map tile host> | NEARSHOP_MAP_TILE_ORIGIN | https://tiles.openfreemap.org when unset: the host of the map styles in frontend/src/components/map/shop-map.tsx. Set it if that host changes. |
| <Sentry ingest host> | NEXT_PUBLIC_SENTRY_DSN (its origin) | Left out while unset, with report-uri, report-to and Reporting-Endpoints. |
| <the Sentry security-report URL> | NEXT_PUBLIC_SENTRY_DSN | https://<ingest host>/api/<project id>/security/?sentry_key=<public key>&sentry_environment=<environment>&sentry_release=<commit>. The key is the DSN's public key, which the browser bundle already carries. |

NEARSHOP_CSP_MODE picks the header: report-only (the default) sends Content-Security-Policy-Report-Only;
enforce sends Content-Security-Policy. Any other value fails the build.

With the production values (media domain and DSN set), the enforced header reads, on one line:
default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self'
data: blob: https://<media domain> https://tiles.openfreemap.org; connect-src 'self'
https://tiles.openfreemap.org https://<ingest host>; worker-src 'self' blob:; font-src 'self'; object-src
'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; upgrade-insecure-requests; report-uri
<the Sentry security-report URL>; report-to csp

## API responses

Every response of the API, on Render directly and through Vercel at /api and /media: every route, errors
(404, 422, the 403 and 429 of the edge check and rate limits) and the 500 of an unhandled exception.

| Header | Value | When | Why |
|---|---|---|---|
| X-Content-Type-Options | nosniff | Always | A JSON body or a photo is never taken for HTML or script. |
| Content-Security-Policy | default-src 'none'; frame-ancestors 'none' | Always | An API response is never a page: opened as one, it may load nothing, run nothing and be framed by nobody. Development only: FastAPI's /docs and /redoc pages go without it, since it would block their scripts. |
| Referrer-Policy | no-referrer | Always | Nothing the API returns links anywhere with a referrer. |
| Cache-Control | private, no-store | Every response to a request carrying a session (the session cookie, or a bearer token, valid or not), and every response under /api/auth, /api/me, /api/reservations, /api/orders, /api/reviews, /api/notifications, /api/owner, /api/admin and /api/ai, signed in or not. It replaces any Cache-Control a route set. | Signed-in JSON carries names, phone numbers and addresses: neither the browser nor Vercel nor any proxy may keep it. |

Not no-store: /media files, even for a signed-in visitor. They are the same public photo for everyone, nothing
personal is served there, and re-downloading every photo on every page would cost slow-4G users. Public
routes requested without a session carry no Cache-Control from this layer (P2.8 adds caching for public pages).

/api/auth/me, fetched through Vercel by a signed-in browser, therefore carries exactly: X-Content-Type-Options
nosniff, Content-Security-Policy default-src 'none'; frame-ancestors 'none', Referrer-Policy no-referrer and
Cache-Control private, no-store (plus the API's X-Request-Id and Server-Timing, from P2.1).

## Cookies

The session cookie stays HttpOnly and SameSite=Lax. Secure is NEARSHOP_COOKIE_SECURE: the API refuses to start
with it false when NEARSHOP_ENVIRONMENT is staging, production or demo (backend/app/main.py, the
lifespan check), so a deploy without it fails and the previous version keeps serving. render.yaml sets it
true on both API services.

## Decisions

**No nonces (v1).** The Next.js 16 guide (frontend/node_modules/next/dist/docs/01-app/02-guides/
content-security-policy.md) says a nonce makes every page render dynamically, which turns off static output,
ISR and CDN caching. On slow 4G, cached HTML matters more than blocking inline scripts. React escapes what it
renders; the only raw-HTML insertions are the chart style block and, from P2.8, escaped JSON-LD. Violations
reach Sentry and alert (docs/runbooks/monitoring.md, alert #15). Revisit when Next.js marks hash-based CSP
(Subresource Integrity, experimental.sri) stable, or if raw HTML from users or third parties is ever
rendered. Plan section 12.

**HSTS stays with Vercel.** Vercel sends Strict-Transport-Security with a two-year max-age,
includeSubDomains and preload on every response (checked on the live site, 18 Oct 2026). Setting it in
next.config.ts as well would send it twice. If the site moves off Vercel or onto a custom domain, check
the header there and add it to next.config.ts if it is missing.

**API and site headers do not mix.** The site's rule matches every path except /api and /media, so
responses passed through from the API carry only the API's set, never two conflicting Referrer-Policy or
Content-Security-Policy headers.

## Report-only, then enforcing

| Step | Date | Done by | Notes |
|---|---|---|---|
| Report-only on staging | not yet | | Merge of P2.6. |
| Report-only on production (starts the 7 days) | not yet; by 4 Dec 2026 | | Release of P2.6. |
| 7 days: 0 violations from the site's own pages | not yet | | List any reports from browser extensions here and ignore them. |
| Enforcing on staging, scripted check passed | not yet | | Follow-up pull request under P2.6. |
| Enforcing on production | not yet; before G1 | | This is the date the policy moved from report-only to enforcing. |

The scripted check on staging, after enforcement, recorded in the pull request: the map loads its style and
tiles; an owner uploads a product photo and it shows; sign-in; a reservation is made and cancelled; a test
error reaches Sentry. During it, Sentry receives no security report from the site's own pages.

How to switch to enforcing, in the follow-up pull request under P2.6:

1. In Sentry > Issues, filter event.type:csp for the 7 days. Any report whose document is a NearShop page and
   whose blocked URI is not a browser extension (chrome-extension:, moz-extension:, safari-web-extension:)
   or an injected script from one is a real gap: fix the code or the policy first, and restart the 7 days.
   Add the extension sources to Sentry > Project Settings > Security Headers > Additional ignored sources.
2. Set NEARSHOP_CSP_MODE=enforce for the Preview environment in Vercel (staging), redeploy main, and run the
   scripted check above.
3. Change the default in frontend/next.config.ts (cspMode) from report-only to enforce, fill in the dates
   above, merge and release. Remove NEARSHOP_CSP_MODE from Vercel.
4. Switch on alert #15 in docs/runbooks/monitoring.md and test-fire it.

Rollback: set NEARSHOP_CSP_MODE=report-only for Production in Vercel and redeploy: the policy is reported,
not enforced, at once.
