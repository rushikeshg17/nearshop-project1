// Thin fetch wrapper for the NearShop API (proxied at /api by next.config.ts).

import type { Messages, Translate } from "@/lib/i18n";

export class ApiError extends Error {
  status: number;
  code: string;
  field?: string;

  constructor(message: string, status: number, code = "error", field?: string) {
    super(message);
    this.status = status;
    this.code = code;
    this.field = field;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

function withQuery(path: string, query?: Query) {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

async function request<T>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  const isForm = typeof FormData !== "undefined" && body instanceof FormData;
  let res: Response;
  try {
    res = await fetch(withQuery(`/api${path}`, query), {
      method,
      credentials: "same-origin",
      headers: {
        // Required by the API on state-changing requests (CSRF defence in depth).
        "X-NearShop-Client": "web",
        ...(body !== undefined && !isForm ? { "Content-Type": "application/json" } : {}),
      },
      body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("Can't reach NearShop right now. Check your connection and try again.", 0, "network");
  }

  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const err = data?.error;
    const detail = typeof data?.detail === "string" ? data.detail : undefined;
    // Without a message from the API (a proxy's or the platform's error page), the code says which fallback it is:
    // server_error or request_failed, both translated by errorMessage below.
    const fallback = res.status >= 500 ? "server_error" : "request_failed";
    throw new ApiError(
      err?.message ?? detail ?? (res.status >= 500 ? "Something went wrong on our side. Please try again." : "Request failed"),
      res.status,
      err?.code ?? (res.status === 401 ? "unauthorized" : err?.message || detail ? "error" : fallback),
      err?.field,
    );
  }
  return data as T;
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>("GET", path, undefined, query),
  post: <T>(path: string, body?: unknown, query?: Query) => request<T>("POST", path, body ?? {}, query),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body ?? {}),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body ?? {}),
  delete: <T>(path: string) => request<T>("DELETE", path),
  upload: <T>(path: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<T>("POST", path, form);
  },
};

export type LeadKind = "call_tap" | "directions_tap";

/**
 * Records a tap on a shop's Call or Directions link (P9.2), just before the browser follows it. Fire and forget:
 * fetch with keepalive still completes when the tap opens the dialler or the map (a beacon could not send the
 * X-NearShop-Client header the API requires), and a failed, slow or refused call never blocks or delays the link.
 * No cookies are sent: nothing ties the tap to an account.
 */
export function recordLead(shopId: number, kind: LeadKind, productId?: number): void {
  try {
    fetch("/api/leads", {
      method: "POST",
      keepalive: true,
      credentials: "omit",
      headers: { "X-NearShop-Client": "web", "Content-Type": "application/json" },
      body: JSON.stringify(productId === undefined ? { shop_id: shopId, kind } : { shop_id: shopId, kind, product_id: productId }),
    }).catch(() => undefined);
  } catch {
    // Never let counting get in the way of the link.
  }
}

// ------------------------------------------------------------------------------------------------ error text (P7.10)
//
// The API answers an error with a stable code and an English message. errorMessage shows the message in the interface
// language when the code is one of TRANSLATED_ERRORS (common.apiErrors in src/messages), and the API's own text for
// any other code, so a new error is never hidden. A code goes on the list only when its English message is fixed text:
// a message that carries details (a count, a limit, a date) keeps the API's text, which has them. The one exception
// is insufficient_stock: its details (how many are left, and of which product in an order) are read from the API's
// text by stockMessage below and put into a translated message (common.stockErrors), so no language loses them.
// The admin console stays in English (plan section 8.16), so its errors keep the API's text too.

type TranslatedError = keyof Messages["common"]["apiErrors"];

const TRANSLATED_ERRORS: ReadonlySet<string> = new Set<TranslatedError>([
  // made here, for a request that got no answer or an answer without the API's error body
  "network",
  "server_error",
  "request_failed",
  "unauthorized",
  // from the API
  "rate_limited",
  "signups_closed",
  "shop_not_verified",
  "consent_required",
  "age_confirmation_required",
  "insufficient_stock",
  "duplicate_reservation",
  "invalid_credentials",
  "email_taken",
  "otp_expired",
  "otp_locked",
  "otp_send_failed",
  "otp_unavailable",
  "phone_signin_required",
  "account_inactive",
  "duplicate_review",
  "review_not_allowed",
  "own_review",
]);

let translate: Translate | null = null;
let translateLanguage: string | null = null;

/** Called by the interface-language provider (src/components/providers.tsx) with a translator for the page's
 * language, whose messages always include the common area; null until then, and errors show the API's text. */
export function setErrorTranslator(t: Translate | null, language: string | null = null) {
  translate = t;
  translateLanguage = t ? language : null;
}

function translating(): Translate | null {
  if (!translate) return null;
  if (typeof window !== "undefined" && /^\/admin(\/|$)/.test(window.location.pathname)) return null;
  return translate;
}

/** An insufficient_stock error in the interface language, with the count and the product the API's text names. The
 * API's English forms (backend/app/services: reservations.py, orders.py, inventory.py) are "This item is out of
 * stock", "Only {n} in stock", "Only {n} of {product} in stock" and "Only {n} of {product} left in stock". A form not
 * recognised here gets the general message, or the API's own text where that is the language anyway (English). */
function stockMessage(t: Translate, message: string): string {
  const text = message.trim().replace(/\.$/, "");
  if (/^This item is out of stock$/i.test(text)) return t("common.stockErrors.outOfStock");
  let m = /^Only (\d+) in stock$/i.exec(text);
  if (m) return Number(m[1]) === 0 ? t("common.stockErrors.outOfStock") : t("common.stockErrors.onlyLeft", { count: m[1] });
  m = /^Only (\d+) of (.+?) (?:left )?in stock$/i.exec(text);
  if (m) {
    return Number(m[1]) === 0
      ? t("common.stockErrors.outOfStockOf", { product: m[2] })
      : t("common.stockErrors.onlyLeftOf", { count: m[1], product: m[2] });
  }
  return message && translateLanguage === "en" ? message : t("common.apiErrors.insufficient_stock");
}

/** The message to show for an error: translated for a known API error code, else the API's (or the error's) text. */
export function errorMessage(error: unknown): string {
  const t = translating();
  if (error instanceof ApiError) {
    if (t && error.code === "insufficient_stock") return stockMessage(t, error.message);
    return t && TRANSLATED_ERRORS.has(error.code) ? t(`common.apiErrors.${error.code as TranslatedError}`) : error.message;
  }
  if (error instanceof Error) return error.message;
  return t ? t("common.apiErrors.unknown") : "Something went wrong";
}
