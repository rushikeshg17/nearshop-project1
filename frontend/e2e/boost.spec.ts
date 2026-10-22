import fs from "node:fs";
import path from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { type APIRequestContext, expect, type Page, type Route, test as base } from "@playwright/test";

/**
 * Boost screens (P10.13; rules from P10.12, plan section 8.22).
 *
 * 1. The sponsored card's first line is the label, its link's accessible name starts with the label, the label keeps
 *    4.5:1 contrast (axe's color-contrast rule) and stays whole at twice the root font size (the largest Android
 *    font scale), and the card sits at its position among the organic cards. Opening it posts its token.
 * 2. A results page never shows more than one sponsored card, even when a later page's response carries one.
 * 3. The owner's Boost page shows a pack's impressions, clicks and requests and its make-good, passes axe (WCAG 2.2
 *    A and AA, no serious or critical violation) at 360 px, and the owner menu offers Boost in an eligible city.
 * 4. The owner menu hides Boost in an ineligible city.
 *
 * Runs against the seeded throwaway database of the frontend-quality CI job, with NEARSHOP_DEMO_PASSWORD. The seed
 * has no eligible city (100 verified shops, 3,000 monthly customers for 4 weeks), Boost sales stay off until B31, and
 * a pack starts only once NearShop matches its payment, so no real search carries a sponsored card and no shop has a
 * pack with figures. The spec therefore adds the card to the real search response (built from that response's own
 * first result, as the API builds it) and serves the owner's Boost eligibility and packs from page.route;
 * everything else on the pages is real.
 */

type Options = { language: string };
const test = base.extend<Options>({ language: ["en", { option: true }] });

const OWNER = process.env.NEARSHOP_E2E_OWNER_EMAIL || "owner@nearshop.demo";
const CLIENT_HEADER = { "X-NearShop-Client": "web" };
const QUERY = "umbrella"; // a query every seed has (the frontend-quality job picks its pages with it)
const SLOT = 3; // settings.boost_slot_position
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22a", "wcag22aa"];

/** The interface text for a message key in the project's language, as the site shows it: a key that is absent or
 * empty in that language's file falls back to the English text (src/lib/i18n.ts), and {name} placeholders are
 * filled in. So the assertions hold in every language on the CI list, before and after its text arrives. */
function message(language: string, key: string, values: Record<string, string> = {}) {
  const lookup = (code: string) => {
    let node: unknown = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "src", "messages", `${code}.json`), "utf8"));
    for (const part of key.split(".")) node = (node as Record<string, unknown> | undefined)?.[part];
    return typeof node === "string" && node ? node : null;
  };
  const text = lookup(language) ?? lookup("en");
  if (text === null) throw new Error(`No message ${key}`);
  return text.replace(/\{(\w+)\}/g, (whole, name: string) => values[name] ?? whole);
}

/** A whole-rupee price as the site's formatPrice writes it (en-IN in every language), formatted in the page. */
function price(page: Page, value: number) {
  return page.evaluate(
    (v) => new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(v),
    value,
  );
}

async function signInOwner(request: APIRequestContext) {
  const password = process.env.NEARSHOP_DEMO_PASSWORD;
  if (!password) throw new Error("Set NEARSHOP_DEMO_PASSWORD to the seeded demo accounts' password.");
  const r = await request.post("/api/auth/login", { data: { email: OWNER, password }, headers: CLIENT_HEADER });
  expect(r.ok(), `sign in as ${OWNER}: HTTP ${r.status()}`).toBeTruthy();
}

// ------------------------------------------------------------------------------------------------ the search page

type Offer = { id: number; price: number; shop: { id: number; name: string } } & Record<string, unknown>;
type Group = { key: string; name: string; brand: string | null; unit: string | null; icon: string; image_url: string | null;
  thumbnail_url: string | null; category: string; catalog_item_id: number | null; best_offer: Offer } & Record<string, unknown>;
type Results = { page: number; has_more: boolean; groups: Group[]; sponsored?: unknown } & Record<string, unknown>;

const TOKEN = "b1.e2e-sponsored-token";

/** A sponsored card as app/services/search.py builds it, for a group's best offer. */
function cardFor(group: Group, position: number, token = TOKEN) {
  return {
    position,
    sponsored: true,
    label: "Sponsored",
    token,
    key: group.key,
    catalog_item_id: group.catalog_item_id,
    name: group.name,
    brand: group.brand,
    unit: group.unit,
    icon: group.icon,
    image_url: group.image_url,
    thumbnail_url: group.thumbnail_url,
    category: group.category,
    listing: group.best_offer,
  };
}

const isSearch = (url: URL) => url.pathname === "/api/search";

/** Adds a sponsored card to the real first page. With `secondPage`, the first page also says there is more, and the
 * second page (made from the first page's groups under new keys) carries a card too, which the screen must ignore. */
async function withSponsored(page: Page, { secondPage = false } = {}) {
  let first: Results | null = null;
  await page.route(isSearch, async (route: Route) => {
    const url = new URL(route.request().url());
    const n = Number(url.searchParams.get("page") || "1");
    if (n > 1 && first) {
      const groups = first.groups.map((g) => ({ ...g, key: `${g.key}-p${n}` }));
      await route.fulfill({ json: { ...first, page: n, has_more: false, groups, sponsored: cardFor(groups[0], 1, `${TOKEN}-p${n}`) } });
      return;
    }
    const response = await route.fetch();
    const real = (await response.json()) as Results;
    expect(real.groups.length, `the seed has results for ${QUERY}`).toBeGreaterThan(0);
    first = {
      ...real,
      has_more: secondPage ? true : real.has_more,
      sponsored: cardFor(real.groups[0], Math.min(SLOT, real.groups.length + 1)),
    };
    await route.fulfill({ response, json: first });
  });
  return () => first;
}

test("the sponsored card carries the label in its first line and in its accessible name", async ({ page, language }) => {
  const results = await withSponsored(page);
  await page.goto(`/search?q=${QUERY}`);
  const card = page.getByTestId("sponsored-card");
  await expect(card).toHaveCount(1);
  const first = results()!;
  const group = first.groups[0];
  const label = message(language, "discovery.result.sponsored.label");

  // The label is the card's first line, and the link's accessible name starts with it.
  await expect(card.locator(":scope > :first-child")).toHaveText(label);
  await expect(card.getByRole("link")).toHaveAccessibleName(
    message(language, "discovery.result.sponsored.name", { label, name: group.name, price: await price(page, group.best_offer.price) }),
  );

  // It sits at its position among the cards, and the organic cards are all still there, in their order.
  const position = Math.min(SLOT, first.groups.length + 1);
  const cards = page.locator("article");
  await expect(cards.nth(position - 1)).toHaveAttribute("data-testid", "sponsored-card");
  await expect(page.locator("article:not([data-sponsored])")).toHaveCount(first.groups.length);

  // 4.5:1 contrast for its text (axe's color-contrast rule, WCAG 1.4.3).
  const axe = await new AxeBuilder({ page }).include('[data-testid="sponsored-card"]').withRules(["color-contrast"]).analyze();
  expect(axe.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);

  // At twice the root font size (the largest Android font scale), the label is whole and inside the screen.
  await page.addStyleTag({ content: "html { font-size: 200% !important; }" });
  const labelLine = page.getByTestId("sponsored-label");
  await expect(labelLine).toBeVisible();
  const fits = await labelLine.evaluate((el) => {
    const r = el.getBoundingClientRect();
    return el.scrollWidth <= el.clientWidth + 1 && r.left >= 0 && r.right <= document.documentElement.clientWidth + 1;
  });
  expect(fits, "the label fits at the largest font size").toBe(true);
  await page.addStyleTag({ content: "html { font-size: 100% !important; }" });

  // Opening the card posts its token, for the pack's click and the session's attribution.
  const click = page.waitForRequest((r) => new URL(r.url()).pathname === "/api/search/sponsored-click" && r.method() === "POST");
  await card.getByRole("link").click();
  expect(JSON.parse((await click).postData() || "{}")).toEqual({ token: TOKEN });
});

test("a results page never shows more than one sponsored card", async ({ page, language }) => {
  const results = await withSponsored(page, { secondPage: true });
  await page.goto(`/search?q=${QUERY}`);
  await expect(page.getByTestId("sponsored-card")).toHaveCount(1);
  const organic = results()!.groups.length;
  await page.getByRole("button", { name: message(language, "discovery.search.showMore") }).click();
  await expect(page.locator("article:not([data-sponsored])")).toHaveCount(organic * 2);
  await expect(page.getByTestId("sponsored-card")).toHaveCount(1);
});

// ------------------------------------------------------------------------------------------------ the owner's pages

type Eligibility = Record<string, unknown> & { city: Record<string, unknown> };

const THRESHOLDS = { verified_shops: 100, monthly_customers: 3000, customer_weeks: 4, median_stock_age_hours: 48 };

function eligibility(eligible: boolean): Eligibility {
  const today = new Date().toISOString().slice(0, 10);
  return {
    can_buy: eligible,
    reasons: eligible ? [] : ["city_verified_shops"],
    sales_enabled: true,
    city: {
      city: "ballari",
      name: "Ballari",
      eligible,
      reasons: eligible ? [] : ["verified_shops"],
      as_of: today,
      verified_shops: eligible ? 128 : 64,
      median_stock_age_hours: 20.5,
      monthly_customers: 3400,
      lowest_monthly_customers: 3150,
      days_checked: 28,
      thresholds: THRESHOLDS,
    },
    label: "Sponsored",
    slot_position: SLOT,
    fresh_hours: 48,
    packs: [
      { kind: "standard", list_price_paise: 49_900, days: 30, max_groups: 3, discount_pct: 0, amount_paise: 49_900, plan_code: "free", plan_version: 1 },
      { kind: "festive", list_price_paise: 99_900, days: 30, max_groups: 3, discount_pct: 0, amount_paise: 99_900, plan_code: "free", plan_version: 1 },
    ],
  };
}

const DAY = 86_400_000;
const iso = (ms: number) => new Date(ms).toISOString();

function packs() {
  const now = Date.now();
  const day = (offset: number) => new Date(now - offset * DAY).toISOString().slice(0, 10);
  const base = {
    kind: "standard", groups: [{ type: "category", id: 1, name: "Umbrellas" }], days: 30, list_price_paise: 49_900,
    discount_pct: 0, amount_paise: 49_900, paused_at: null, pause_reason: null, ended_at: null, end_reason: null,
  };
  return {
    packs: [
      {
        ...base, id: 902, status: "active", payment_request_number: "NS-PR-000902", paid_at: iso(now - 5 * DAY),
        period_start: iso(now - 5 * DAY), ends_at: iso(now + 25 * DAY), make_good_used: false, paused_days_added: 0,
        period_requests: 7, totals: { impressions: 1234, clicks: 56, requests: 7 },
        by_day: [
          { day: day(1), impressions: 600, clicks: 30, requests: 4 },
          { day: day(0), impressions: 634, clicks: 26, requests: 3 },
        ],
      },
      {
        ...base, id: 901, status: "make_good", payment_request_number: "NS-PR-000901", paid_at: iso(now - 40 * DAY),
        period_start: iso(now - 40 * DAY), ends_at: iso(now + 20 * DAY), make_good_used: true, paused_days_added: 2,
        period_requests: 0, totals: { impressions: 800, clicks: 12, requests: 0 }, by_day: [],
      },
    ],
  };
}

async function serveBoost(page: Page, eligible: boolean) {
  await page.route("**/api/owner/boost/eligibility", (route) => route.fulfill({ json: eligibility(eligible) }));
  await page.route("**/api/owner/boost/packs", (route) =>
    route.request().method() === "GET" ? route.fulfill({ json: packs() }) : route.continue(),
  );
}

async function openMenu(page: Page, language: string) {
  await page.getByRole("button", { name: message(language, "shell.dashboard.openMenu") }).click();
  const menu = page.getByRole("dialog");
  await expect(menu.locator('a[href="/shop/settings"]')).toBeVisible();
  return menu;
}

test("the Boost page shows each pack's figures and make-good, and the menu offers it in an eligible city", async ({ page, language }) => {
  await signInOwner(page.request);
  await serveBoost(page, true);
  await page.goto("/shop/boost");
  await expect(page.getByRole("heading", { level: 1, name: message(language, "owner.boost.title") })).toBeVisible();
  await expect(page.getByTestId("boost-city")).toHaveAttribute("data-eligible", "true");
  await expect(page.getByTestId("boost-buy")).toBeVisible();

  const active = page.locator('[data-testid="boost-pack"][data-pack="902"]');
  await expect(active.getByTestId("boost-impressions")).toHaveText("1,234");
  await expect(active.getByTestId("boost-clicks")).toHaveText("56");
  await expect(active.getByTestId("boost-requests")).toHaveText("7");
  await expect(active.getByTestId("boost-make-good")).toContainText(message(language, "owner.boost.packs.makeGoodOpen", { count: "7" }));

  const makeGood = page.locator('[data-testid="boost-pack"][data-pack="901"]');
  await expect(makeGood).toHaveAttribute("data-status", "make_good");
  await expect(makeGood.getByTestId("boost-requests")).toHaveText("0");
  await expect(makeGood.getByTestId("boost-make-good")).toContainText(message(language, "owner.boost.packs.makeGoodRunning"));
  await expect(makeGood.getByTestId("boost-make-good")).toContainText(message(language, "owner.boost.packs.daysAdded", { days: "2" }));

  // The P2.10 rules on the page: no serious or critical axe violation (WCAG 2.2 A and AA) and no sideways scroll.
  const axe = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  expect(axe.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);

  const menu = await openMenu(page, language);
  await expect(menu.locator('a[href="/shop/boost"]')).toHaveText(message(language, "owner.shell.nav.boost"));
});

test("the owner menu hides Boost in an ineligible city", async ({ page, language }) => {
  await signInOwner(page.request);
  await serveBoost(page, false);
  const answered = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/owner/boost/eligibility");
  await page.goto("/shop");
  await answered;
  const menu = await openMenu(page, language);
  await expect(menu.locator('a[href="/shop/boost"]')).toHaveCount(0);
  await expect(page.locator('a[href="/shop/boost"]')).toHaveCount(0);
});
