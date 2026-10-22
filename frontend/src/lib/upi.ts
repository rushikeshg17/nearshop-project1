import { useEffect, useState } from "react";

import type { MessageKey } from "@/lib/i18n";

/**
 * Paying the shop directly by UPI (P6.2, plan section 8.8): NearShop never holds the customer's money. The customer
 * pays the shop's own UPI ID from a upi://pay link or a QR code made in the browser, and the owner records how the
 * customer paid when completing or delivering (P6.1).
 *
 * The format rules mirror backend/app/models/core.py (validate_upi_id, validate_upi_payee_name), so the settings
 * form shows the same error inline before the API would refuse it. The errors are message keys (owner.settings.upi),
 * shown with UPI_LIMITS as their {min} and {max}.
 */

/** The cancellation and refund section of the terms (P3.1; its text comes from B9), linked wherever the policy is
 * shown (plan check 9.5). */
export const REFUND_POLICY_HREF = "/terms#cancellation-and-refunds";
/** Days within which the shop refunds a direct UPI payment for a cancelled order or reservation (shop agreement). */
export const UPI_REFUND_DAYS = 5;

// A handle, an at sign and a provider, 3 to 50 characters, compared in lower case: yourshop@okhdfcbank.
const UPI_ID_PATTERN = /^[a-z0-9][a-z0-9._-]*@[a-z][a-z0-9]*$/;
const UPI_ID_MIN = 3;
const UPI_ID_MAX = 50;
const PAYEE_MIN = 2;
const PAYEE_MAX = 60;

/** The lengths the error messages name. */
export const UPI_LIMITS = {
  upi_id: { min: UPI_ID_MIN, max: UPI_ID_MAX },
  upi_payee_name: { min: PAYEE_MIN, max: PAYEE_MAX },
} as const;

/** The UPI ID as the backend stores it: outer spaces removed, lower case. */
export function normalizeUpiId(value: string) {
  return value.trim().toLowerCase();
}

/** The payee name with runs of spaces collapsed, as the backend stores it. */
export function normalizePayeeName(value: string) {
  return value.split(/\s+/).filter(Boolean).join(" ");
}

/** Why a UPI ID is refused (a message key), or null when it is valid or empty (empty removes it). */
export function upiIdError(value: string): MessageKey | null {
  const upi = normalizeUpiId(value);
  if (!upi) return null;
  if (upi.length < UPI_ID_MIN || upi.length > UPI_ID_MAX || !UPI_ID_PATTERN.test(upi)) {
    return "owner.settings.upi.idError";
  }
  return null;
}

/** Why a payee name is refused (a message key), or null when it is valid or empty (empty means the shop's name is
 * used). */
export function payeeNameError(value: string): MessageKey | null {
  const name = normalizePayeeName(value);
  if (!name) return null;
  if (name.length < PAYEE_MIN || name.length > PAYEE_MAX) {
    return "owner.settings.upi.payeeError";
  }
  return null;
}

/**
 * The UPI deep link (NPCI's upi://pay format) that opens the customer's UPI app on a payment to the shop:
 * pa the shop's UPI ID, pn the payee name, am the amount in rupees with two decimals, cu INR, and tn the note,
 * the order or reservation code, so the owner can match the payment. The at sign stays literal, as some UPI apps
 * do not decode it in pa.
 */
export function upiPaymentLink({ upiId, payeeName, amount, note }: { upiId: string; payeeName: string; amount: number; note: string }) {
  const params: [string, string][] = [
    ["pa", normalizeUpiId(upiId)],
    ["pn", normalizePayeeName(payeeName)],
    ["am", amount.toFixed(2)],
    ["cu", "INR"],
    ["tn", note],
  ];
  return `upi://pay?${params.map(([k, v]) => `${k}=${encodeURIComponent(v).replace(/%40/g, "@")}`).join("&")}`;
}

export type QrPath = { path: string; size: number };

/**
 * The QR code for `link`, as an SVG path over a size by size grid of modules, or null until it is ready. The QR
 * library (lean-qr) is imported here on demand, so it is a separate chunk that only the order and reservation pages
 * load, and only once they show a payment block: it is never first-load JavaScript (P2.9 budgets).
 */
export function useUpiQr(link: string | null): QrPath | null {
  const [qr, setQr] = useState<(QrPath & { link: string }) | null>(null);
  useEffect(() => {
    if (!link) return;
    let live = true;
    Promise.all([import("lean-qr"), import("lean-qr/extras/svg")])
      .then(([{ generate, correction }, { toSvgPath }]) => {
        const code = generate(link, { minCorrectionLevel: correction.M });
        if (live) setQr({ link, path: toSvgPath(code), size: code.size });
      })
      .catch(() => {
        // The link still works without the picture; nothing to tell the customer.
      });
    return () => {
      live = false;
    };
  }, [link]);
  return qr && qr.link === link ? { path: qr.path, size: qr.size } : null;
}
