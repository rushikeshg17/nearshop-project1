import fs from "node:fs";
import path from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, type Route, test as base } from "@playwright/test";

/**
 * Plan and billing screens (P9.10; backend P9.4 to P9.6, plan section 8.18).
 *
 * 1. The pay block for a Growth year shows NearShop's payee name, the amount and the payment request number as the
 *    note, in its upi://pay link and beside the QR code, and /shop/billing has no serious or critical axe violation.
 * 2. The list of tools free on every plan (plan section 8.17) is visible on Free and on Growth.
 * 3. A lapsed shop's page shows Free and a renew action.
 * 4. The document page shows its number and the draft line, reads in full at 360 px, and fits an A4 page's printable
 *    width when printed.
 * 5. An admin can match a planted bank credit by hand: the request carries the credit's UPI reference, the request
 *    number and the reason, and the credit leaves the unmatched queue.
 *
 * The owner tests sign in as the seeded demo owner with NEARSHOP_DEMO_PASSWORD, so the console around the page is
 * real. One test reads the real API unmocked, so the page and the shapes copied below stay in step with it. In the
 * others the billing API itself is served from page.route: the frontend-quality CI job runs the API without NearShop's
 * UPI ID and payee name (payment requests answer 503 until they are set, docs/runbooks/billing.md) and before
 * billing starts (1 Aug 2027), and its database holds no payments, documents or credits to show. The backend's own
 * rules are tested in backend/tests (test_plans.py, test_billing.py, test_autopay.py). The admin test also serves
 * the signed-in user from page.route, as an admin: admins sign in with a second factor the seed does not enrol (see
 * kyc-verification.spec.ts). It signs in as the demo owner only so that a session cookie exists (src/proxy.ts sends
 * a request without one to sign-in); every admin API call the page makes is served by the test.
 */

type Options = { language: string };
const test = base.extend<Options>({ language: ["en", { option: true }] });

const OWNER = process.env.NEARSHOP_E2E_OWNER_EMAIL || "owner@nearshop.demo";
const CLIENT_HEADER = { "X-NearShop-Client": "web" };
const BLOCKING = new Set(["serious", "critical"]);
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22a", "wcag22aa"];
const DAY = 86_400_000;
// A test UPI ID and payee, never NearShop's real account (those are settings, B22).
const PAYEE = "NearShop Test Payee";
const UPI_ID = "nearshop.e2e@okaxis";
const REQUEST_NUMBER = "NSP000042";
const A4_PRINTABLE_PX = Math.floor(((210 - 2 * 14) / 25.4) * 96); // A4 width less the page's 14 mm margins, at 96 dpi

/** The interface text for a message key in the project's language, falling back to English per key as the site does
 * (src/lib/i18n.ts), with {name} placeholders filled in. */
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

async function signInOwner(page: Page) {
  const password = process.env.NEARSHOP_DEMO_PASSWORD;
  if (!password) throw new Error("Set NEARSHOP_DEMO_PASSWORD to the seeded demo accounts' password.");
  const r = await page.request.post("/api/auth/login", { data: { email: OWNER, password }, headers: CLIENT_HEADER });
  expect(r.ok(), `sign in as ${OWNER}: HTTP ${r.status()}`).toBeTruthy();
}

// ------------------------------------------------------------------ the API's answers (shapes from backend/app/services)

const iso = (ms: number) => new Date(ms).toISOString();

const FREE_ENTITLEMENTS = {
  demand_report: "teaser", low_stock_alerts: false, logins: 1, roi_statement: false, priority_support: false,
  boost_discount_pct: 0, outcome_guarantee: false, pro_insights: false, outlets: 1,
};
const GROWTH_ENTITLEMENTS = {
  demand_report: "full", low_stock_alerts: true, logins: 3, roi_statement: true, priority_support: true,
  boost_discount_pct: 25, outcome_guarantee: true, pro_insights: false, outlets: 1,
};

const growthPlan = {
  id: 2, code: "growth", version: 1, name: "Growth", price_year_paise: 149_900, price_month_paise: 14_900,
  founding_year_paise: 99_900, on_sale: true, entitlements: GROWTH_ENTITLEMENTS, effective_from: iso(Date.UTC(2026, 0, 1)),
  published_at: iso(Date.UTC(2026, 0, 1)), reason: "Catalogue version 1",
};

function subscription(now: number) {
  return {
    id: 7, account_type: "shop_owner", account_id: 2, shop_ids: [1], plan_code: "growth", plan_version: 1, period: "year",
    price_paise: 99_900, founding: true, founding_slot: 12, source: "shop_paid", status: "active",
    starts_at: iso(now - 100 * DAY), ends_at: iso(now + 265 * DAY), paid_until: iso(now + 265 * DAY),
    paid_at: iso(now - 100 * DAY), created_at: iso(now - 100 * DAY), updated_at: iso(now - 100 * DAY),
  };
}

function ownerPlan(kind: "free" | "growth" | "lapsed", now = Date.now()) {
  const growth = kind === "growth";
  const sub = growth ? subscription(now) : null;
  return {
    plan: {
      code: growth ? "growth" : "free", name: growth ? "Growth" : "Free", version: 1,
      entitlements: growth ? GROWTH_ENTITLEMENTS : FREE_ENTITLEMENTS, subscription_id: sub?.id ?? null,
      status: growth ? "active" : "free", source: sub?.source ?? null, starts_at: sub?.starts_at ?? null, ends_at: sub?.ends_at ?? null,
    },
    subscription: sub,
    advance: null,
    founding: { eligible: kind === "free", in_city: true, already_claimed: kind !== "free", remaining: 88, cap: 100, price_year_paise: 99_900 },
    guarantee: growth
      ? { applies: true, quarter_start: iso(now - 10 * DAY), quarter_end: iso(now + 80 * DAY), customer_requests: 2, needed: 3, met: false, credit_months: 3 }
      : { applies: false, needed: 3 },
    billing_starts_on: "2027-08-01",
    on_sale: [growthPlan],
  };
}

function paymentRequest() {
  return {
    id: 42, number: REQUEST_NUMBER, shop_id: 1, purpose: "plan", plan_code: "growth", plan_version: 1, period: "year",
    kind: "new", founding: true, amount_paise: 99_900, gst_mode: "unregistered", gst_paise: 0, total_paise: 99_900,
    status: "open", created_at: iso(Date.now()), payment_id: null, created_by: "owner", reason: null,
    pay_to: { upi_id: UPI_ID, payee_name: PAYEE, amount: "999.00", note: REQUEST_NUMBER },
  };
}

// The number in the backend's format, {prefix}/{yy-yy}/{seq:06d} (billing.next_number), slashes and all.
const DOCUMENT = {
  id: 501, number: "NS/26-27/000012", financial_year: "2026-27", seq: 12, type: "receipt", issued_at: iso(Date.now() - 300 * DAY),
  payment_date: iso(Date.now() - 300 * DAY), format: "draft", draft_line: "Draft format, not for use",
  wording: "[The CA's approved wording (B22) goes here]",
  supplier: { legal_name: "NearShop Test Supplier Private Limited", address: "1 Test Road, Ballari, Karnataka 583101", state_code: "29", gstin: null },
  recipient: { legal_name: "Test Kirana and General Stores", address: "22 Long Market Street, Near the Clock Tower, Ballari, Karnataka 583101", state_code: "29", gstin: null },
  place_of_supply: "29 Karnataka", description: "NearShop Growth plan, 1 year (founding offer)", sac: null,
  taxable_paise: 99_900, gst_rate_pct: 0, cgst_paise: 0, sgst_paise: 0, igst_paise: 0, total_paise: 99_900,
  payment_id: 31, request_number: "NSP000031", upi_reference: "512345678901", original_document_id: null, original_number: null,
  refund_reference: null, content_hash: "0".repeat(64), intact: true,
};

function payment(now: number) {
  return {
    id: 31, request_id: 31, request_number: "NSP000031", shop_id: 1, credit_id: 9, upi_reference: "512345678901",
    amount_paise: 99_900, paid_at: iso(now - 400 * DAY), matched_at: iso(now - 399 * DAY), matched_by: { type: "admin", id: 1 },
    match: "statement", reason: "Bank statement march.csv", subscription_id: 7, document_id: DOCUMENT.id, status: "matched",
    refund: null, settled_at: null, settlement_id: null,
    documents: [{ id: DOCUMENT.id, number: DOCUMENT.number, type: "receipt", issued_at: DOCUMENT.issued_at, total_paise: 99_900, draft: true }],
  };
}

function ownerBilling(state: { requests?: unknown[]; payments?: unknown[] } = {}) {
  return {
    configured: true, account: null, gst_mode: "unregistered", requests: state.requests ?? [], payments: state.payments ?? [],
    autopay: { available: false, price_month_paise: 14_900, mandate: null },
  };
}

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

/** Serves the owner's plan and billing from these answers; a POST to make a payment request opens one. */
async function serveBilling(page: Page, plan: unknown, billing: ReturnType<typeof ownerBilling>) {
  const state = { billing };
  await page.route(/\/api\/owner\/plan(\?.*)?$/, (route) => json(route, plan));
  await page.route(/\/api\/owner\/billing(\?.*)?$/, (route) => json(route, state.billing));
  await page.route(/\/api\/owner\/billing\/requests$/, async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({ plan_code: "growth", period: "year" });
    const req = paymentRequest();
    state.billing = { ...state.billing, requests: [req] };
    await json(route, req);
  });
  await page.route(/\/api\/owner\/billing\/documents\/\d+$/, (route) =>
    route.request().url().endsWith(`/${DOCUMENT.id}`) ? json(route, DOCUMENT) : json(route, { error: { code: "not_found", message: "Document not found" } }, 404));
}

async function audit(page: Page, what: string) {
  const results = await new AxeBuilder({ page }).include("main").withTags(WCAG_TAGS).analyze();
  const blocking = results.violations.filter((v) => BLOCKING.has(v.impact ?? ""));
  expect.soft(blocking.map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(" ")).join("; ")})`), `accessibility of ${what}`).toEqual([]);
}

// ------------------------------------------------------------------ owner

test("the pay block shows NearShop's payee name, the amount and the request number as the note", async ({ page, language }) => {
  await signInOwner(page);
  await serveBilling(page, ownerPlan("free"), ownerBilling());
  await page.goto("/shop/billing");
  await expect(page.getByTestId("plan-name")).toHaveText("Free");
  await audit(page, "/shop/billing on Free");

  await page.getByTestId("request-payment").click();
  const block = page.getByTestId("pay-block");
  await expect(block).toBeVisible();
  await expect(block.getByTestId("pay-payee")).toHaveText(PAYEE);
  await expect(block.getByTestId("pay-amount")).toContainText("999");
  await expect(block.getByTestId("pay-note")).toHaveText(REQUEST_NUMBER);
  await expect(block).toContainText(message(language, "owner.billing.block.body", { price: "₹999", payee: PAYEE }));
  const href = await block.getByTestId("pay-link").getAttribute("href");
  expect(href).toMatch(/^upi:\/\/pay\?/);
  const link = new URL(href!);
  expect(link.searchParams.get("pa")).toBe(UPI_ID);
  expect(link.searchParams.get("pn")).toBe(PAYEE);
  expect(link.searchParams.get("am")).toBe("999.00");
  expect(link.searchParams.get("cu")).toBe("INR");
  expect(link.searchParams.get("tn")).toBe(REQUEST_NUMBER);
  await expect(block.getByTestId("pay-qr")).toBeVisible();
  await audit(page, "/shop/billing with the pay block");
});

for (const kind of ["free", "growth"] as const) {
  test(`the free-tools list is visible on ${kind === "free" ? "Free" : "Growth"}`, async ({ page, language }) => {
    await signInOwner(page);
    await serveBilling(page, ownerPlan(kind), ownerBilling(kind === "growth" ? { payments: [payment(Date.now())] } : {}));
    await page.goto("/shop/billing");
    await expect(page.getByTestId("plan-name")).toHaveText(kind === "free" ? "Free" : "Growth");
    const tools = page.getByTestId("free-tools");
    await expect(tools).toBeVisible();
    await expect(tools.getByRole("heading", { name: message(language, "owner.billing.freeTools.heading") })).toBeVisible();
    for (const key of ["listings", "import", "whatsapp", "alerts", "standee", "trust", "statement"]) {
      await expect(tools).toContainText(message(language, `owner.billing.freeTools.${key}`));
    }
    if (kind === "growth") await audit(page, "/shop/billing on Growth");
  });
}

test("a lapsed shop's page shows Free and a renew action", async ({ page, language }) => {
  await signInOwner(page);
  await serveBilling(page, ownerPlan("lapsed"), ownerBilling({ payments: [payment(Date.now())] }));
  await page.goto("/shop/billing");
  const current = page.getByTestId("current-plan");
  await expect(current).toHaveAttribute("data-status", "lapsed");
  await expect(page.getByTestId("plan-name")).toHaveText("Free");
  await expect(page.getByTestId("lapsed-note")).toHaveText(message(language, "owner.billing.current.lapsed"));
  await expect(page.getByTestId("request-payment")).toHaveText(message(language, "owner.billing.current.renew"));
  await expect(page.getByTestId("free-tools")).toBeVisible();
});

test("against the real API, the seeded owner's page shows their plan and the free tools", async ({ page, language }) => {
  // Nothing served from page.route here: the shapes the mocks above copy must still be what the API sends.
  await signInOwner(page);
  const [plan, billing] = await Promise.all(["/api/owner/plan", "/api/owner/billing"].map(async (url) => {
    const r = await page.request.get(url, { headers: CLIENT_HEADER });
    expect(r.ok(), `${url}: HTTP ${r.status()}`).toBeTruthy();
    return r.json();
  }));
  await page.goto("/shop/billing");
  await expect(page.getByTestId("current-plan")).toHaveAttribute("data-plan", plan.plan.code);
  await expect(page.getByTestId("plan-name")).toHaveText(plan.plan.name);
  await expect(page.getByTestId("free-tools")).toContainText(message(language, "owner.billing.freeTools.listings"));
  if (!billing.configured && (await page.getByTestId("pay-section").count()) > 0) {
    await expect(page.getByTestId("pay-section")).toContainText(message(language, "owner.billing.pay.notOpen"));
  }
  await audit(page, "/shop/billing from the real API");
});

test("the document page shows its number, fits 360 px and an A4 page", async ({ page, language }) => {
  await signInOwner(page);
  await serveBilling(page, ownerPlan("growth"), ownerBilling({ payments: [payment(Date.now())] }));
  await page.goto("/shop/billing");
  await page.getByRole("link", { name: `${message(language, "owner.billing.docTypes.receipt")} ${DOCUMENT.number}` }).click();
  await expect(page).toHaveURL(new RegExp(`/shop/billing/documents/${DOCUMENT.id}$`));
  await expect(page.getByTestId("document-number")).toHaveText(DOCUMENT.number);
  await expect(page.getByTestId("draft-line")).toHaveText(DOCUMENT.draft_line);
  await expect(page.getByTestId("document-total")).toContainText("999");
  await audit(page, "a billing document");

  // In full at 360 px: nothing in the document is wider than the screen.
  const overflow = () => page.locator("#billing-document").evaluate((el) => {
    const box = el.getBoundingClientRect();
    const wide = [...el.querySelectorAll("*")].filter((n) => n.getBoundingClientRect().right > box.right + 0.5 || n.scrollWidth > n.clientWidth + 1);
    return { right: box.right, page: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth, wide: wide.length };
  });
  let o = await overflow();
  expect(o.right).toBeLessThanOrEqual(o.page);
  expect(o.scroll).toBeLessThanOrEqual(o.page);
  expect(o.wide).toBe(0);

  // Printed on A4: only the document, within the printable width, nothing clipped.
  await page.emulateMedia({ media: "print" });
  await page.setViewportSize({ width: A4_PRINTABLE_PX, height: 1000 });
  await expect(page.getByRole("button", { name: message(language, "owner.billing.document.print") })).toBeHidden();
  o = await overflow();
  expect(o.right).toBeLessThanOrEqual(A4_PRINTABLE_PX);
  expect(o.wide).toBe(0);
  const pdf = await page.pdf({ format: "A4", printBackground: true });
  expect(pdf.byteLength).toBeGreaterThan(1000);
});

// ------------------------------------------------------------------ admin

test("an admin can match a planted credit by hand", async ({ page, language }) => {
  test.skip(language !== "en", "The admin console is in English only (plan section 8.16).");
  const credit = {
    id: 9, upi_reference: "598765432109", credited_on: "2027-06-03", amount_paise: 99_900, narration: "UPI/598765432109/payment for plan",
    source: "statement", statement_id: 3, request_number: null, status: "unmatched", note: "No payment request number in the narration",
    payment_id: null, alerted_at: null, returned: null, closed: null, created_at: iso(Date.now() - 2 * DAY),
  };
  let queue = [credit];
  let matched: Record<string, unknown> | null = null;
  await page.route(/\/api\/auth\/me(\?.*)?$/, (route) => json(route, {
    id: 1, name: "Test Admin", email: "admin@example.invalid", phone: null, role: "admin", home_lat: null, home_lng: null,
    home_label: null, shop: null, phone_verified: false, email_login_sunset: "2100-01-01", phone_migration: null,
  }));
  await page.route(/\/api\/admin\/billing\/credits(\?.*)?$/, (route) => json(route, queue));
  await page.route(/\/api\/admin\/billing\/credits\/match$/, async (route) => {
    matched = route.request().postDataJSON();
    queue = [];
    await json(route, {
      ...payment(Date.now()), id: 77, credit_id: credit.id, upi_reference: credit.upi_reference, request_number: REQUEST_NUMBER,
      match: "manual", reason: matched?.reason, documents: undefined,
    });
  });
  await page.route(/\/api\/admin\/(billing\/(requests|payments)|subscriptions)(\?.*)?$/, (route) => json(route, []));
  await page.route(/\/api\/admin\/founding$/, (route) => json(route, { cap: 100, taken: 12, remaining: 88, cities: ["ballari", "bellary"] }));
  await page.route(/\/api\/admin\/(overview|verification|reviews\/queue)(\?.*)?$/, (route) => json(route, { error: { code: "test", message: "Not served in this test" } }, 503));

  await signInOwner(page); // for the session cookie only (see the top of this file)
  await page.goto("/admin/billing");
  const item = page.getByRole("listitem", { name: `Credit ${credit.upi_reference}` });
  await expect(item).toBeVisible();
  await expect(page.getByTestId("founding-counter")).toContainText("88");
  await item.getByRole("button", { name: "Match by hand" }).click();
  const form = page.getByRole("form", { name: "Match a credit by hand" });
  await expect(form.getByLabel("UPI reference")).toHaveValue(credit.upi_reference);
  await form.getByLabel("Payment request number").fill(REQUEST_NUMBER);
  const match = form.getByRole("button", { name: "Match", exact: true });
  await expect(match).toBeDisabled(); // no reason yet
  await form.getByLabel("Reason").fill("Owner wrote the request number in the wrong field");
  await match.click();
  await expect(page.getByTestId("unmatched-empty")).toBeVisible();
  expect(matched).toEqual({ upi_reference: credit.upi_reference, request_number: REQUEST_NUMBER, reason: "Owner wrote the request number in the wrong field" });
  await audit(page, "/admin/billing");
});
