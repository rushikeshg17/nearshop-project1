import fs from "node:fs";
import path from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { type APIRequestContext, expect, type Page, test as base } from "@playwright/test";

/**
 * The statement and demand-report screens (P9.11; backend P9.7 and P9.9, plan sections 8.18 and 8.19).
 *
 * 1. "Asked for near you, not in stock" on the insights page shows each item with its band ("5 to 9 people"), never
 *    an exact count: no number below 5 appears among the items, even when the response carries one.
 * 2. A Free shop sees its top 3 items and a line about Growth.
 * 3. A held report shows its reason instead of items.
 * 4. A statement card's counts are the counts the API sent, and the month so far on the statements page and the
 *    overview are the API's current counts.
 *
 * Runs against the seeded throwaway database of the frontend-quality CI job, signed in as the demo owner with
 * NEARSHOP_DEMO_PASSWORD. The seed has no statements (they are never built from demo data) and the demand report is
 * off until R5 switches it on, so the report and the statement list are served from page.route on top of the real
 * responses; the month so far, the rest of each page and the sign-in are real.
 */

type Options = { language: string };
const test = base.extend<Options>({ language: ["en", { option: true }] });

const OWNER = process.env.NEARSHOP_E2E_OWNER_EMAIL || "owner@nearshop.demo";
const CLIENT_HEADER = { "X-NearShop-Client": "web" };
const COUNT_FIELDS = ["reservation_requests", "pickups_completed", "delivery_orders", "call_taps", "directions_taps"] as const;
const BLOCKING = new Set(["serious", "critical"]);
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

async function signInOwner(request: APIRequestContext) {
  const password = process.env.NEARSHOP_DEMO_PASSWORD;
  if (!password) throw new Error("Set NEARSHOP_DEMO_PASSWORD to the seeded demo accounts' password.");
  const r = await request.post("/api/auth/login", { data: { email: OWNER, password }, headers: CLIENT_HEADER });
  expect(r.ok(), `sign in as ${OWNER}: HTTP ${r.status()}`).toBeTruthy();
}

type Gate = { shown: boolean; code: string | null; reason: string | null; progress: { label: string; have: number; need: number }[] };
type DemandItem = { query: string; item: { id: number; name: string | null } | null; category: string | null; band: string };
type Counts = Record<(typeof COUNT_FIELDS)[number], number> & Record<string, number | string | boolean>;

const SHOWN: Gate = { shown: true, code: null, reason: null, progress: [{ label: "% genuine", have: 90, need: 80 }] };

/** Serves the owner's insights with the missed-demand module replaced, keeping everything else as the API returns it. */
async function withDemandReport(page: Page, items: DemandItem[], report: { week: string | null; plan: string | null; hidden: number }, gate: Gate) {
  await page.route("**/api/owner/insights", async (route) => {
    const response = await route.fetch();
    const real = (await response.json()) as Record<string, unknown> & { gates?: Record<string, Gate> };
    await route.fulfill({
      response,
      json: { ...real, missed_demand: items, missed_demand_report: report, gates: { ...(real.gates ?? {}), missed_demand: gate } },
    });
  });
}

/** The zero of each script whose digits the interface may show: Latin, Devanagari, Telugu and Kannada. */
const ZEROS = [0x30, 0x966, 0xc66, 0xce6];

/** Every whole number in a text, in any of those scripts, by its value. */
function numbersIn(text: string) {
  const digit = (d: string) => {
    const cp = d.codePointAt(0)!;
    const zero = ZEROS.find((z) => cp >= z && cp <= z + 9);
    return zero === undefined ? "" : String(cp - zero);
  };
  return [...text.matchAll(/[0-9\u0966-\u096f\u0c66-\u0c6f\u0ce6-\u0cef]+/g)].map((m) => Number([...m[0]].map(digit).join("")));
}

const ITEMS: DemandItem[] = [
  // searches is what the old screen showed; the API no longer sends it, and the screen must not show it if it did.
  { query: "basmati rice", item: { id: 1, name: "Basmati Rice" }, category: "Groceries", band: "20+", searches: 3 } as DemandItem,
  { query: "umbrella", item: null, category: null, band: "10-19" },
  { query: "phenyl", item: null, category: null, band: "5-9" },
  { query: "ragi flour", item: null, category: null, band: "5-9" },
];
const BAND_KEYS: Record<string, string> = { "5-9": "low", "10-19": "mid", "20+": "high" };

test("demand items show a band and never a number below 5", async ({ page, language }) => {
  await signInOwner(page.request);
  await withDemandReport(page, ITEMS, { week: "2027-W23", plan: "full", hidden: 0 }, SHOWN);
  await page.goto("/shop/insights");
  const card = page.getByTestId("missed-demand");
  await expect(card.getByRole("heading", { name: message(language, "owner.insights.missed.title") })).toBeVisible();
  const rows = card.getByTestId("missed-items").getByRole("listitem");
  await expect(rows).toHaveCount(ITEMS.length);
  for (const [i, item] of ITEMS.entries()) {
    const row = rows.nth(i);
    await expect(row).toContainText(item.query, { ignoreCase: true });
    await expect(row).toContainText(message(language, `owner.insights.missed.band.${BAND_KEYS[item.band]}`));
    for (const n of numbersIn(await row.innerText())) expect(n, `a number in "${await row.innerText()}"`).toBeGreaterThanOrEqual(5);
  }
  // The week the report covers: the Monday of ISO week 23 of 2027 is 7 June 2027, shown as the page's language writes
  // that date.
  const week = card.getByTestId("missed-week");
  await expect(week).toHaveAttribute("data-start", "2027-06-07");
  const weekStart = await page.evaluate(() =>
    new Date(Date.UTC(2027, 5, 7)).toLocaleDateString(document.documentElement.lang, { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" }),
  );
  await expect(week).toHaveText(message(language, "owner.insights.missed.week", { date: weekStart }));
  await expect(card.getByTestId("missed-teaser")).toHaveCount(0);

  const results = await new AxeBuilder({ page }).include("[data-testid='missed-demand']").withTags(WCAG_TAGS).analyze();
  expect.soft(results.violations.filter((v) => BLOCKING.has(v.impact ?? "")).map((v) => `${v.id}: ${v.help}`)).toEqual([]);
});

test("a Free shop sees its top 3 items and a line about Growth", async ({ page, language }) => {
  await signInOwner(page.request);
  // The API gives a Free shop its top 3 (the teaser) and how many more Growth would show.
  await withDemandReport(page, ITEMS.slice(0, 3), { week: "2027-W23", plan: "teaser", hidden: 4 }, SHOWN);
  await page.goto("/shop/insights");
  const card = page.getByTestId("missed-demand");
  await expect(card.getByTestId("missed-items").getByRole("listitem")).toHaveCount(3);
  const teaser = card.getByTestId("missed-teaser");
  await expect(teaser).toContainText(message(language, "owner.insights.missed.teaser"));
  await expect(teaser).toContainText(message(language, "owner.insights.missed.hidden.other", { count: "4" }));
});

test("a held report shows its reason", async ({ page, language }) => {
  await signInOwner(page.request);
  const held: Gate = {
    shown: false,
    code: "audit_pending",
    reason: "The report is on hold while NearShop checks it: 2 of 4 weekly checks done",
    progress: [{ label: "weekly checks", have: 2, need: 4 }],
  };
  await withDemandReport(page, [], { week: null, plan: null, hidden: 0 }, held);
  await page.goto("/shop/insights");
  const card = page.getByTestId("missed-demand");
  const notice = card.getByTestId("gate-notice");
  await expect(notice).toHaveAttribute("data-code", "audit_pending");
  await expect(notice).toContainText(message(language, "owner.insights.missed.gate.auditPending", { have: "2", need: "4" }));
  await expect(card.getByTestId("missed-items")).toHaveCount(0);

  // A disabled report says so too.
  await page.unrouteAll({ behavior: "wait" });
  await withDemandReport(page, [], { week: null, plan: null, hidden: 0 }, { shown: false, code: "disabled", reason: "off", progress: [] });
  await page.reload();
  await expect(card.getByTestId("gate-notice")).toContainText(message(language, "owner.insights.missed.gate.disabled"));
});

type Delivery = "not_sent" | "queued" | "sent" | "delivered" | "failed" | "cancelled";

function statement(id: number, month: string, kind: "shadow_bill" | "roi", counts: Partial<Counts>, replies: unknown[], delivery: Delivery = "delivered", builtAt = `${month}-01T04:30:00Z`) {
  const full = { call_taps: 0, directions_taps: 0, reservation_requests: 0, reservations_confirmed: 0, pickups_completed: 0, delivery_orders: 0, deliveries_completed: 0, customer_requests: 0, ...counts };
  return {
    id,
    month,
    kind,
    counts: full,
    asked_for: kind === "shadow_bill" ? ["umbrella"] : [],
    plan_code: kind === "roi" ? "growth" : "free",
    price: kind === "roi" ? null : { advance_paid: false, founding: true, year_paise: 99_900, month_paise: 8_325 },
    pay_link: null,
    plan: kind === "roi" ? { code: "growth", name: "Growth", ends_at: "2028-07-31T18:30:00Z" } : null,
    guarantee: kind === "roi" ? { applies: true, customer_requests: 7, needed: 3, met: true, credit_months: 3 } : null,
    delivery,
    built_at: builtAt,
    text: kind === "roi"
      ? { month_shop: month, counts: "", plan: "Growth, paid until 31 Jul 2028", guarantee: "7 customer requests this quarter, 3 needed: met." }
      : { month_shop: month, counts: "", amount_due: "Amount due: ₹0. You are on the Free plan, and every stock tool stays free.", price: "Growth would cost ₹83 a month at the founding price (₹999 for the first year)." },
    replies,
  };
}

test("a statement card's counts match the API, and the month so far is the API's", async ({ page, language }) => {
  await signInOwner(page.request);
  // The newest statement went out on WhatsApp two days ago, so it can still be answered there (backend REPLY_WINDOW,
  // 35 days); the oldest was never sent on WhatsApp, so the screen must not offer a WhatsApp reply to it.
  const twoDaysAgo = new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString();
  const statements = [
    statement(3, "2027-09", "roi", { reservation_requests: 14, pickups_completed: 11, delivery_orders: 6, call_taps: 23, directions_taps: 17 }, [], "delivered", twoDaysAgo),
    statement(2, "2027-08", "shadow_bill", { reservation_requests: 9, pickups_completed: 7, delivery_orders: 2, call_taps: 31, directions_taps: 12 }, [
      { kind: "accurate", at: "2027-09-01T06:00:00Z", reason: null },
    ]),
    statement(1, "2027-07", "shadow_bill", { reservation_requests: 4, pickups_completed: 3, delivery_orders: 1, call_taps: 8, directions_taps: 5 }, [], "failed"),
  ];
  const seen: { current?: Counts } = {};
  await page.route("**/api/owner/statements", async (route) => {
    const response = await route.fetch();
    const real = (await response.json()) as { current: Counts };
    seen.current = real.current;
    await route.fulfill({ response, json: { ...real, statements } });
  });

  await page.goto("/shop/statements");
  await expect(page.getByTestId("statements-note")).toContainText(message(language, "owner.statements.note"));
  const cards = page.getByTestId("statement-card");
  await expect(cards).toHaveCount(statements.length);
  for (const [i, s] of statements.entries()) {
    const card = cards.nth(i);
    await expect(card).toHaveAttribute("data-month", s.month);
    for (const field of COUNT_FIELDS) {
      await expect(card.locator(`[data-count='${field}'] dd`), `${s.month} ${field}`).toHaveAttribute("data-value", String(s.counts[field]));
    }
  }
  // The ₹0-due line (Free) and the plan line (Growth), as sent; the reply recorded, or none.
  await expect(cards.nth(1).getByTestId("statement-price")).toContainText("₹0");
  await expect(cards.nth(0).getByTestId("statement-price")).toContainText("Growth, paid until 31 Jul 2028");
  await expect(cards.nth(1).getByTestId("statement-reply")).toContainText(message(language, "owner.statements.reply.kinds.accurate"));
  await expect(cards.nth(0).getByTestId("statement-reply")).toContainText(message(language, "owner.statements.reply.noneCanReply"));
  // No WhatsApp reply is offered for a statement that never went out on WhatsApp, or that is not the latest one.
  const unsent = cards.nth(2).getByTestId("statement-reply");
  await expect(unsent).toContainText(message(language, "owner.statements.reply.none"));
  await expect(unsent).not.toContainText(message(language, "owner.statements.reply.noneCanReply"));

  // This month so far: the API's current counts, on the statements page and on the overview.
  const current = seen.current;
  if (!current) throw new Error("the statements response was not seen");
  for (const where of ["/shop/statements", "/shop"]) {
    if (where !== "/shop/statements") await page.goto(where);
    const soFar = page.getByTestId("month-so-far");
    await expect(soFar).toBeVisible();
    for (const field of COUNT_FIELDS) {
      await expect(soFar.locator(`[data-count='${field}'] dd`), `${where} ${field}`).toHaveAttribute("data-value", String(current[field]));
    }
  }
  await expect(page.getByTestId("month-so-far").getByRole("link", { name: message(language, "owner.overview.monthSoFar.statements") })).toHaveAttribute(
    "href",
    "/shop/statements",
  );

  await page.goto("/shop/statements");
  await expect(cards).toHaveCount(statements.length);
  const results = await new AxeBuilder({ page }).include("main").withTags(WCAG_TAGS).analyze();
  expect.soft(results.violations.filter((v) => BLOCKING.has(v.impact ?? "")).map((v) => `${v.id}: ${v.help}`)).toEqual([]);
});
