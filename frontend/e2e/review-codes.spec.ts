import crypto from "node:crypto";

import { type APIRequestContext, type Browser, expect, type Page, test as base } from "@playwright/test";

/**
 * Completion codes, delivery codes and review reports on screen (P3.7; rules from P3.5 and P3.6, plan section 8.2).
 *
 * 1. The demo owner completes a confirmed pickup from the reservations queue: a wrong code shows the error in the
 *    code dialog and the reservation stays open; the code from the customer's screen (typed without its R-, as the
 *    customer sees it) completes it.
 * 2. A confirmed delivery order shows its 4-digit delivery code on the customer's order page, and not on the
 *    owner's order card (nor in the owner's API response). Out for delivery, "Delivered, cash collected" with a
 *    wrong code shows the error in the code dialog and the order stays out; the customer's code delivers it. If
 *    the test stops before that, the order is cancelled (or failed and returned) so the stock goes back.
 * 3. A shop review reported by 3 different users disappears from the shop page: two report through the API, the
 *    third through the report dialog on the page.
 *
 * Runs against the seeded throwaway database of the frontend-quality CI job, with NEARSHOP_DEMO_PASSWORD, and with
 * sign-ups open (any environment but production): the seed has only one customer who can sign in, so the third test
 * registers its reporters. The API must run with completion codes on (NEARSHOP_COMPLETION_CODES_REQUIRED, the
 * default since P3.7).
 */

type Options = { language: string };
const test = base.extend<Options>({ language: ["en", { option: true }] });

const CUSTOMER = process.env.NEARSHOP_E2E_CUSTOMER_EMAIL || "priya@nearshop.demo";
const OWNER = process.env.NEARSHOP_E2E_OWNER_EMAIL || "owner@nearshop.demo";
const CLIENT_HEADER = { "X-NearShop-Client": "web" };
const MAX_ACTIVE_RESERVATIONS = 5; // backend/app/services/reservations.py, MAX_ACTIVE_PER_CUSTOMER

type Listing = { id: number; name: string; quantity: number; is_active: boolean };
type Reservation = { id: number; code: string; status: string; created_at: string; product: { id: number } };
type Order = { id: number; code: string; status: string; delivery_code?: string | null };
type OwnerShop = { id: number; slug: string; lat: number; lng: number };
type PublicReview = { id: number };

function demoPassword() {
  const password = process.env.NEARSHOP_DEMO_PASSWORD;
  if (!password) throw new Error("Set NEARSHOP_DEMO_PASSWORD to the seeded demo accounts' password.");
  return password;
}

async function signIn(request: APIRequestContext, email: string, password = demoPassword()) {
  const r = await request.post("/api/auth/login", { data: { email, password }, headers: CLIENT_HEADER });
  expect(r.ok(), `sign in as ${email}: HTTP ${r.status()}`).toBeTruthy();
}

async function getJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const r = await request.get(path);
  expect(r.ok(), `GET ${path}: HTTP ${r.status()}`).toBeTruthy();
  return (await r.json()) as T;
}

async function post<T = unknown>(request: APIRequestContext, path: string, body: unknown = {}): Promise<T> {
  const r = await request.post(path, { data: body, headers: CLIENT_HEADER });
  expect(r.ok(), `POST ${path}: HTTP ${r.status()} ${await r.text()}`).toBeTruthy();
  return (await r.json()) as T;
}

async function newSignedIn(browser: Browser, baseURL: string | undefined, email: string, password?: string) {
  const context = await browser.newContext({ baseURL });
  await signIn(context.request, email, password);
  return context;
}

/** A listing at the demo owner's shop with stock that the customer has no open reservation for. */
async function pickListing(customer: APIRequestContext, owner: APIRequestContext, minQuantity = 1): Promise<Listing> {
  const active = await getJson<Reservation[]>(customer, "/api/reservations?scope=active");
  const oldestFirst = [...active].sort((a, b) => a.created_at.localeCompare(b.created_at));
  for (const r of oldestFirst.slice(0, Math.max(0, active.length - (MAX_ACTIVE_RESERVATIONS - 2)))) {
    await post(customer, `/api/reservations/${r.id}/cancel`, { reason: "review-codes test" });
  }
  const reserved = new Set(active.map((r) => r.product.id));
  const listings = (await getJson<{ items: Listing[] }>(owner, "/api/owner/products?page_size=50")).items;
  const listing = listings.find((l) => l.is_active && l.quantity >= minQuantity && !reserved.has(l.id));
  if (!listing) throw new Error("The demo owner's shop has no listing in stock to reserve or order.");
  return listing;
}

/** The code dialog the queue cards open (a dialog on desktop, a bottom sheet on phones: both have role dialog). */
function codeDialog(page: Page) {
  return page.getByRole("dialog");
}

test("completing a pickup needs the customer's code: a wrong one shows an error, the right one completes", async ({ page, browser }, testInfo) => {
  const customer = await newSignedIn(browser, testInfo.project.use.baseURL, CUSTOMER);
  await signIn(page.request, OWNER);
  const listing = await pickListing(customer.request, page.request);
  const reservation = await post<Reservation>(customer.request, "/api/reservations", { product_id: listing.id, quantity: 1 });
  await post(page.request, `/api/owner/reservations/${reservation.id}/confirm`);

  await page.goto("/shop/reservations");
  const card = page.getByRole("listitem").filter({ hasText: `#${reservation.id}` });
  await expect(card).toBeVisible();
  // The owner's card does not show the code: the customer shows it at the counter.
  await expect(card).not.toContainText(reservation.code.replace(/^R-/, ""));

  await card.getByRole("button", { name: "Picked up and paid" }).click();
  const dialog = codeDialog(page);
  await expect(dialog).toBeVisible();
  const field = dialog.getByLabel("Pickup code");
  const wrong = reservation.code.endsWith("ZZZZZZ") ? "YYYYYY" : "ZZZZZZ";
  await field.fill(wrong);
  await dialog.getByRole("button", { name: "Picked up and paid" }).click();
  await expect(dialog.getByRole("alert")).toContainText("That is not this reservation's code");
  await expect(field).toHaveAttribute("aria-invalid", "true");
  expect((await getJson<Reservation>(customer.request, `/api/reservations/${reservation.id}`)).status).toBe("CONFIRMED");

  // As the customer's screen shows it: without the R-, in lower case does not matter either.
  await field.fill(reservation.code.replace(/^R-/, "").toLowerCase());
  await dialog.getByRole("button", { name: "Picked up and paid" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText("Completed. Sale recorded.")).toBeVisible();
  expect((await getJson<Reservation>(customer.request, `/api/reservations/${reservation.id}`)).status).toBe("COMPLETED");
  await customer.close();
});

test("the delivery code shows on the customer's confirmed order, not on the owner's card, and delivering needs it", async ({ page, browser }, testInfo) => {
  const owner = await newSignedIn(browser, testInfo.project.use.baseURL, OWNER);
  await signIn(page.request, CUSTOMER);
  const shop = await getJson<OwnerShop>(owner.request, "/api/owner/shop");
  const listing = await pickListing(page.request, owner.request);
  const order = await post<Order>(page.request, "/api/orders", {
    lat: shop.lat + 0.002,
    lng: shop.lng + 0.002,
    items: [{ product_id: listing.id, quantity: 1 }],
    address: "12 Test Street, near the park",
    phone: "9876501234",
    note: "review-codes test",
  });
  expect(order.delivery_code ?? null, "no code before the shop confirms").toBeNull();
  const confirmed = await post<Order>(owner.request, `/api/owner/orders/${order.id}/confirm`);
  expect(confirmed, "the owner's response never carries the delivery code").not.toHaveProperty("delivery_code");
  const code = (await getJson<Order>(page.request, `/api/orders/${order.id}`)).delivery_code;
  expect(code).toMatch(/^\d{4}$/);

  let delivered = false;
  try {
    await page.goto(`/account/orders/${order.id}`);
    const panel = page.getByRole("region", { name: "Your delivery code" });
    await expect(panel).toBeVisible();
    await expect(panel).toContainText(code!);

    const ownerPage = await owner.newPage();
    await ownerPage.goto("/shop/orders");
    const card = ownerPage.getByRole("listitem").filter({ hasText: order.code });
    await expect(card).toBeVisible();
    await expect(card.getByRole("button", { name: "Delivered, cash collected" }).or(card.getByRole("button", { name: "Start packing" })).first()).toBeVisible();
    await expect(card).not.toContainText("delivery code", { ignoreCase: true });
    for (const owned of await getJson<Order[]>(owner.request, "/api/owner/orders?scope=active")) {
      expect(owned).not.toHaveProperty("delivery_code");
    }

    // Out for delivery, then the doorstep: a wrong code fails, the customer's code delivers.
    await post(owner.request, `/api/owner/orders/${order.id}/prepare`);
    await post(owner.request, `/api/owner/orders/${order.id}/dispatch`);
    await ownerPage.reload();
    await expect(card).toBeVisible();
    await card.getByRole("button", { name: "Delivered, cash collected" }).click();
    const dialog = codeDialog(ownerPage);
    await expect(dialog).toBeVisible();
    const field = dialog.getByLabel("Delivery code");
    await field.fill(code === "0000" ? "1111" : "0000");
    await dialog.getByRole("button", { name: "Delivered, cash collected" }).click();
    await expect(dialog.getByRole("alert")).toContainText("That is not this order's delivery code");
    await expect(field).toHaveAttribute("aria-invalid", "true");
    expect((await getJson<Order>(page.request, `/api/orders/${order.id}`)).status).toBe("OUT_FOR_DELIVERY");

    await field.fill(code!);
    await dialog.getByRole("button", { name: "Delivered, cash collected" }).click();
    await expect(dialog).toBeHidden();
    await expect(ownerPage.getByText("Delivered. Cash collected and sale recorded.")).toBeVisible();
    expect((await getJson<Order>(page.request, `/api/orders/${order.id}`)).status).toBe("DELIVERED");
    delivered = true;
  } finally {
    if (!delivered) {
      // Put the stock back: cancel before dispatch; once out for delivery, fail it and return it to the shop.
      const { status } = await getJson<Order>(page.request, `/api/orders/${order.id}`);
      const reason = { reason: "End-to-end check finished" };
      if (status === "OUT_FOR_DELIVERY") {
        await post(owner.request, `/api/owner/orders/${order.id}/fail`, reason);
        await post(owner.request, `/api/owner/orders/${order.id}/return`, reason);
      } else if (["PENDING", "SHOP_CONFIRMED", "PREPARING"].includes(status)) {
        await post(owner.request, `/api/owner/orders/${order.id}/cancel`, reason);
      }
    }
    await owner.close();
  }
});

test("a review reported by 3 users disappears from the shop page", async ({ page, browser }, testInfo) => {
  const baseURL = testInfo.project.use.baseURL;
  const owner = await newSignedIn(browser, baseURL, OWNER);
  const shop = await getJson<OwnerShop>(owner.request, "/api/owner/shop");
  await owner.close();
  const before = await getJson<{ reviews: PublicReview[] }>(page.request, `/api/shops/${shop.slug}/reviews?page_size=10`);
  test.skip(!before.reviews.length, "The demo shop has no visible review left to report.");
  const review = before.reviews[0];

  // Three reporters: fresh customer accounts, as the seed has only one customer who can sign in.
  const reporters: { email: string; password: string }[] = [];
  for (let i = 0; i < 3; i++) {
    const email = `e2e-reporter-${Date.now()}-${i}-${crypto.randomBytes(3).toString("hex")}@example.com`;
    const password = crypto.randomBytes(18).toString("base64url");
    const context = await browser.newContext({ baseURL });
    const r = await context.request.post("/api/auth/register", {
      data: { name: `Reporter ${i + 1}`, email, password, consent: { account_and_orders: true, age_18_plus: true } },
      headers: CLIENT_HEADER,
    });
    expect(r.ok(), `register a reporter: HTTP ${r.status()} ${await r.text()}`).toBeTruthy();
    await context.close();
    reporters.push({ email, password });
  }
  for (const reporter of reporters.slice(0, 2)) {
    const context = await newSignedIn(browser, baseURL, reporter.email, reporter.password);
    await post(context.request, `/api/reviews/${review.id}/report`, { reason: "fake", note: "End-to-end check" });
    await context.close();
  }
  // Two reports do not hide it.
  await page.goto(`/shops/${shop.slug}`);
  const item = page.locator(`#review-${review.id}`);
  await expect(item).toBeVisible();

  // The third, through the page.
  await signIn(page.request, reporters[2].email, reporters[2].password);
  await page.reload();
  await item.getByRole("button", { name: "Report this review" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("radio", { name: /^Fake/ }).click();
  await dialog.getByRole("button", { name: "Send report" }).click();
  await expect(page.getByText("Thanks. An admin will look at it within 3 days.")).toBeVisible();

  // Hidden until an admin decides: gone from the public list and from the page.
  const after = await getJson<{ reviews: PublicReview[] }>(page.request, `/api/shops/${shop.slug}/reviews?page_size=50`);
  expect(after.reviews.map((r) => r.id)).not.toContain(review.id);
  await page.reload();
  await expect(page.locator("#reviews")).toBeVisible();
  await expect(item).toHaveCount(0);
});
