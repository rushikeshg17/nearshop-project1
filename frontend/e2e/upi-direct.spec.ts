import AxeBuilder from "@axe-core/playwright";
import { type APIRequestContext, type Browser, expect, type Locator, type Page, test as base } from "@playwright/test";

/**
 * Paying the shop directly by UPI on screen (P6.2; backend P6.1, plan section 8.8).
 *
 * 1. The "Pay the shop by UPI" block shows on the customer's held reservation and confirmed order only while the shop
 *    has a UPI ID, and its upi://pay link carries the shop's UPI ID, the payee name, the amount and the reservation or
 *    order code as the note. The QR code is drawn next to it. No public shop response carries the UPI ID.
 * 2. The owner cannot complete a pickup or deliver an order without choosing how the customer paid: the confirm
 *    button stays disabled with the right code until a method is chosen, and UPI is offered only once the shop has a
 *    UPI ID. The chosen method is what the customer's reservation or order records.
 * 3. The cancellation and refund policy (plan check 9.5) is visible in the reserve and delivery dialogs before
 *    anything is submitted, with its link to the terms.
 *
 * Runs against the seeded throwaway database of the frontend-quality CI job, signed in as the demo customer and owner
 * with NEARSHOP_DEMO_PASSWORD. The spec sets a test UPI ID on the demo owner's shop and puts the shop's own UPI
 * settings back afterwards; reservations and orders it leaves open are cancelled, so the stock goes back.
 */

type Options = { language: string };
const test = base.extend<Options>({ language: ["en", { option: true }] });

const CUSTOMER = process.env.NEARSHOP_E2E_CUSTOMER_EMAIL || "priya@nearshop.demo";
const OWNER = process.env.NEARSHOP_E2E_OWNER_EMAIL || "owner@nearshop.demo";
const CLIENT_HEADER = { "X-NearShop-Client": "web" };
const MAX_ACTIVE_RESERVATIONS = 5; // backend/app/services/reservations.py, MAX_ACTIVE_PER_CUSTOMER
const TEST_UPI_ID = "e2e.testshop@okaxis";
const TEST_PAYEE = "E2E Test Shop";
const BLOCKING = new Set(["serious", "critical"]);
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22a", "wcag22aa"];

type Listing = { id: number; name: string; quantity: number; is_active: boolean };
type Reservation = {
  id: number;
  code: string;
  status: string;
  total: number;
  created_at: string;
  product: { id: number };
  payment_method?: string | null;
  payment_status?: string | null;
};
type Order = { id: number; code: string; status: string; total: number; delivery_code?: string | null; payment_method: string; payment_status: string };
type OwnerShop = { id: number; slug: string; lat: number; lng: number; upi_id: string | null; upi_payee_name: string | null };

function demoPassword() {
  const password = process.env.NEARSHOP_DEMO_PASSWORD;
  if (!password) throw new Error("Set NEARSHOP_DEMO_PASSWORD to the seeded demo accounts' password.");
  return password;
}

async function signIn(request: APIRequestContext, email: string) {
  const r = await request.post("/api/auth/login", { data: { email, password: demoPassword() }, headers: CLIENT_HEADER });
  expect(r.ok(), `sign in as ${email}: HTTP ${r.status()}`).toBeTruthy();
}

async function getJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const r = await request.get(path);
  expect(r.ok(), `GET ${path}: HTTP ${r.status()}`).toBeTruthy();
  return (await r.json()) as T;
}

async function send<T = unknown>(request: APIRequestContext, method: "POST" | "PATCH", path: string, body: unknown = {}): Promise<T> {
  const r = await request.fetch(path, { method, data: body, headers: CLIENT_HEADER });
  expect(r.ok(), `${method} ${path}: HTTP ${r.status()} ${await r.text()}`).toBeTruthy();
  return (await r.json()) as T;
}

const post = <T = unknown>(request: APIRequestContext, path: string, body: unknown = {}) => send<T>(request, "POST", path, body);

async function newSignedIn(browser: Browser, baseURL: string | undefined, email: string) {
  const context = await browser.newContext({ baseURL });
  await signIn(context.request, email);
  return context;
}

/** Sets (or, with null, removes) the demo shop's UPI ID through the owner's settings API. */
async function setShopUpi(owner: APIRequestContext, upiId: string | null, payee: string | null = null) {
  return send<OwnerShop>(owner, "PATCH", "/api/owner/shop", { upi_id: upiId, upi_payee_name: payee });
}

/** Listings at the demo owner's shop with stock that the customer has no open reservation for. */
async function pickListings(customer: APIRequestContext, owner: APIRequestContext, count: number): Promise<Listing[]> {
  const active = await getJson<Reservation[]>(customer, "/api/reservations?scope=active");
  const oldestFirst = [...active].sort((a, b) => a.created_at.localeCompare(b.created_at));
  for (const r of oldestFirst.slice(0, Math.max(0, active.length - (MAX_ACTIVE_RESERVATIONS - 2)))) {
    await post(customer, `/api/reservations/${r.id}/cancel`, { reason: "upi-direct test" });
  }
  const reserved = new Set(active.map((r) => r.product.id));
  const listings = (await getJson<{ items: Listing[] }>(owner, "/api/owner/products?page_size=50")).items;
  const free = listings.filter((l) => l.is_active && l.quantity >= 2 && !reserved.has(l.id)).slice(0, count);
  if (free.length < count) throw new Error("The demo owner's shop has too few listings in stock to reserve or order.");
  return free;
}

async function placeOrder(customer: APIRequestContext, shop: OwnerShop, listing: Listing) {
  return post<Order>(customer, "/api/orders", {
    lat: shop.lat + 0.002,
    lng: shop.lng + 0.002,
    items: [{ product_id: listing.id, quantity: 1 }],
    address: "12 Test Street, near the park",
    phone: "9876501234",
    note: "upi-direct test",
  });
}

/** Cancels what a test left open, so its stock goes back on the shelf. */
async function cleanUp(customer: APIRequestContext, owner: APIRequestContext, reservations: number[], orders: number[]) {
  const reason = { reason: "End-to-end check finished" };
  for (const id of reservations) {
    const { status } = await getJson<Reservation>(customer, `/api/reservations/${id}`);
    if (["REQUESTED", "CONFIRMED", "READY_FOR_PICKUP"].includes(status)) await post(customer, `/api/reservations/${id}/cancel`, reason);
  }
  for (const id of orders) {
    const { status } = await getJson<Order>(customer, `/api/orders/${id}`);
    if (status === "OUT_FOR_DELIVERY") {
      await post(owner, `/api/owner/orders/${id}/fail`, reason);
      await post(owner, `/api/owner/orders/${id}/return`, reason);
    } else if (["PENDING", "SHOP_CONFIRMED", "PREPARING"].includes(status)) {
      await post(owner, `/api/owner/orders/${id}/cancel`, reason);
    }
  }
}

/** axe on one part of the page: serious or critical violations fail (soft, so the test carries on). */
async function auditPart(page: Page, selector: string, what: string) {
  const results = await new AxeBuilder({ page }).include(selector).withTags(WCAG_TAGS).analyze();
  const blocking = results.violations.filter((v) => BLOCKING.has(v.impact ?? ""));
  expect.soft(blocking.map((v) => `${v.id}: ${v.help}`), `accessibility of ${what}`).toEqual([]);
}

/** The pay block's link, parsed: upi://pay?pa=...&pn=...&am=...&cu=INR&tn=... */
async function payLink(block: Locator) {
  const href = await block.getByRole("link", { name: /by UPI$/ }).getAttribute("href");
  expect(href, "the pay link").toMatch(/^upi:\/\/pay\?/);
  return new URL(href!);
}

function expectPayLink(link: URL, amount: number, note: string) {
  expect(link.protocol).toBe("upi:");
  expect(link.searchParams.get("pa")).toBe(TEST_UPI_ID);
  expect(link.searchParams.get("pn")).toBe(TEST_PAYEE);
  expect(link.searchParams.get("am")).toBe(amount.toFixed(2));
  expect(link.searchParams.get("cu")).toBe("INR");
  expect(link.searchParams.get("tn")).toBe(note);
}

test("the pay block shows only when the shop has a UPI ID, with the right payee, amount and note", async ({ page, browser }, testInfo) => {
  const owner = await newSignedIn(browser, testInfo.project.use.baseURL, OWNER);
  await signIn(page.request, CUSTOMER);
  const original = await getJson<OwnerShop>(owner.request, "/api/owner/shop");
  const reservations: number[] = [];
  const orders: number[] = [];
  try {
    await setShopUpi(owner.request, null);
    const [first, second] = await pickListings(page.request, owner.request, 2);
    const reservation = await post<Reservation>(page.request, "/api/reservations", { product_id: first.id, quantity: 1 });
    reservations.push(reservation.id);
    await post(owner.request, `/api/owner/reservations/${reservation.id}/confirm`);
    const order = await placeOrder(page.request, original, second);
    orders.push(order.id);
    await post(owner.request, `/api/owner/orders/${order.id}/confirm`);

    // Without a UPI ID: no pay block, on either page, and nothing in the customer's responses.
    const payBlock = page.getByRole("region", { name: "Pay the shop by UPI" });
    await page.goto(`/account/reservations/${reservation.id}`);
    await expect(page.getByRole("region", { name: "Show this at the counter" })).toBeVisible();
    await expect(payBlock).toHaveCount(0);
    await page.goto(`/account/orders/${order.id}`);
    await expect(page.getByRole("region", { name: "Your delivery code" })).toBeVisible();
    await expect(payBlock).toHaveCount(0);

    // With one: the block, its link and its QR code.
    const saved = await setShopUpi(owner.request, TEST_UPI_ID, TEST_PAYEE);
    expect(saved.upi_id).toBe(TEST_UPI_ID);
    await page.reload();
    await expect(payBlock).toBeVisible();
    expectPayLink(await payLink(payBlock), order.total, order.code);
    await expect(payBlock.getByRole("img", { name: /^QR code to pay/ })).toBeVisible();
    await expect(payBlock).toContainText(TEST_UPI_ID);
    await auditPart(page, "[aria-labelledby='pay-upi-title']", "the order's pay block");

    await page.goto(`/account/reservations/${reservation.id}`);
    await expect(payBlock).toBeVisible();
    expectPayLink(await payLink(payBlock), reservation.total, reservation.code);
    await expect(payBlock.getByRole("img", { name: /^QR code to pay/ })).toBeVisible();

    // The UPI ID is only in the customer's own responses (and the owner's settings), never on the public shop.
    const publicShop = await page.request.get(`/api/shops/${original.slug}`);
    expect(publicShop.ok()).toBeTruthy();
    expect(await publicShop.text()).not.toContain(TEST_UPI_ID);
  } finally {
    await cleanUp(page.request, owner.request, reservations, orders);
    await setShopUpi(owner.request, original.upi_id, original.upi_payee_name);
    await owner.close();
  }
});

test("the owner cannot complete or deliver without choosing how the customer paid", async ({ page, browser }, testInfo) => {
  const customer = await newSignedIn(browser, testInfo.project.use.baseURL, CUSTOMER);
  await signIn(page.request, OWNER);
  const original = await getJson<OwnerShop>(page.request, "/api/owner/shop");
  const reservations: number[] = [];
  const orders: number[] = [];
  try {
    await setShopUpi(page.request, null);
    const [first, second] = await pickListings(customer.request, page.request, 2);
    const reservation = await post<Reservation>(customer.request, "/api/reservations", { product_id: first.id, quantity: 1 });
    reservations.push(reservation.id);
    await post(page.request, `/api/owner/reservations/${reservation.id}/confirm`);

    // No UPI ID: cash is the only method offered, and it still has to be chosen.
    await page.goto("/shop/reservations");
    const card = page.getByRole("listitem").filter({ hasText: `#${reservation.id}` });
    await card.getByRole("button", { name: "Picked up and paid" }).click();
    let dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await dialog.getByLabel("Pickup code").fill(reservation.code.replace(/^R-/, ""));
    const confirm = dialog.getByRole("button", { name: "Picked up and paid" });
    await expect(confirm).toBeDisabled();
    await expect(dialog.getByRole("radio", { name: /^Cash/ })).toBeVisible();
    await expect(dialog.getByRole("radio", { name: /^UPI/ })).toHaveCount(0);
    await expect(dialog.getByRole("link", { name: "Shop settings" })).toBeVisible();
    await dialog.getByRole("radio", { name: /^Cash/ }).click();
    await expect(confirm).toBeEnabled();
    await auditPart(page, "[role='dialog']", "the code and payment dialog");
    await dialog.getByRole("button", { name: "Not yet" }).click();
    await expect(dialog).toBeHidden();
    expect((await getJson<Reservation>(customer.request, `/api/reservations/${reservation.id}`)).status).toBe("CONFIRMED");

    // With a UPI ID: UPI is offered; completing records it.
    await setShopUpi(page.request, TEST_UPI_ID, TEST_PAYEE);
    await page.reload();
    await card.getByRole("button", { name: "Picked up and paid" }).click();
    dialog = page.getByRole("dialog");
    await dialog.getByLabel("Pickup code").fill(reservation.code.replace(/^R-/, ""));
    await expect(confirm).toBeDisabled();
    await dialog.getByRole("radio", { name: /^UPI/ }).click();
    await confirm.click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText("Completed. UPI payment and sale recorded.")).toBeVisible();
    const completed = await getJson<Reservation>(customer.request, `/api/reservations/${reservation.id}`);
    expect(completed).toMatchObject({ status: "COMPLETED", payment_method: "upi_direct", payment_status: "paid" });

    // Delivering an order: the same choice, next to the delivery code.
    const order = await placeOrder(customer.request, original, second);
    orders.push(order.id);
    await post(page.request, `/api/owner/orders/${order.id}/confirm`);
    await post(page.request, `/api/owner/orders/${order.id}/prepare`);
    await post(page.request, `/api/owner/orders/${order.id}/dispatch`);
    const code = (await getJson<Order>(customer.request, `/api/orders/${order.id}`)).delivery_code;
    expect(code).toMatch(/^\d{4}$/);
    await page.goto("/shop/orders");
    const orderCard = page.getByRole("listitem").filter({ hasText: order.code });
    await orderCard.getByRole("button", { name: "Delivered and paid" }).click();
    dialog = page.getByRole("dialog");
    await dialog.getByLabel("Delivery code").fill(code!);
    const deliver = dialog.getByRole("button", { name: "Delivered and paid" });
    await expect(deliver).toBeDisabled();
    await dialog.getByRole("radio", { name: /^Cash/ }).click();
    await deliver.click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText("Delivered. Cash payment and sale recorded.")).toBeVisible();
    const delivered = await getJson<Order>(customer.request, `/api/orders/${order.id}`);
    expect(delivered).toMatchObject({ status: "DELIVERED", payment_method: "cod", payment_status: "paid" });
  } finally {
    await cleanUp(customer.request, page.request, reservations, orders);
    await setShopUpi(page.request, original.upi_id, original.upi_payee_name);
    await customer.close();
  }
});

test("the cancellation and refund policy shows in the reserve and delivery dialogs before submitting", async ({ page, browser }, testInfo) => {
  const owner = await newSignedIn(browser, testInfo.project.use.baseURL, OWNER);
  await signIn(page.request, CUSTOMER);
  const [listing] = await pickListings(page.request, owner.request, 1);
  await owner.close();
  const before = {
    reservations: (await getJson<Reservation[]>(page.request, "/api/reservations?scope=active")).length,
    orders: (await getJson<Order[]>(page.request, "/api/orders?scope=active")).length,
  };

  await page.goto(`/product/${listing.id}`);
  await page.getByRole("button", { name: "Reserve for pickup" }).click();
  let dialog = page.getByRole("dialog");
  let policy = dialog.getByRole("region", { name: "Cancellation and refunds" });
  await expect(policy).toBeVisible();
  await expect(policy).toContainText("Free cancellation before pickup");
  await expect(policy).toContainText("refunds you within 5 days");
  await expect(policy.getByRole("link", { name: "Read the cancellation and refund policy" })).toHaveAttribute(
    "href",
    "/terms#cancellation-and-refunds",
  );
  // On a 360 by 800 phone the sheet scrolls: the policy and the submit button can both be on screen.
  await policy.scrollIntoViewIfNeeded();
  await expect(policy).toBeInViewport();
  await expect(dialog.getByRole("button", { name: /^Reserve/ })).toBeInViewport();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  // Wait for the browser's own fetch (distance, delivery quote): the delivery button depends on it.
  await expect(page.getByText(/Delivery .* away/)).toBeVisible();
  await page.getByRole("button", { name: "Request delivery" }).click();
  dialog = page.getByRole("dialog");
  policy = dialog.getByRole("region", { name: "Cancellation and refunds" });
  await expect(policy).toBeVisible();
  await expect(policy).toContainText("Cancelling is free before dispatch");
  await expect(policy).toContainText("until the shop starts packing");
  await expect(policy).toContainText("refunds you within 5 days");
  await expect(policy.getByRole("link", { name: "Read the cancellation and refund policy" })).toHaveAttribute(
    "href",
    "/terms#cancellation-and-refunds",
  );
  await policy.scrollIntoViewIfNeeded();
  await expect(policy).toBeInViewport();
  await expect(dialog.getByRole("button", { name: /^Place order/ })).toBeInViewport();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  // Nothing was placed: the policy was read before submitting.
  expect((await getJson<Reservation[]>(page.request, "/api/reservations?scope=active")).length).toBe(before.reservations);
  expect((await getJson<Order[]>(page.request, "/api/orders?scope=active")).length).toBe(before.orders);
});
