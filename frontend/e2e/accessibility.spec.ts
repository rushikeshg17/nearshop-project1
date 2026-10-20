import fs from "node:fs";

import AxeBuilder from "@axe-core/playwright";
import { type APIRequestContext, expect, type Page, test as base, type TestInfo } from "@playwright/test";

/**
 * Accessibility baseline (P2.10, plan section 8.15, docs/accessibility.md).
 *
 * Every customer and owner screen in the WCAG 2.2 AA target, at 360 by 800 px (playwright.config.ts), with each
 * screen's dialogs opened in turn. axe runs with the WCAG 2.0, 2.1 and 2.2 level A and AA rule tags, and any
 * serious or critical violation fails the test. Moderate and minor ones are attached to the report and logged.
 *
 * The site runs against a seeded throwaway database (the frontend-quality CI job). The spec signs in as the seeded
 * demo customer and owner with NEARSHOP_DEMO_PASSWORD, which the CI job generates for its run. Before the screens
 * it makes sure the customer has an open reservation and an open delivery order at the owner's shop, through the
 * site's own API, so the detail pages and the owner's queues have something to show.
 */

type Options = { language: string };
const test = base.extend<Options>({ language: ["en", { option: true }] });

const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22a", "wcag22aa"];
const BLOCKING = new Set(["serious", "critical"]);
const CUSTOMER = process.env.NEARSHOP_E2E_CUSTOMER_EMAIL || "priya@nearshop.demo";
const OWNER = process.env.NEARSHOP_E2E_OWNER_EMAIL || "owner@nearshop.demo";
const CLIENT_HEADER = { "X-NearShop-Client": "web" };
const MAX_ACTIVE_RESERVATIONS = 5; // backend/app/services/reservations.py, MAX_ACTIVE_PER_CUSTOMER

function demoPassword(): string {
  const password = process.env.NEARSHOP_DEMO_PASSWORD;
  if (!password) throw new Error("Set NEARSHOP_DEMO_PASSWORD to the seeded demo accounts' password.");
  return password;
}

// ------------------------------------------------------------------------------------------------ test data

type Listing = { id: number; name: string; quantity: number; is_active: boolean };
type Shop = { id: number; slug: string; lat: number; lng: number; offers_delivery: boolean; offers_pickup: boolean };
type Reservation = { id: number; code: string; status: string; created_at: string; product: { id: number }; shop: { id: number } };
type Order = { id: number; code: string; status: string; shop: { id: number } };

type Fixture = {
  shop: Shop;
  product: number; // a listing at the owner's shop that the customer has not reserved: reserve and delivery dialogs
  reservation: number; // the customer's open reservation at the owner's shop
  order: number; // the customer's open delivery order at the owner's shop
};

let data: Fixture | null = null;

async function apiSignIn(request: APIRequestContext, email: string) {
  const r = await request.post("/api/auth/login", { data: { email, password: demoPassword() }, headers: CLIENT_HEADER });
  expect(r.ok(), `sign in as ${email}: HTTP ${r.status()}`).toBeTruthy();
}

async function getJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const r = await request.get(path);
  expect(r.ok(), `GET ${path}: HTTP ${r.status()}`).toBeTruthy();
  return (await r.json()) as T;
}

async function postJson<T>(request: APIRequestContext, path: string, body: unknown): Promise<T> {
  const r = await request.post(path, { data: body, headers: CLIENT_HEADER });
  expect(r.ok(), `POST ${path}: HTTP ${r.status()} ${await r.text()}`).toBeTruthy();
  return (await r.json()) as T;
}

/** The owner's shop, an unreserved listing there, and the customer's open reservation and order at it. */
async function prepare(owner: APIRequestContext, customer: APIRequestContext): Promise<Fixture> {
  const shop = await getJson<Shop>(owner, "/api/owner/shop");
  const listings = (await getJson<{ items: Listing[] }>(owner, "/api/owner/products?page_size=50")).items.filter(
    (p) => p.is_active && p.quantity >= 3,
  );
  expect(listings.length, "the owner's shop needs listings in stock").toBeGreaterThan(3);
  const meta = await getJson<{ city: { center: { lat: number; lng: number } } }>(customer, "/api/meta");

  // The detail pages and the owner's queues need a reservation the shop has not answered yet (decline, cancel)
  // and an order it has not confirmed. Earlier runs on the same database may have used theirs up, so new ones are
  // made when needed; the oldest open reservations are cancelled first if the customer is at the limit.
  let reservations = await getJson<Reservation[]>(customer, "/api/reservations?scope=active");
  let reservation = reservations.find((r) => r.shop.id === shop.id && r.status === "REQUESTED");
  if (!reservation && reservations.length >= MAX_ACTIVE_RESERVATIONS) {
    const oldestFirst = [...reservations].sort((a, b) => a.created_at.localeCompare(b.created_at));
    for (const r of oldestFirst.slice(0, reservations.length - MAX_ACTIVE_RESERVATIONS + 2)) {
      await postJson(customer, `/api/reservations/${r.id}/cancel`, { reason: "accessibility test" });
    }
    reservations = await getJson<Reservation[]>(customer, "/api/reservations?scope=active");
  }
  const reserved = new Set(reservations.map((r) => r.product.id));
  if (!reservation) {
    const listing = listings.find((p) => !reserved.has(p.id))!;
    reservation = await postJson<Reservation>(customer, "/api/reservations", { product_id: listing.id, quantity: 1 });
    reserved.add(listing.id);
  }

  const orders = await getJson<Order[]>(customer, "/api/orders?scope=active");
  let order = orders.find((o) => o.shop.id === shop.id && o.status === "PENDING");
  if (!order) {
    const listing = listings.find((p) => !reserved.has(p.id))!;
    order = await postJson<Order>(customer, "/api/orders", {
      items: [{ product_id: listing.id, quantity: 1 }],
      lat: meta.city.center.lat,
      lng: meta.city.center.lng,
      address: "12, Test Lane, near the bus stand",
      phone: "9876500000",
      note: null,
    });
  }

  const product = listings.find((p) => !reserved.has(p.id))!;
  return { shop, product: product.id, reservation: reservation.id, order: order.id };
}

test.beforeAll(async ({ playwright }, testInfo) => {
  const baseURL = testInfo.project.use.baseURL;
  const owner = await playwright.request.newContext({ baseURL });
  const customer = await playwright.request.newContext({ baseURL });
  try {
    await apiSignIn(owner, OWNER);
    await apiSignIn(customer, CUSTOMER);
    data = await prepare(owner, customer);
  } finally {
    await owner.dispose();
    await customer.dispose();
  }
});

function fixture(): Fixture {
  if (!data) throw new Error("test data was not prepared");
  return data;
}

// ------------------------------------------------------------------------------------------------ helpers

async function signIn(page: Page, email: string) {
  await apiSignIn(page.request, email);
}

/** Waits for the page to finish loading data and for finite animations (dialogs opening) to end. */
async function settle(page: Page) {
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => undefined);
  await page.evaluate(async () => {
    const running = document.getAnimations().filter((a) => {
      const end = a.effect?.getComputedTiming().endTime;
      return a.playState === "running" && typeof end === "number" && Number.isFinite(end);
    });
    await Promise.race([
      Promise.all(running.map((a) => a.finished.catch(() => undefined))),
      new Promise((resolve) => setTimeout(resolve, 3_000)),
    ]);
  });
  // JavaScript-driven transitions (motion, the drawer) have no Web Animations entry: give them a moment.
  await page.waitForTimeout(400);
}

type Violation = Awaited<ReturnType<AxeBuilder["analyze"]>>["violations"][number];

function describeViolation(v: Violation) {
  const targets = v.nodes.slice(0, 4).map((n) => n.target.join(" ")).join(" | ");
  const more = v.nodes.length > 4 ? ` (+${v.nodes.length - 4} more)` : "";
  return `${v.id} [${v.impact}] ${v.help}: ${targets}${more}`;
}

/** Runs axe on the page as it is now. Serious or critical violations fail the test (soft, so every dialog runs). */
async function audit(page: Page, testInfo: TestInfo, screen: string, language: string) {
  await settle(page);
  await expect(page.locator("html"), "the page's language").toHaveAttribute("lang", new RegExp(`^${language}(-|$)`));
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  const blocking = results.violations.filter((v) => BLOCKING.has(v.impact ?? ""));
  const other = results.violations.filter((v) => !BLOCKING.has(v.impact ?? ""));
  await testInfo.attach(`axe ${screen}`, { body: JSON.stringify(results.violations, null, 2), contentType: "application/json" });
  const count = (list: Violation[]) => list.reduce((n, v) => n + v.nodes.length, 0);
  const summary = process.env.NEARSHOP_E2E_AXE_SUMMARY;
  if (summary) {
    const row = {
      language,
      screen,
      blocking: count(blocking),
      other: count(other),
      violations: results.violations.map((v) => ({
        rule: v.id,
        impact: v.impact,
        nodes: v.nodes.map((n) => ({ target: n.target.join(" "), html: n.html.slice(0, 160), why: n.failureSummary })),
      })),
    };
    fs.appendFileSync(summary, `${JSON.stringify(row)}\n`);
  }
  for (const v of other) console.log(`[${language}] ${screen}: ${describeViolation(v)}`);
  expect.soft(blocking.map(describeViolation), `${screen}: serious or critical axe violations`).toEqual([]);
  // Reflow (WCAG 1.4.10, which axe does not test): nothing makes the page scroll sideways at 360 px.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect.soft(overflow, `${screen}: the page scrolls sideways by ${overflow} px`).toBeLessThanOrEqual(0);
}

/** Opens a dialog with its trigger, audits it, then closes it with Escape. */
async function auditDialog(page: Page, testInfo: TestInfo, screen: string, language: string, open: () => Promise<void>) {
  await open();
  const dialog = page.getByRole("dialog").last();
  await expect(dialog).toBeVisible();
  await audit(page, testInfo, screen, language);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
}

// ------------------------------------------------------------------------------------------------ customers

test.describe("customer screens", () => {
  test("home", async ({ page, language }, testInfo) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await audit(page, testInfo, "home", language);
  });

  test("search, with the filters sheet", async ({ page, language }, testInfo) => {
    await page.goto("/search?q=umbrella");
    await expect(page.locator('a[href^="/product/"]').first()).toBeVisible();
    await audit(page, testInfo, "search", language);
    await auditDialog(page, testInfo, "search: filters", language, () =>
      page.getByRole("button", { name: /filters/i }).click(),
    );
  });

  test("product, signed out", async ({ page, language }, testInfo) => {
    await page.goto(`/product/${fixture().product}`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await audit(page, testInfo, "product", language);
  });

  test("shop", async ({ page, language }, testInfo) => {
    await page.goto(`/shops/${fixture().shop.slug}`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await audit(page, testInfo, "shop", language);
  });

  test("sign-in", async ({ page, language }, testInfo) => {
    await page.goto("/login");
    await expect(page.getByLabel("Email")).toBeVisible();
    await audit(page, testInfo, "sign-in", language);
  });

  test("register", async ({ page, language }, testInfo) => {
    await page.goto("/register");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await audit(page, testInfo, "register", language);
  });

  test.describe("signed in", () => {
    test.beforeEach(async ({ page }) => signIn(page, CUSTOMER));

    test("product, with the reserve and delivery dialogs", async ({ page, language }, testInfo) => {
      await page.goto(`/product/${fixture().product}`);
      const reserve = page.getByRole("button", { name: "Reserve for pickup" });
      await expect(reserve).toBeEnabled();
      // Wait for the browser's own fetch (distance, delivery quote): the delivery button depends on it.
      await expect(page.getByText(/Delivery .* away/)).toBeVisible();
      await audit(page, testInfo, "product (signed in)", language);
      await auditDialog(page, testInfo, "product: reserve dialog", language, () => reserve.click());
      await auditDialog(page, testInfo, "product: delivery dialog", language, () =>
        page.getByRole("button", { name: "Request delivery" }).click(),
      );
    });

    test("reservation detail, with the cancel dialog", async ({ page, language }, testInfo) => {
      await page.goto(`/account/reservations/${fixture().reservation}`);
      const cancel = page.getByRole("button", { name: "Cancel reservation" });
      await expect(cancel).toBeVisible();
      await audit(page, testInfo, "reservation detail", language);
      await auditDialog(page, testInfo, "reservation detail: cancel dialog", language, () => cancel.click());
    });

    test("order detail, with the cancel dialog", async ({ page, language }, testInfo) => {
      await page.goto(`/account/orders/${fixture().order}`);
      const cancel = page.getByRole("button", { name: "Cancel order" });
      await expect(cancel).toBeVisible();
      await audit(page, testInfo, "order detail", language);
      await auditDialog(page, testInfo, "order detail: cancel dialog", language, () => cancel.click());
    });

    test("account", async ({ page, language }, testInfo) => {
      await page.goto("/account");
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.locator('a[href^="/account/reservations/"]').first()).toBeVisible();
      await audit(page, testInfo, "account", language);
    });
  });
});

// ------------------------------------------------------------------------------------------------ owners

test.describe("owner screens", () => {
  test.beforeEach(async ({ page }) => signIn(page, OWNER));

  test("overview, with the menu", async ({ page, language }, testInfo) => {
    await page.goto("/shop");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await audit(page, testInfo, "owner overview", language);
    await auditDialog(page, testInfo, "owner overview: menu", language, () =>
      page.getByRole("button", { name: "Open menu" }).click(),
    );
  });

  test("reservation queue, with the decline dialog", async ({ page, language }, testInfo) => {
    await page.goto("/shop/reservations");
    const decline = page.getByRole("button", { name: "Decline" }).first();
    await expect(decline).toBeVisible();
    await audit(page, testInfo, "owner reservations", language);
    await auditDialog(page, testInfo, "owner reservations: decline dialog", language, () => decline.click());
  });

  test("order queue, with the decline dialog", async ({ page, language }, testInfo) => {
    await page.goto("/shop/orders");
    const decline = page.getByRole("button", { name: "Decline" }).first();
    await expect(decline).toBeVisible();
    await audit(page, testInfo, "owner orders", language);
    await auditDialog(page, testInfo, "owner orders: decline dialog", language, () => decline.click());
  });

  test("inventory, with the product dialogs", async ({ page, language }, testInfo) => {
    await page.goto("/shop/inventory");
    await expect(page.getByRole("button", { name: /^More actions for / }).first()).toBeVisible();
    await audit(page, testInfo, "owner inventory", language);
    await auditDialog(page, testInfo, "owner inventory: new product dialog", language, () =>
      page.getByRole("button", { name: "New product" }).click(),
    );
    await auditDialog(page, testInfo, "owner inventory: edit product dialog", language, async () => {
      await page.getByRole("button", { name: /^More actions for / }).first().click();
      await page.getByRole("menuitem", { name: "Edit details" }).click();
    });
  });

  test("import, before and after choosing a file", async ({ page, language }, testInfo) => {
    await page.goto("/shop/import");
    await expect(page.getByRole("button", { name: "Choose file" })).toBeVisible();
    await audit(page, testInfo, "owner import", language);
    // The preview of a small stock file: one listing the shop has, one catalogue item, one new product.
    const csv = ["Item Name,Qty,Rate", "Ambrane 20W PD Type-C Fast Charger,12,445", "Popy 3 Fold Umbrella (Black),4,", "Hand-made jute bag,7,120"].join("\n");
    await page.locator('input[type="file"]').setInputFiles({ name: "stock.csv", mimeType: "text/csv", buffer: Buffer.from(csv) });
    await expect(page.getByRole("button", { name: /import/i }).last()).toBeVisible();
    await audit(page, testInfo, "owner import: preview", language);
  });
});
