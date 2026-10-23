import fs from "node:fs";
import path from "node:path";

import { type APIRequestContext, expect, type Page, test as base } from "@playwright/test";

/**
 * Public stock bands (P10.2; backend P10.1, plan section 8.20).
 *
 * 1. A shop that has not opted in to showing exact stock shows bands (In stock, Low stock, Out of stock) and no stock
 *    count on the search, product and shop pages.
 * 2. Once the shop opts in, its product page shows the exact count.
 * 3. The owner's "Show exact stock counts to customers" toggle in shop settings saves.
 *
 * Runs against the seeded throwaway database of the frontend-quality CI job, with NEARSHOP_DEMO_PASSWORD. The seed
 * leaves every shop without the opt-in (Shop.show_exact_stock is false by default). The tests that turn it on for the
 * demo owner's shop turn it off again when they finish, pass or fail, so the other specs and a second language's run
 * see the seeded state.
 */

type Options = { language: string };
const test = base.extend<Options>({ language: ["en", { option: true }] });

const OWNER = process.env.NEARSHOP_E2E_OWNER_EMAIL || "owner@nearshop.demo";
const CLIENT_HEADER = { "X-NearShop-Client": "web" };

type OwnerShop = { id: number; slug: string; show_exact_stock: boolean };
type OwnerListing = { id: number; name: string; quantity: number; is_active: boolean; stock_status: string };
type SearchResult = { groups: { best_offer: { id: number; shop: { slug: string } | null } }[] };
type PublicListing = { id: number; quantity: number; stock_exact: boolean; stock_status: string };

/** The interface text for a message key in the project's language, falling back to English per key as the site does
 * (src/lib/i18n.ts). Placeholders stay as they are. */
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

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** A message's text as a pattern: {count} is a number in any script (formatNumber may group it), and any other
 * placeholder is any text. */
function pattern(language: string, key: string) {
  const parts = message(language, key).split(/(\{\w+\})/);
  const body = parts
    .map((p) => (p === "{count}" ? "[\\p{Nd}][\\p{Nd},.]*" : /^\{\w+\}$/.test(p) ? ".*?" : escape(p)))
    .join("");
  return new RegExp(body, "u");
}

/** Every way the site states a stock count to customers. */
const COUNT_KEYS = [
  "common.stock.left",
  "common.stock.only",
  "common.stock.inStock",
  "discovery.product.stickyStock",
  "transactions.reserve.eachInStock",
];
const BAND_KEYS = ["common.stock.available", "common.stock.low", "common.stock.out"];

async function expectBandsOnly(page: Page, language: string, where: string) {
  const bands = BAND_KEYS.map((k) => message(language, k));
  // At least one band is shown...
  await expect(page.getByText(new RegExp(`^(${bands.map(escape).join("|")})$`)).first(), `${where}: a stock band`).toBeVisible();
  // ...and no stock count anywhere on the page.
  const text = await page.locator("body").innerText();
  for (const key of COUNT_KEYS) {
    expect(text, `${where}: no stock count (${key})`).not.toMatch(pattern(language, key));
  }
}

async function signInOwner(request: APIRequestContext) {
  const password = process.env.NEARSHOP_DEMO_PASSWORD;
  if (!password) throw new Error("Set NEARSHOP_DEMO_PASSWORD to the seeded demo accounts' password.");
  const r = await request.post("/api/auth/login", { data: { email: OWNER, password }, headers: CLIENT_HEADER });
  expect(r.ok(), `sign in as ${OWNER}: HTTP ${r.status()}`).toBeTruthy();
}

async function getJson<T>(request: APIRequestContext, url: string): Promise<T> {
  const r = await request.get(url);
  expect(r.ok(), `GET ${url}: HTTP ${r.status()}`).toBeTruthy();
  return (await r.json()) as T;
}

async function setExactStock(request: APIRequestContext, on: boolean) {
  const r = await request.patch("/api/owner/shop", { data: { show_exact_stock: on }, headers: CLIENT_HEADER });
  expect(r.ok(), `PATCH /api/owner/shop: HTTP ${r.status()}`).toBeTruthy();
  expect(((await r.json()) as OwnerShop).show_exact_stock).toBe(on);
}

/** An active listing at the demo owner's shop with stock, that search finds first, and the owner's shop. */
async function pickListing(request: APIRequestContext): Promise<{ shop: OwnerShop; listing: OwnerListing }> {
  const shop = await getJson<OwnerShop>(request, "/api/owner/shop");
  const listings = (await getJson<{ items: OwnerListing[] }>(request, "/api/owner/products?page_size=50")).items;
  for (const listing of listings) {
    if (!listing.is_active || listing.quantity < 1) continue;
    const found = await getJson<SearchResult>(request, `/api/search?q=${encodeURIComponent(listing.name)}&page_size=1`);
    if (found.groups[0]?.best_offer.id === listing.id) return { shop, listing };
  }
  throw new Error("No listing at the demo owner's shop comes first in search.");
}

test.describe.configure({ mode: "serial" });

test("a shop without the opt-in shows bands and no count on search, product and shop pages", async ({ page, language }) => {
  await signInOwner(page.request);
  await setExactStock(page.request, false); // the seeded state, in case an earlier run stopped half-way
  const { shop, listing } = await pickListing(page.request);
  await page.context().clearCookies({ name: "nearshop_session" });

  const pub = await getJson<PublicListing>(page.request, `/api/products/${listing.id}`);
  expect(pub.stock_exact, "the public listing is not exact").toBe(false);

  await page.goto(`/search?q=${encodeURIComponent(listing.name)}`);
  await expectBandsOnly(page, language, "search");

  await page.goto(`/product/${listing.id}`);
  await expectBandsOnly(page, language, "product page");

  await page.goto(`/shops/${shop.slug}`);
  await expectBandsOnly(page, language, "shop page");
});

test("an opted-in shop shows its exact count on the product page", async ({ page, language }) => {
  await signInOwner(page.request);
  const { listing } = await pickListing(page.request);
  try {
    await setExactStock(page.request, true);
    const pub = await getJson<PublicListing>(page.request, `/api/products/${listing.id}`);
    expect(pub.stock_exact).toBe(true);
    expect(pub.quantity, "the public listing carries the exact count").toBe(listing.quantity);
    await page.goto(`/product/${listing.id}`);
    const key = pub.stock_status === "low_stock" ? "common.stock.only" : "common.stock.inStock";
    await expect(page.getByText(pattern(language, key)).first(), "the exact count on the product page").toBeVisible();
  } finally {
    await setExactStock(page.request, false);
  }
});

test("the owner's exact-stock toggle saves", async ({ page, language }) => {
  await signInOwner(page.request);
  await setExactStock(page.request, false);
  try {
    await page.goto("/shop/settings");
    const toggle = page.locator("#exact-stock");
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await expect(page.getByText(message(language, "owner.settings.exactStockLabel"))).toBeVisible();
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    const saved = page.waitForResponse((r) => r.url().endsWith("/api/owner/shop") && r.request().method() === "PATCH");
    await page.getByRole("button", { name: message(language, "owner.settings.save") }).click();
    const response = await saved;
    expect(response.ok(), `save: HTTP ${response.status()}`).toBeTruthy();
    expect(((await response.json()) as OwnerShop).show_exact_stock).toBe(true);

    await page.reload();
    await expect(page.locator("#exact-stock")).toHaveAttribute("aria-checked", "true");
    expect((await getJson<OwnerShop>(page.request, "/api/owner/shop")).show_exact_stock).toBe(true);
  } finally {
    await setExactStock(page.request, false);
  }
});
