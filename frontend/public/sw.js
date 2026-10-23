/* NearShop service worker (P7.12): the installable app and offline reservation codes.
 *
 * Hand-written, as the Next.js progressive web apps guide does it; no build plugin. Registered by
 * src/components/site/sw-register.tsx as /sw.js?v=<build id>, so every deploy is a new worker script: the browser
 * installs it on the first launch after the deploy and, since it skips waiting and claims the open pages, the app
 * runs the new version by the second launch at the latest. next.config.ts serves this file uncached, with a strict
 * policy of its own.
 *
 * What it keeps (Cache Storage, all named nearshop-*):
 * - nearshop-shell-<build>: the app shell of this build, precached on install (the home page's scripts and styles,
 *   the fonts those styles name, and the install icons), plus every hashed /_next/static file a page loads later.
 * - nearshop-pages-<build>: public pages the visitor opened, and the customer's own reservation pages
 *   (/account/reservations/<id>), for the network-first navigations below. Never an owner (/shop) or admin (/admin)
 *   page, and no other /account page.
 * - nearshop-reservations: the signed-in customer's open reservations (REQUESTED, CONFIRMED, READY_FOR_PICKUP), one
 *   entry per reservation under its API path. The reservation page (reservation-view.tsx) writes an entry when it
 *   loads an open reservation and removes it when the reservation closes; sign-out (src/hooks/use-session.ts) has
 *   this worker delete them all. This is the only API data kept: the worker never caches an API response itself,
 *   and /api requests go to the network untouched.
 * - nearshop-meta: the offline screen's text in the interface language, sent by sw-register.tsx.
 * When a new version activates, the shell and page caches of every other version are deleted.
 */

const VERSION = new URL(self.location.href).searchParams.get("v") || "dev";
const SHELL = `nearshop-shell-${VERSION}`;
const PAGES = `nearshop-pages-${VERSION}`;
const RESERVATIONS = "nearshop-reservations";
const META = "nearshop-meta";
const KEEP = new Set([SHELL, PAGES, RESERVATIONS, META]);

const NAVIGATION_TIMEOUT_MS = 4000;
const MAX_PAGES = 30;
const ICONS = ["/icons/icon-192.png", "/icons/icon-512.png", "/icons/icon-maskable-512.png"];
const STRINGS_KEY = "/__nearshop/offline-strings";

// The offline screen's text until sw-register.tsx has sent the interface language's (src/messages, shell.offline).
const DEFAULT_STRINGS = {
  lang: "en-IN",
  title: "You are offline",
  body: "This page has not been saved on this phone. Check your connection and try again.",
  retry: "Try again",
  home: "Home",
  savedCodeTitle: "Your saved pickup code",
  showAtCounter: "Show this at the counter",
  savedAt: "Saved at {time}",
  mayBeOutdated: "Saved while you were online, so it may be out of date. The shop has the current status.",
};

// ------------------------------------------------------------------------------------------- install and activate

self.addEventListener("install", (event) => {
  event.waitUntil(precache().then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((n) => n.startsWith("nearshop-") && !KEEP.has(n)).map((n) => caches.delete(n)));
      await self.clients.claim();
    })(),
  );
});

/** Same-origin hashed build files named in a page or a style sheet. */
function staticUrls(text, base) {
  const found = new Set();
  for (const match of text.matchAll(/\/_next\/static\/[^"'\s\\)<>]+/g)) found.add(match[0]);
  for (const match of text.matchAll(/url\(\s*['"]?([^'")]+)['"]?\s*\)/g)) {
    try {
      const url = new URL(match[1], base);
      if (url.origin === self.location.origin && url.pathname.startsWith("/_next/static/")) found.add(url.pathname + url.search);
    } catch {
      // not a URL
    }
  }
  return found;
}

async function precache() {
  const [shell, pages] = await Promise.all([caches.open(SHELL), caches.open(PAGES)]);
  // The home page is the shell: its scripts and styles are the ones every page shares. Without it the worker
  // does not install, and the browser tries again on the next visit.
  const home = await fetch("/", { cache: "no-store" });
  if (!home.ok || home.redirected) throw new Error(`The home page answered ${home.status}`);
  const html = await home.clone().text();
  await pages.put(new Request(new URL("/", self.location.origin).href), home);
  const assets = staticUrls(html, self.location.origin);
  // The fonts are named only in the style sheets.
  await Promise.all(
    [...assets]
      .filter((u) => u.split("?")[0].endsWith(".css"))
      .map(async (u) => {
        try {
          const r = await fetch(u);
          if (!r.ok) return;
          await shell.put(u, r.clone());
          for (const font of staticUrls(await r.text(), new URL(u, self.location.origin).href)) assets.add(font);
        } catch {
          // fetched again when a page needs it
        }
      }),
  );
  await Promise.all(
    [...assets, ...ICONS].map(async (u) => {
      try {
        if (await shell.match(u)) return;
        const r = await fetch(u);
        if (r.ok) await shell.put(u, r);
      } catch {
        // fetched again when a page needs it
      }
    }),
  );
}

// ------------------------------------------------------------------------------------------------------ requests

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // The API and photos always go to the network, untouched: no API response is cached here.
  if (url.pathname === "/api" || url.pathname.startsWith("/api/") || url.pathname.startsWith("/media/")) return;
  if (request.mode === "navigate") {
    event.respondWith(navigate(event, url));
    return;
  }
  if (url.pathname.startsWith("/_next/static/") || ICONS.includes(url.pathname)) {
    event.respondWith(cacheFirst(request));
  }
});

/** Hashed build files and icons never change under the same URL: the shell cache first, else the network. */
async function cacheFirst(request) {
  const shell = await caches.open(SHELL);
  const cached = await shell.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok && response.type === "basic") await shell.put(request, response.clone());
  return response;
}

const NEVER_CACHED = /^\/(shop|admin)(\/|$)/;
const RESERVATION_PAGE = /^\/account\/reservations\/(\d+)\/?$/;
const PRIVATE = /^\/account(\/|$)/;

/** "never": owner, admin and private pages; "reservation": a customer's reservation page; "public": the rest. */
function pagePolicy(pathname) {
  if (NEVER_CACHED.test(pathname)) return "never";
  if (RESERVATION_PAGE.test(pathname)) return "reservation";
  if (PRIVATE.test(pathname)) return "never";
  return "public";
}

function reservationId(pathname) {
  const match = RESERVATION_PAGE.exec(pathname);
  return match ? match[1] : null;
}

function cacheable(response) {
  return (
    response.ok &&
    response.type === "basic" &&
    !response.redirected &&
    (response.headers.get("Content-Type") || "").includes("text/html")
  );
}

/** Page navigations: the network first; after 4 seconds, or when the network fails, the saved page or the offline
 * screen. A slow network with nothing saved is still waited for. */
async function navigate(event, url) {
  const policy = pagePolicy(url.pathname);
  const network = fetch(event.request);
  if (policy === "never") {
    try {
      return await network;
    } catch {
      return offlineScreen(url, null);
    }
  }
  const saved = network.then(async (response) => {
    if (cacheable(response)) {
      const pages = await caches.open(PAGES);
      await pages.put(url.href, response.clone());
      await trimPages(pages);
    }
    return response;
  });
  event.waitUntil(saved.catch(() => undefined));
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve(null), NAVIGATION_TIMEOUT_MS);
  });
  try {
    const first = await Promise.race([saved, timeout]);
    if (first) return first;
    const fallback = await savedPage(url, policy);
    return fallback || (await saved);
  } catch {
    return (await savedPage(url, policy)) || offlineScreen(url, await storedReservation(reservationId(url.pathname)));
  } finally {
    clearTimeout(timer);
  }
}

/** The saved copy of a page. A reservation page is used only while its reservation is still stored, so a closed
 * reservation, or one signed out of, is never shown from a saved page. */
async function savedPage(url, policy) {
  const id = policy === "reservation" ? reservationId(url.pathname) : null;
  if (policy === "reservation" && !(await storedReservation(id))) return null;
  const pages = await caches.open(PAGES);
  return (await pages.match(url.href)) || (await pages.match(url.href, { ignoreSearch: true })) || null;
}

async function trimPages(pages) {
  const keys = await pages.keys();
  // Oldest first; the home page from the precache stays.
  const removable = keys.filter((k) => new URL(k.url).pathname !== "/");
  for (const key of removable.slice(0, Math.max(0, removable.length - MAX_PAGES))) await pages.delete(key);
}

async function storedReservation(id) {
  if (!id) return null;
  try {
    const cache = await caches.open(RESERVATIONS);
    const entry = await cache.match(`/api/reservations/${id}`);
    return entry ? await entry.json() : null;
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------------------------------------ offline screen

async function offlineStrings() {
  try {
    const entry = await (await caches.open(META)).match(STRINGS_KEY);
    if (entry) return { ...DEFAULT_STRINGS, ...(await entry.json()) };
  } catch {
    // the defaults
  }
  return DEFAULT_STRINGS;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function formatTime(iso, lang) {
  try {
    return new Intl.DateTimeFormat(lang, { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
  } catch {
    return iso;
  }
}

/** A page of its own, with no script: the offline notice and, on a stored reservation's page, its code. */
async function offlineScreen(url, reservation) {
  const s = await offlineStrings();
  const fill = (text, values) => text.replace(/\{(\w+)\}/g, (whole, name) => (name in values ? values[name] : whole));
  const code = reservation
    ? `<section class="code" aria-labelledby="code-label">
        <p id="code-label" class="label">${escapeHtml(s.showAtCounter)}</p>
        <p class="digits">${escapeHtml(String(reservation.code || "").replace(/^R-/, ""))}</p>
        <p>${escapeHtml(reservation.product?.name)} &times; ${escapeHtml(reservation.quantity)}</p>
      </section>
      <section class="shop">
        <p class="name">${escapeHtml(reservation.shop?.name)}</p>
        <p>${escapeHtml(reservation.shop?.address_line)}</p>
        <p>${escapeHtml(reservation.shop?.hours_label)}</p>
      </section>
      <p class="saved" data-saved-at="${escapeHtml(reservation.saved_at)}">${escapeHtml(fill(s.savedAt, { time: formatTime(reservation.saved_at, s.lang) }))}</p>
      <p class="muted">${escapeHtml(s.mayBeOutdated)}</p>`
    : `<p class="muted">${escapeHtml(s.body)}</p>`;
  const html = `<!doctype html>
<html lang="${escapeHtml(s.lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#fbfaf8">
<title>${escapeHtml(reservation ? s.savedCodeTitle : s.title)} · NearShop</title>
<style>
  :root { color-scheme: light dark; --bg: #fbfaf8; --fg: #1c1a17; --muted: #6b665e; --card: #ffffff; --line: #d9d4cb; }
  @media (prefers-color-scheme: dark) { :root { --bg: #141210; --fg: #f3f0ea; --muted: #a9a297; --card: #1e1b18; --line: #3a352f; } }
  body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, sans-serif; }
  main { max-width: 32rem; margin: 0 auto; padding: 2rem 1rem; }
  h1 { font-size: 1.5rem; margin: 0 0 .5rem; }
  p { margin: .25rem 0; }
  .muted { color: var(--muted); font-size: .875rem; }
  .code { margin: 1.5rem 0 1rem; padding: 1.25rem; text-align: center; background: var(--card); border: 2px dashed var(--line); border-radius: 1rem; }
  .label { font-weight: 600; font-size: .875rem; }
  .digits { font: 700 3rem/1.2 ui-monospace, monospace; letter-spacing: .25em; margin: .5rem 0; }
  .shop { margin: 1rem 0; }
  .name { font-weight: 600; }
  .saved { margin-top: 1rem; font-weight: 500; }
  nav { display: flex; gap: 1rem; margin-top: 1.5rem; }
  a { color: inherit; font-weight: 600; }
</style>
</head>
<body>
<main>
  <h1>${escapeHtml(reservation ? s.savedCodeTitle : s.title)}</h1>
  ${code}
  <nav><a href="${escapeHtml(url.pathname + url.search)}">${escapeHtml(s.retry)}</a><a href="/">${escapeHtml(s.home)}</a></nav>
</main>
</body>
</html>`;
  return new Response(html, {
    status: 200,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    },
  });
}

// ------------------------------------------------------------------------------------------------------ messages

self.addEventListener("message", (event) => {
  const data = event.data || {};
  const reply = (body) => event.ports?.[0]?.postMessage(body);
  if (data.type === "offline-strings" && data.strings && typeof data.strings === "object") {
    const strings = { lang: typeof data.lang === "string" ? data.lang : DEFAULT_STRINGS.lang };
    for (const key of Object.keys(DEFAULT_STRINGS)) {
      if (key !== "lang" && typeof data.strings[key] === "string" && data.strings[key]) strings[key] = data.strings[key];
    }
    event.waitUntil(
      caches
        .open(META)
        .then((cache) => cache.put(STRINGS_KEY, new Response(JSON.stringify(strings), { headers: { "Content-Type": "application/json" } })))
        .then(() => reply({ ok: true })),
    );
    return;
  }
  if (data.type === "sign-out") {
    // Every stored reservation, and every saved /account page in any version's page cache.
    event.waitUntil(
      (async () => {
        await caches.delete(RESERVATIONS);
        for (const name of await caches.keys()) {
          if (!name.startsWith("nearshop-pages-")) continue;
          const pages = await caches.open(name);
          for (const key of await pages.keys()) {
            if (PRIVATE.test(new URL(key.url).pathname)) await pages.delete(key);
          }
        }
        reply({ ok: true });
      })(),
    );
  }
});
