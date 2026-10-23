import fs from "node:fs";
import path from "node:path";

import { type APIRequestContext, expect, type Page, test as base } from "@playwright/test";

/**
 * The installable app and offline reservation codes (P7.12; public/sw.js, src/components/site/sw-register.tsx).
 *
 * As the seeded customer, with a reservation the demo owner has confirmed:
 * 1. Viewed once while online, the reservation still shows its code, the shop and the time it was saved after the
 *    browser goes offline and the page is reloaded: from the worker's offline screen when the page itself was not
 *    saved yet, and in the reservation page once it has been.
 * 2. The caches hold no API response except the customer's open reservations.
 * 3. After sign-out, neither the caches nor the page's storage hold any reservation.
 * 4. The other ways out of the account do the same: a session revoked from another device forgets the stored copy
 *    when the page next loads online, and shows no code, and "Sign out everywhere" clears the caches like sign-out.
 *
 * Runs against the production build (the worker is registered only there) in front of the API on the seeded
 * throwaway database of the frontend-quality CI job, with NEARSHOP_DEMO_PASSWORD, like review-codes.spec.ts.
 */

type Options = { language: string };
const test = base.extend<Options>({ language: ["en", { option: true }] });

const CUSTOMER = process.env.NEARSHOP_E2E_CUSTOMER_EMAIL || "priya@nearshop.demo";
const OWNER = process.env.NEARSHOP_E2E_OWNER_EMAIL || "owner@nearshop.demo";
const CLIENT_HEADER = { "X-NearShop-Client": "web" };
const MAX_ACTIVE_RESERVATIONS = 5; // backend/app/services/reservations.py, MAX_ACTIVE_PER_CUSTOMER
const OPEN = ["REQUESTED", "CONFIRMED", "READY_FOR_PICKUP"];

type Listing = { id: number; quantity: number; is_active: boolean };
type Reservation = { id: number; code: string; status: string; created_at: string; product: { id: number }; shop: { name: string } };

function demoPassword() {
  const password = process.env.NEARSHOP_DEMO_PASSWORD;
  if (!password) throw new Error("Set NEARSHOP_DEMO_PASSWORD to the seeded demo accounts' password.");
  return password;
}

/** The interface text for a message key in the project's language, falling back to English per key, as the site does. */
function message(language: string, key: string) {
  const lookup = (code: string) => {
    let node: unknown = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "src", "messages", `${code}.json`), "utf8"));
    for (const part of key.split(".")) node = (node as Record<string, unknown> | undefined)?.[part];
    return typeof node === "string" && node ? node : null;
  };
  const text = lookup(language) ?? lookup("en");
  if (text === null) throw new Error(`No message ${key}`);
  return text;
}

/** The text of "Saved at {time}" before the time, which the test cannot predict to the minute. */
function savedAtPrefix(language: string) {
  return message(language, "shell.offline.savedAt").split("{time}")[0].trim();
}

async function signIn(request: APIRequestContext, email: string) {
  const r = await request.post("/api/auth/login", { data: { email, password: demoPassword() }, headers: CLIENT_HEADER });
  expect(r.ok(), `sign in as ${email}: HTTP ${r.status()}`).toBeTruthy();
}

async function getJson<T>(request: APIRequestContext, url: string): Promise<T> {
  const r = await request.get(url);
  expect(r.ok(), `GET ${url}: HTTP ${r.status()}`).toBeTruthy();
  return (await r.json()) as T;
}

async function post<T = unknown>(request: APIRequestContext, url: string, body: unknown = {}): Promise<T> {
  const r = await request.post(url, { data: body, headers: CLIENT_HEADER });
  expect(r.ok(), `POST ${url}: HTTP ${r.status()} ${await r.text()}`).toBeTruthy();
  return (await r.json()) as T;
}

/** A reservation at the demo owner's shop that the owner has confirmed, so its code is on the customer's page. */
async function confirmedReservation(customer: APIRequestContext, owner: APIRequestContext): Promise<Reservation> {
  const active = await getJson<Reservation[]>(customer, "/api/reservations?scope=active");
  const oldestFirst = [...active].sort((a, b) => a.created_at.localeCompare(b.created_at));
  for (const r of oldestFirst.slice(0, Math.max(0, active.length - (MAX_ACTIVE_RESERVATIONS - 2)))) {
    await post(customer, `/api/reservations/${r.id}/cancel`, { reason: "offline-reservation test" });
  }
  const reserved = new Set(active.map((r) => r.product.id));
  const listings = (await getJson<{ items: Listing[] }>(owner, "/api/owner/products?page_size=50")).items;
  const listing = listings.find((l) => l.is_active && l.quantity >= 1 && !reserved.has(l.id));
  if (!listing) throw new Error("The demo owner's shop has no listing in stock to reserve.");
  const created = await post<Reservation>(customer, "/api/reservations", { product_id: listing.id, quantity: 1 });
  await post(owner, `/api/owner/reservations/${created.id}/confirm`);
  return getJson<Reservation>(customer, `/api/reservations/${created.id}`);
}

/** Every Cache Storage entry the page's origin holds, by cache, with each reservation entry's stored status. */
function cacheContents(page: Page) {
  return page.evaluate(async () => {
    const result: { cache: string; path: string; status?: string }[] = [];
    for (const name of await caches.keys()) {
      const cache = await caches.open(name);
      for (const request of await cache.keys()) {
        const entry: { cache: string; path: string; status?: string } = { cache: name, path: new URL(request.url).pathname };
        if (entry.path.startsWith("/api/")) entry.status = ((await (await cache.match(request))?.json()) as { status?: string })?.status;
        result.push(entry);
      }
    }
    return result;
  });
}

/** Wait until the worker runs this page and the reservation is stored for offline use. */
async function waitForOfflineCopy(page: Page, id: number) {
  await page.waitForFunction(() => !!navigator.serviceWorker?.controller, undefined, { timeout: 20_000 });
  await expect
    .poll(() => page.evaluate(async (key) => !!(await caches.match(key)), `/api/reservations/${id}`), { timeout: 10_000 })
    .toBe(true);
}

test("an open reservation viewed once online shows its code offline; sign-out leaves none stored", async ({ page, browser, context, language }, testInfo) => {
  const owner = await browser.newContext({ baseURL: testInfo.project.use.baseURL });
  await signIn(owner.request, OWNER);
  await signIn(page.request, CUSTOMER);
  const reservation = await confirmedReservation(page.request, owner.request);
  const code = reservation.code.replace(/^R-/, "");
  const url = `/account/reservations/${reservation.id}`;

  // 1. Viewed once while online.
  await page.goto(url);
  await expect(page.getByText(code, { exact: true })).toBeVisible();
  await waitForOfflineCopy(page, reservation.id);

  // Offline, the page itself was loaded before the worker ran it: the worker's own offline screen shows the code.
  await context.setOffline(true);
  await page.reload();
  await expect(page.getByText(code, { exact: true })).toBeVisible();
  await expect(page.getByText(reservation.shop.name)).toBeVisible();
  await expect(page.getByText(savedAtPrefix(language), { exact: false })).toBeVisible();

  // Online again, the worker saves the page; offline, the reservation page itself shows the saved copy.
  await context.setOffline(false);
  await page.reload();
  await expect(page.getByText(code, { exact: true })).toBeVisible();
  await context.setOffline(true);
  const started = Date.now();
  await page.reload();
  await expect(page.getByTestId("offline-code")).toHaveText(code);
  testInfo.annotations.push({ type: "offline code shown after (ms)", description: String(Date.now() - started) });
  await expect(page.getByText(reservation.shop.name)).toBeVisible();
  await expect(page.getByTestId("offline-saved-at")).toContainText(savedAtPrefix(language));
  await expect(page.getByText(message(language, "shell.offline.banner"))).toBeVisible();

  // 2. No API response in any cache except the customer's open reservations.
  const contents = await cacheContents(page);
  const api = contents.filter((e) => e.path.startsWith("/api/"));
  expect(api.length).toBeGreaterThan(0);
  for (const entry of api) {
    expect(entry.cache, `${entry.path} is cached in ${entry.cache}`).toBe("nearshop-reservations");
    expect(entry.path).toMatch(/^\/api\/reservations\/\d+$/);
    expect(OPEN).toContain(entry.status);
  }
  // Never an owner or admin page.
  expect(contents.filter((e) => /^\/(shop|admin)(\/|$)/.test(e.path))).toEqual([]);

  // 3. Sign out (online, from the account menu).
  await context.setOffline(false);
  await page.goto("/account");
  await page.getByRole("button", { name: message(language, "shell.userMenu.trigger") }).click();
  await page.getByRole("menuitem", { name: message(language, "shell.userMenu.signOut") }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect.poll(async () => (await cacheContents(page)).filter((e) => e.path.startsWith("/api/") || e.path.startsWith("/account"))).toEqual([]);
  const webStorage = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
  expect(webStorage).not.toContain(code);
  expect(webStorage).not.toContain(reservation.code);

  // Offline after sign-out, the reservation's page shows no code.
  await context.setOffline(true);
  await page.goto(url).catch(() => undefined);
  await expect(page.getByText(code, { exact: true })).toHaveCount(0);
  await context.setOffline(false);

  await post(page.request, "/api/auth/login", { email: CUSTOMER, password: demoPassword() });
  await post(page.request, `/api/reservations/${reservation.id}/cancel`, { reason: "offline-reservation test" }).catch(() => undefined);
  await owner.close();
});

/** The cache entries a signed-out browser must not keep: stored reservations and saved /account pages. */
async function privateEntries(page: Page) {
  return (await cacheContents(page)).filter((e) => e.path.startsWith("/api/") || e.path.startsWith("/account"));
}

test("a session revoked elsewhere, and sign-out everywhere, leave no reservation stored", async ({ page, browser, language }, testInfo) => {
  const owner = await browser.newContext({ baseURL: testInfo.project.use.baseURL });
  await signIn(owner.request, OWNER);
  await signIn(page.request, CUSTOMER);
  const reservation = await confirmedReservation(page.request, owner.request);
  const code = reservation.code.replace(/^R-/, "");
  const url = `/account/reservations/${reservation.id}`;
  const stored = () => page.evaluate(async (key) => !!(await caches.match(key)), `/api/reservations/${reservation.id}`);

  // Revoked from another device (the lost-phone case): online, the page shows no code and forgets the stored copy.
  await page.goto(url);
  await expect(page.getByText(code, { exact: true })).toBeVisible();
  await waitForOfflineCopy(page, reservation.id);
  const elsewhere = await browser.newContext({ baseURL: testInfo.project.use.baseURL });
  await signIn(elsewhere.request, CUSTOMER);
  await post(elsewhere.request, "/api/auth/logout-everywhere");
  await elsewhere.close();
  await page.reload();
  await expect.poll(stored).toBe(false);
  await expect(page.getByText(code, { exact: true })).toHaveCount(0);

  // "Sign out everywhere" on the account page clears the caches as sign-out does.
  await signIn(page.request, CUSTOMER);
  await page.goto(url);
  await expect(page.getByText(code, { exact: true })).toBeVisible();
  await waitForOfflineCopy(page, reservation.id);
  await page.goto("/account");
  await page.getByRole("button", { name: message(language, "transactions.account.devices.everywhere") }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect.poll(() => privateEntries(page)).toEqual([]);

  await signIn(page.request, CUSTOMER);
  await post(page.request, `/api/reservations/${reservation.id}/cancel`, { reason: "offline-reservation test" }).catch(() => undefined);
  await owner.close();
});
