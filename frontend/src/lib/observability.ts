/**
 * Error-tracking settings shared by src/instrumentation.ts (server and edge) and
 * src/instrumentation-client.ts (browser), the same rules as the API's app/core/observability.py.
 *
 * Error tracking is on only when NEXT_PUBLIC_SENTRY_DSN is set when the site is built (next.config.ts
 * inlines it, with the environment and the release). Events never carry cookies, the authorization
 * header, request bodies or the user, and anything that looks like an Indian mobile number, an email
 * address or a street address is masked wherever it appears. Traces: 10% in general and 25% on search.
 */
import type * as Sentry from "@sentry/nextjs";

type InitOptions = NonNullable<Parameters<typeof Sentry.init>[0]>;
type TracesSamplerSamplingContext = Parameters<NonNullable<InitOptions["tracesSampler"]>>[0];

export const SENTRY_DSN = process.env.NEXT_PUBLIC_SENTRY_DSN || undefined;
export const SENTRY_ENVIRONMENT = process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT || "development";
export const SENTRY_RELEASE = process.env.NEXT_PUBLIC_SENTRY_RELEASE || undefined;

export const TRACES_SAMPLE_RATE = 0.1;
export const SEARCH_TRACES_SAMPLE_RATE = 0.25;

/** A pattern that matches word in any letter case, for use inside a case-sensitive regular expression. */
function anyCase(word: string): string {
  return [...word].map((c) => (/[a-z]/i.test(c) ? `[${c.toUpperCase()}${c.toLowerCase()}]` : c)).join("");
}

const STREET_WORDS = [
  "road", "rd", "street", "st", "lane", "ln", "marg", "nagar", "layout", "colony", "galli", "gali", "circle",
  "avenue", "ave", "extension", "extn", "bazaar", "bazar", "market", "cross", "main", "block", "sector", "stage",
  "phase", "halli", "pete", "pet",
].map(anyCase).join("|");
const ORDINAL_WORDS = ["main", "cross", "block", "stage", "phase", "sector"].map(anyCase).join("|");

// A 10-digit Indian mobile number (6-9 first), with an optional +91, 91 or 0 prefix, not inside a longer
// word or number (ids, hashes, timestamps).
const PHONE = /(?<![\w+])(?:\+91[\s-]?|91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\w)/g;
const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}/g;
// A house number, up to four capitalised words and a street word ("12 MG Road"), or "5th Main".
const ADDRESS = new RegExp(
  String.raw`(?:#\s?)?\b\d{1,5}[A-Za-z]?(?:[/-]\d{1,5}[A-Za-z]?)?,?\s+(?:[A-Z][\w.'-]*,?\s+){0,4}(?:${STREET_WORDS})\b\.?` +
    String.raw`|\b\d{1,3}(?:st|nd|rd|th)\s+(?:${ORDINAL_WORDS})\b`,
  "g",
);

// Values Sentry needs intact; they never hold personal data.
const ID_KEYS = new Set(["event_id", "trace_id", "span_id", "parent_span_id", "release", "dist", "sentry-trace", "baggage"]);

// Request headers an event may keep; every other header is dropped.
const SAFE_HEADERS = new Set([
  "accept", "accept-encoding", "accept-language", "content-length", "content-type", "host", "origin", "referer",
  "user-agent", "x-nearshop-client", "x-request-id", "x-vercel-id", "sentry-trace", "baggage",
]);

// A run of non-space text that may be URL-encoded: it holds a %XX escape or a + (a space in a query string).
const ENCODED = /[^\s]*(?:%[0-9A-Fa-f]{2}|\+)[^\s]*/g;

function maskPlain(text: string): string {
  return text.replace(EMAIL, "[email]").replace(PHONE, "[phone]").replace(ADDRESS, "[address]");
}

/** One round of URL decoding (+ is a space), leaving malformed escapes as they are. */
function decodeOnce(text: string): string {
  return text.replace(/\+/g, " ").replace(/(?:%[0-9A-Fa-f]{2})+/g, (run) => {
    try {
      return decodeURIComponent(run);
    } catch {
      return run;
    }
  });
}

/** A URL or query string (q=tape+98450%2012345, a%40b.com, next=%2Fsearch%3Fq%3D...) decoded, up to three
 * times for a URL inside a URL, and masked; unchanged when its decoded form holds nothing to mask. */
function maskEncoded(token: string): string {
  let decoded = token;
  for (let i = 0; i < 3; i++) {
    const once = decodeOnce(decoded);
    if (once === decoded) break;
    decoded = once;
    const masked = maskPlain(decoded);
    if (masked !== decoded) return masked;
  }
  return token;
}

/** text with Indian mobile numbers, email addresses and street addresses replaced by placeholders, in plain
 * text and in URL-encoded text alike (page URLs, query strings, fetch breadcrumbs). */
export function maskText(text: string): string {
  const masked = maskPlain(text);
  return masked.includes("%") || masked.includes("+") ? masked.replace(ENCODED, maskEncoded) : masked;
}

/** A copy of value with maskText applied to every string, however deeply nested. */
export function maskPii<T>(value: T, key?: string): T {
  if (typeof value === "string") return (key && ID_KEYS.has(key) ? value : maskText(value)) as T;
  if (Array.isArray(value)) return value.map((v) => maskPii(v)) as T;
  if (value && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, maskPii(v, k)])) as T;
  }
  return value;
}

/** beforeSend and beforeSendTransaction: remove what may identify a person. */
export function scrubEvent<T extends Sentry.Event>(event: T): T {
  const request = event.request;
  if (request) {
    delete request.cookies;
    delete request.data;
    if (request.headers) {
      request.headers = Object.fromEntries(
        Object.entries(request.headers).filter(([name]) => SAFE_HEADERS.has(name.toLowerCase())),
      );
    }
  }
  delete event.user;
  return maskPii(event);
}

/** beforeBreadcrumb: navigation URLs, fetch URLs and console messages are masked like events. */
export function scrubBreadcrumb(crumb: Sentry.Breadcrumb): Sentry.Breadcrumb {
  return maskPii(crumb);
}

/** The paths a span being sampled is about: its name ("/search", "GET /api/search"), the page's location
 * in the browser, and the incoming request's URL on the server. */
function sampledPaths({ name, location, normalizedRequest }: TracesSamplerSamplingContext): string[] {
  const paths = [name.replace(/^[A-Z]+\s+/, "")];
  if (location?.pathname) paths.push(location.pathname);
  if (normalizedRequest?.url) {
    try {
      paths.push(new URL(normalizedRequest.url, "http://localhost").pathname);
    } catch {
      // not a URL: the name is enough
    }
  }
  return paths;
}

/** 25% of searches (the search page and /api/search), 10% of everything else, never a health check. A
 * trace continued from elsewhere keeps that trace's decision. */
export function tracesSampler(context: TracesSamplerSamplingContext): number {
  const paths = sampledPaths(context);
  if (paths.some((p) => p.startsWith("/api/health"))) return 0;
  const search = paths.some((p) => /^(\/api)?\/search(\/|$)/.test(p));
  return context.inheritOrSampleWith(search ? SEARCH_TRACES_SAMPLE_RATE : TRACES_SAMPLE_RATE);
}

/** The options both runtimes pass to Sentry.init. */
export function sentryOptions() {
  return {
    dsn: SENTRY_DSN,
    enabled: Boolean(SENTRY_DSN),
    environment: SENTRY_ENVIRONMENT,
    release: SENTRY_RELEASE,
    sendDefaultPii: false,
    tracesSampler,
    beforeSend: scrubEvent,
    beforeSendTransaction: scrubEvent,
    beforeBreadcrumb: scrubBreadcrumb,
  };
}
