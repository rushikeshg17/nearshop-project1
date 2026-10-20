import { type APIRequestContext, expect, type Locator, type Page, test as base } from "@playwright/test";

/**
 * Keyboard-only reserve (P2.10, plan section 8.15, docs/accessibility.md).
 *
 * Signed in as the seeded demo customer, with the keyboard only: search from the home page, open the product,
 * reserve it and see the pickup code. The mouse is never used. At every step the focused element must
 *   - match :focus-visible,
 *   - look different from the same element unfocused (the pixels in and around it change when it loses focus), and
 *   - not be entirely hidden by sticky or fixed content, such as the header or the bottom bars (WCAG 2.4.11).
 *
 * Runs against the seeded throwaway database of the frontend-quality CI job, with NEARSHOP_DEMO_PASSWORD.
 */

type Options = { language: string };
const test = base.extend<Options>({ language: ["en", { option: true }] });

const CUSTOMER = process.env.NEARSHOP_E2E_CUSTOMER_EMAIL || "priya@nearshop.demo";
const OWNER = process.env.NEARSHOP_E2E_OWNER_EMAIL || "owner@nearshop.demo";
const CLIENT_HEADER = { "X-NearShop-Client": "web" };
const MAX_ACTIVE_RESERVATIONS = 5; // backend/app/services/reservations.py, MAX_ACTIVE_PER_CUSTOMER
const MAX_TABS = 60;

type Listing = { id: number; name: string; quantity: number; is_active: boolean };
type SearchResult = { groups: { best_offer: { id: number } }[] };
type Reservation = { id: number; code: string; status: string; created_at: string; product: { id: number } };

async function signIn(request: APIRequestContext, email: string) {
  const password = process.env.NEARSHOP_DEMO_PASSWORD;
  if (!password) throw new Error("Set NEARSHOP_DEMO_PASSWORD to the seeded demo accounts' password.");
  const r = await request.post("/api/auth/login", { data: { email, password }, headers: CLIENT_HEADER });
  expect(r.ok(), `sign in as ${email}: HTTP ${r.status()}`).toBeTruthy();
}

async function getJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const r = await request.get(path);
  expect(r.ok(), `GET ${path}: HTTP ${r.status()}`).toBeTruthy();
  return (await r.json()) as T;
}

async function post(request: APIRequestContext, path: string, body: unknown = {}) {
  const r = await request.post(path, { data: body, headers: CLIENT_HEADER });
  expect(r.ok(), `POST ${path}: HTTP ${r.status()}`).toBeTruthy();
}

/**
 * A search query whose first result is a listing at the demo owner's shop that the customer has not reserved, so
 * the owner can confirm the reservation and the page then shows the pickup code. The seeded database is shared
 * with the other specs (and with earlier local runs), so the customer may already hold reservations: the oldest
 * are cancelled if the customer is near the limit of open reservations.
 */
async function pickQuery(customer: APIRequestContext, owner: APIRequestContext): Promise<{ query: string; product: number }> {
  const active = await getJson<Reservation[]>(customer, "/api/reservations?scope=active");
  const oldestFirst = [...active].sort((a, b) => a.created_at.localeCompare(b.created_at));
  for (const r of oldestFirst.slice(0, Math.max(0, active.length - (MAX_ACTIVE_RESERVATIONS - 2)))) {
    await post(customer, `/api/reservations/${r.id}/cancel`, { reason: "keyboard test" });
  }
  const reserved = new Set(active.map((r) => r.product.id));
  const listings = (await getJson<{ items: Listing[] }>(owner, "/api/owner/products?page_size=50")).items;
  for (const listing of listings) {
    if (!listing.is_active || listing.quantity < 1 || reserved.has(listing.id)) continue;
    const found = await getJson<SearchResult>(customer, `/api/search?q=${encodeURIComponent(listing.name)}&page_size=1`);
    if (found.groups[0]?.best_offer.id === listing.id) return { query: listing.name, product: listing.id };
  }
  throw new Error("No listing at the demo owner's shop comes first in search: nothing to reserve.");
}

/** Checks the focused element: :focus-visible, a visible indicator, and not hidden behind other content. */
async function expectVisibleFocus(page: Page, step: string) {
  const focused = page.locator(":focus");
  await expect(focused, `${step}: something has focus`).toHaveCount(1);
  const measure = () =>
    focused.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const inset = (n: number, size: number) => Math.min(n, size / 2);
      const xs = [r.left + inset(2, r.width), r.left + r.width / 2, r.right - inset(2, r.width)];
      const ys = [r.top + inset(2, r.height), r.top + r.height / 2, r.bottom - inset(2, r.height)];
      let uncovered = false;
      for (const x of xs)
        for (const y of ys) {
          if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
          const hit = document.elementFromPoint(x, y);
          if (hit && (hit === el || el.contains(hit) || hit.contains(el))) uncovered = true;
        }
      return {
        focusVisible: el.matches(":focus-visible"),
        uncovered,
        box: { x: r.left, y: r.top, width: r.width, height: r.height },
        name: (el.getAttribute("aria-label") || el.textContent || el.tagName).trim().slice(0, 60),
      };
    });
  // Let the page settle first: a list that closes when focus leaves it (the search suggestions) may still cover
  // the element for a moment, and a dialog that has just opened may still be sliding into place.
  let state = await measure();
  let previous = "";
  await expect
    .poll(
      async () => {
        state = await measure();
        const box = JSON.stringify(state.box);
        const settled = box === previous;
        previous = box;
        return settled && state.uncovered;
      },
      { message: `${step}: "${state.name}" is not hidden behind other content`, timeout: 3_000, intervals: [100] },
    )
    .toBe(true);
  expect(state.focusVisible, `${step}: "${state.name}" matches :focus-visible`).toBe(true);

  // The indicator: the element's surroundings (with room for an outline, a ring, or a ring on the field around an
  // input) must look different when the element loses focus. The test harness removes focus with blur() and gives
  // it back with focus(); the user's own input stays keyboard-only. Transient effects (a list closing) settle first.
  const pad = 16;
  const viewport = page.viewportSize()!;
  const x = Math.max(0, state.box.x - pad);
  const y = Math.max(0, state.box.y - pad);
  const clip = {
    x,
    y,
    width: Math.max(1, Math.min(viewport.width, state.box.x + state.box.width + pad) - x),
    height: Math.max(1, Math.min(viewport.height, state.box.y + state.box.height + pad) - y),
  };
  const shot = () => page.screenshot({ clip, animations: "disabled", caret: "hide" });
  const handle = await focused.elementHandle();
  if (!handle) throw new Error(`${step}: the focused element went away`);
  await page.waitForTimeout(250);
  const focusedShot = await shot();
  await handle.evaluate((el) => (el as HTMLElement).blur());
  await page.waitForTimeout(250);
  const blurredShot = await shot();
  await handle.evaluate((el) => (el as HTMLElement).focus());
  expect(
    await handle.evaluate((el) => el === document.activeElement && el.matches(":focus-visible")),
    `${step}: focus returns to "${state.name}"`,
  ).toBe(true);
  await handle.dispose();
  expect(focusedShot.equals(blurredShot), `${step}: "${state.name}" shows a visible focus indicator`).toBe(false);
}

/** Presses Tab until `target` has focus, checking the focus indicator at every stop on the way. */
async function tabTo(page: Page, target: Locator, step: string) {
  for (let i = 1; i <= MAX_TABS; i++) {
    await page.keyboard.press("Tab");
    await expectVisibleFocus(page, `${step}, Tab ${i}`);
    if (await target.evaluate((el) => el === document.activeElement).catch(() => false)) return;
  }
  throw new Error(`${step}: the target was not reached in ${MAX_TABS} presses of Tab`);
}

test("search, open a product, reserve it and see the code, with the keyboard only", async ({ page, playwright }, testInfo) => {
  const owner = await playwright.request.newContext({ baseURL: testInfo.project.use.baseURL });
  await signIn(owner, OWNER);
  await signIn(page.request, CUSTOMER);
  const { query, product } = await pickQuery(page.request, owner);

  // 1. Home page: Tab to the search box, type and press Enter.
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  const box = page.getByRole("combobox", { name: "Search products" }).first();
  await tabTo(page, box, "home: to the search box");
  await page.keyboard.type(query);
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/search\?q=/);

  // 2. Results: Tab to the first result and open it.
  const first = page.locator(`main a[href="/product/${product}"]`).first();
  await expect(first).toBeVisible();
  await tabTo(page, first, "search: to the first result");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`/product/${product}$`));

  // 3. Product: Tab to "Reserve for pickup" and press Enter. The dialog opens with focus inside it.
  const reserve = page.getByRole("button", { name: "Reserve for pickup" });
  await expect(reserve).toBeEnabled();
  await tabTo(page, reserve, "product: to Reserve for pickup");
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Reserve for pickup" });
  await expect(dialog).toBeVisible();
  await expect
    .poll(() => dialog.evaluate((d) => d.contains(document.activeElement)), { message: "focus moves into the dialog" })
    .toBe(true);

  // 4. Dialog: Tab to the reserve button (focus stays inside the dialog) and press Enter.
  const confirm = dialog.getByRole("button", { name: /^Reserve (it|\d+ units)/ });
  await tabTo(page, confirm, "reserve dialog: to the reserve button");
  await page.keyboard.press("Enter");

  // 5. The reservation page. Once the shop confirms (the owner, through the API), it shows the pickup code.
  await expect(page).toHaveURL(/\/account\/reservations\/\d+$/, { timeout: 15_000 });
  const id = Number(page.url().split("/").pop());
  const reservation = await getJson<Reservation>(page.request, `/api/reservations/${id}`);
  expect(reservation.product.id).toBe(product);
  await post(owner, `/api/owner/reservations/${id}/confirm`);
  await owner.dispose();
  const code = page.getByText(reservation.code.replace("R-", ""), { exact: true });
  await expect(code).toBeVisible({ timeout: 20_000 }); // the page checks for news every 8 seconds
  await expect(code).toBeInViewport();
});
