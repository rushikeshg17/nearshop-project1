import crypto from "node:crypto";
import zlib from "node:zlib";

import { type APIRequestContext, type Browser, expect, type Page, test as base } from "@playwright/test";

/**
 * KYC submission and review screens (P3.4; rules from P3.3, plan section 8.1).
 *
 * 1. The seeded demo owner submits evidence from shop settings: a GSTIN with a wrong check character shows an inline
 *    error; the corrected one is sent with a storefront photo. An admin rejects it on the Verification page with a
 *    reason, the owner's banner shows the reason and a resubmit button, the owner submits again, the admin approves,
 *    and the owner's banner goes away while customers can open the shop page again.
 * 2. A shop within 30 days of verified_until shows the renewal banner with its date and a renew button, and one past
 *    verified_until says it is hidden until renewed.
 *
 * Runs against the seeded throwaway database of the frontend-quality CI job, with NEARSHOP_DEMO_PASSWORD.
 *
 * The first test needs an admin account, which the seed does not provide: admins sign in with a second factor, and
 * only database/admin_tasks.py create-admin enrols one. Create one in the throwaway database and pass
 *   NEARSHOP_E2E_ADMIN_EMAIL, NEARSHOP_E2E_ADMIN_PASSWORD and NEARSHOP_E2E_ADMIN_TOTP (the otpauth:// URI that
 *   create-admin prints, or its base32 secret);
 * without them the first test is skipped, with a note saying so.
 *
 * The demo owner's shop is verified in the seed and not yet due for renewal, so it cannot submit evidence: the first
 * test suspends it through the admin API first, and it ends verified again (for 12 months from the approval), as the
 * other specs need. The seed has no shop within 30 days of verified_until whose owner can sign in (only the demo
 * owner has the demo password, and its approval is a day old), so the second test serves the owner's verification
 * state from page.route with the dates moved; everything else on the page is real.
 */

type Options = { language: string };
const test = base.extend<Options>({ language: ["en", { option: true }] });

const OWNER = process.env.NEARSHOP_E2E_OWNER_EMAIL || "owner@nearshop.demo";
const ADMIN_EMAIL = process.env.NEARSHOP_E2E_ADMIN_EMAIL || "";
const ADMIN_PASSWORD = process.env.NEARSHOP_E2E_ADMIN_PASSWORD || "";
const ADMIN_TOTP = process.env.NEARSHOP_E2E_ADMIN_TOTP || "";
const CLIENT_HEADER = { "X-NearShop-Client": "web" };
const VALID_GSTIN = "27AAPFU0939F1ZV";
const BAD_GSTIN = "27AAPFU0939F1ZX"; // the same with a wrong check character
const DAY = 86_400_000;

type Verification = {
  status: string;
  can_submit: boolean;
  renewal_pending: boolean;
  verified_until: string | null;
  renewal_opens_at: string | null;
  rejection_reason: string | null;
};
type OwnerShop = { id: number; name: string; slug: string; verification: Verification };

async function signIn(request: APIRequestContext, email: string, password: string, code?: string) {
  const r = await request.post("/api/auth/login", { data: { email, password, ...(code ? { code } : {}) }, headers: CLIENT_HEADER });
  expect(r.ok(), `sign in as ${email}: HTTP ${r.status()}`).toBeTruthy();
}

async function signInOwner(request: APIRequestContext) {
  const password = process.env.NEARSHOP_DEMO_PASSWORD;
  if (!password) throw new Error("Set NEARSHOP_DEMO_PASSWORD to the seeded demo accounts' password.");
  await signIn(request, OWNER, password);
}

async function getJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const r = await request.get(path);
  expect(r.ok(), `GET ${path}: HTTP ${r.status()}`).toBeTruthy();
  return (await r.json()) as T;
}

async function post(request: APIRequestContext, path: string, body: unknown = {}) {
  const r = await request.post(path, { data: body, headers: CLIENT_HEADER });
  expect(r.ok(), `POST ${path}: HTTP ${r.status()} ${await r.text()}`).toBeTruthy();
}

/** The current RFC 6238 code (SHA-1, 6 digits, 30 seconds unless the otpauth URI says otherwise). */
function totp(secretOrUri: string, at = Date.now()) {
  let secret = secretOrUri;
  let digits = 6;
  let period = 30;
  if (secretOrUri.startsWith("otpauth://")) {
    const params = new URL(secretOrUri).searchParams;
    secret = params.get("secret") ?? "";
    digits = Number(params.get("digits") ?? 6);
    period = Number(params.get("period") ?? 30);
  }
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of secret.replace(/=+$/, "").toUpperCase()) bits += alphabet.indexOf(ch).toString(2).padStart(5, "0");
  const key = Buffer.from(bits.match(/.{8}/g)!.map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 1000 / period)));
  const mac = crypto.createHmac("sha1", key).update(counter).digest();
  const offset = mac[mac.length - 1] & 0xf;
  const value = (mac.readUInt32BE(offset) & 0x7fffffff) % 10 ** digits;
  return String(value).padStart(digits, "0");
}

/** A small solid-colour PNG, as a stand-in for a phone photo (the API decodes and re-encodes it, P2.7). */
function photo(rgb: [number, number, number], width = 64, height = 48) {
  const chunk = (type: string, data: Buffer) => {
    const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
    const out = Buffer.alloc(body.length + 8);
    out.writeUInt32BE(data.length, 0);
    body.copy(out, 4);
    out.writeUInt32BE(zlib.crc32(body), body.length + 4);
    return out;
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8; // bit depth
  header[9] = 2; // truecolour
  const row = Buffer.concat([Buffer.from([0]), Buffer.from(Array.from({ length: width }, () => rgb).flat())]);
  const pixels = zlib.deflateSync(Buffer.concat(Array.from({ length: height }, () => row)));
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", pixels),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/** Makes the owner's shop able to submit evidence: a pending renewal is rejected, a verified shop suspended. */
async function openForSubmission(admin: APIRequestContext, owner: APIRequestContext): Promise<OwnerShop> {
  let shop = await getJson<OwnerShop>(owner, "/api/owner/shop");
  if (shop.verification.can_submit) return shop;
  const reason = "End-to-end check: the demo shop is verified again from fresh evidence.";
  if (shop.verification.renewal_pending) await post(admin, `/api/admin/shops/${shop.id}/verification/reject`, { reason });
  shop = await getJson<OwnerShop>(owner, "/api/owner/shop");
  if (!shop.verification.can_submit && shop.verification.status === "verified") {
    await post(admin, `/api/admin/shops/${shop.id}/verification/suspend`, { reason });
    shop = await getJson<OwnerShop>(owner, "/api/owner/shop");
  }
  expect(shop.verification.can_submit, "the demo shop can submit evidence").toBe(true);
  return shop;
}

/** Fills in and sends the "Verify your shop" form, checking the inline GSTIN error on the way. */
async function submitEvidence(page: Page) {
  const section = page.locator("section#verify");
  await expect(section).toBeVisible();
  await section.getByRole("radio", { name: /^GSTIN/ }).click();
  const number = section.getByLabel("GSTIN number");
  await number.fill(BAD_GSTIN);
  await number.blur();
  await expect(section.getByText("That GSTIN's last character does not match")).toBeVisible();
  await expect(number).toHaveAttribute("aria-invalid", "true");
  await number.fill(VALID_GSTIN);
  await number.blur();
  await expect(section.getByText("That GSTIN's last character does not match")).toHaveCount(0);
  await section.getByLabel(/^Photo of the certificate/).setInputFiles({ name: "certificate.png", mimeType: "image/png", buffer: photo([230, 230, 220]) });
  await section.getByLabel("Storefront photo showing your signboard").setInputFiles({ name: "storefront.png", mimeType: "image/png", buffer: photo([200, 60, 40]) });
  await section.getByRole("button", { name: "Submit for verification" }).click();
}

async function adminContext(browser: Browser, baseURL: string | undefined) {
  const context = await browser.newContext({ baseURL });
  // Wait for a fresh time step if this one is about to end: a code is accepted once, and only near its own step.
  if (Date.now() % 30_000 > 27_000) await new Promise((r) => setTimeout(r, 3_500));
  await signIn(context.request, ADMIN_EMAIL, ADMIN_PASSWORD, totp(ADMIN_TOTP));
  return context;
}

test("an owner submits KYC evidence, an admin rejects and then approves it, and the banner follows", async ({ page, browser }, testInfo) => {
  test.skip(
    !ADMIN_EMAIL || !ADMIN_PASSWORD || !ADMIN_TOTP,
    "Needs an admin with a second factor: set NEARSHOP_E2E_ADMIN_EMAIL, NEARSHOP_E2E_ADMIN_PASSWORD and NEARSHOP_E2E_ADMIN_TOTP (see the comment at the top).",
  );
  test.setTimeout(120_000);
  const admin = await adminContext(browser, testInfo.project.use.baseURL);
  await signInOwner(page.request);
  const shop = await openForSubmission(admin.request, page.request);
  const banner = page.getByTestId("verification-banner");

  // 1. The owner submits; the banner says it is waiting, usually within 2 working days.
  await page.goto("/shop/settings");
  await expect(banner).toBeVisible();
  await submitEvidence(page);
  await expect(banner).toHaveAttribute("data-status", "submitted");
  await expect(banner).toContainText("usually within 2 working days");
  await expect(page.request.get(`/api/shops/${shop.slug}`).then((r) => r.status())).resolves.toBe(404);

  // 2. The admin finds it in the queue, opens the evidence links, and rejects it with a reason.
  const adminPage = await admin.newPage();
  await adminPage.goto("/admin/verification");
  const row = adminPage.locator(`[data-testid="verification-row"][data-shop-id="${shop.id}"]`);
  await expect(row).toBeVisible();
  await expect(row).toContainText(VALID_GSTIN);
  const storefront = await row.getByRole("link", { name: /^Storefront photo/ }).getAttribute("href");
  expect((await admin.request.get(storefront!)).status(), "the signed storefront link opens").toBe(200);
  const reason = "The signboard is not readable in the storefront photo. Please take it again in daylight.";
  await row.getByRole("button", { name: `Reject ${shop.name}` }).click();
  const dialog = adminPage.getByRole("dialog", { name: `Reject ${shop.name}?` });
  await dialog.getByLabel("Reason the owner will see").fill(reason);
  await dialog.getByRole("button", { name: "Reject" }).click();
  await expect(row).toHaveCount(0);

  // 3. The owner sees the reason and a resubmit button, and submits again.
  await page.goto("/shop");
  await expect(banner).toHaveAttribute("data-status", "rejected");
  await expect(banner).toContainText(reason);
  await banner.getByRole("link", { name: "Resubmit" }).click();
  await expect(page).toHaveURL(/\/shop\/settings#verify$/);
  await submitEvidence(page);
  await expect(banner).toHaveAttribute("data-status", "submitted");

  // 4. The admin records the phone call-back if needed, and approves.
  await adminPage.reload();
  await expect(row).toBeVisible();
  const callBack = row.getByRole("button", { name: "Record call-back" });
  if (await callBack.isVisible()) await callBack.click();
  const approve = row.getByRole("button", { name: `Approve ${shop.name}` });
  await expect(approve).toBeEnabled();
  await approve.click();
  await expect(row).toHaveCount(0);
  await admin.close();

  // 5. The owner's banner goes away (verified for 12 months, so no renewal yet), and customers see the shop again.
  await page.reload();
  await expect(page.getByTestId("verification-summary")).toContainText("Verified until");
  await expect(banner).toHaveCount(0);
  await expect(page.request.get(`/api/shops/${shop.slug}`).then((r) => r.status())).resolves.toBe(200);
});

/** Serves the owner's verification with the dates moved, keeping everything else as the API returns it. */
async function withVerificationDates(page: Page, verifiedUntil: number) {
  await page.route("**/api/owner/verification", async (route) => {
    const response = await route.fetch();
    const real = (await response.json()) as Verification;
    const opens = verifiedUntil - 30 * DAY;
    const inForce = verifiedUntil > Date.now();
    await route.fulfill({
      response,
      json: {
        ...real,
        status: "verified",
        rejection_reason: null,
        renewal_pending: false,
        verified_until: new Date(verifiedUntil).toISOString(),
        renewal_opens_at: inForce ? new Date(opens).toISOString() : null,
        can_submit: !inForce || Date.now() >= opens,
        visible_to_customers: inForce,
      },
    });
  });
}

test("a shop within 30 days of verified_until shows the renewal banner", async ({ page }) => {
  await signInOwner(page.request);
  const until = Date.now() + 20 * DAY;
  await withVerificationDates(page, until);
  await page.goto("/shop");
  const banner = page.getByTestId("verification-banner");
  await expect(banner).toHaveAttribute("data-status", "renew");
  const date = new Date(until).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" });
  await expect(banner).toContainText(`Renew your verification by ${date}`);
  await expect(banner.getByRole("link", { name: "Renew" })).toHaveAttribute("href", "/shop/settings#verify");
});

test("a shop past verified_until is told it is hidden until renewed", async ({ page }) => {
  await signInOwner(page.request);
  await withVerificationDates(page, Date.now() - 2 * DAY);
  await page.goto("/shop");
  const banner = page.getByTestId("verification-banner");
  await expect(banner).toHaveAttribute("data-status", "expired");
  await expect(banner).toContainText("hidden from customers until it is renewed");
});
