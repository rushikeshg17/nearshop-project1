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

// Customer and owner routes with a dynamic segment, as their templates (src/app). Web Vitals and every other
// browser span carry the template, never the id, slug or query string of the page the visitor was on.
const DYNAMIC_ROUTES: [RegExp, string][] = [
  [/^\/product\/[^/]+$/, "/product/[id]"],
  [/^\/shops\/[^/]+$/, "/shops/[slug]"],
  [/^\/account\/orders\/[^/]+$/, "/account/orders/[id]"],
  [/^\/account\/reservations\/[^/]+$/, "/account/reservations/[id]"],
  [/^\/sitemap\/[^/]+$/, "/sitemap/[id]"],
];
// A static route: up to four segments of lowercase letters and hyphens (/, /search, /shop/inventory).
const STATIC_ROUTE = /^(?:\/[a-z][a-z-]*){0,4}$/;

/** The route template of a path or URL: "/product/880?ref=x" gives "/product/[id]", "/search?q=tape" gives
 * "/search". A path that is neither a dynamic route nor static-looking gives "/[other]". */
export function routeTemplate(pathOrUrl: string): string {
  let path = pathOrUrl;
  try {
    path = new URL(pathOrUrl, "http://localhost").pathname;
  } catch {
    // not a URL: use it as it is
  }
  const pathname = path.replace(/[?#].*$/, "").replace(/\/+$/, "") || "/";
  for (const [pattern, template] of DYNAMIC_ROUTES) if (pattern.test(pathname)) return template;
  return pathname === "/" || STATIC_ROUTE.test(pathname) ? pathname : "/[other]";
}

// A URL or path followed by a query string or fragment ("/search?q=tape", "https://host/a?b=c#d").
const URL_WITH_QUERY = /((?:[a-z][a-z0-9+.-]*:)?\/\/?[^\s?#"']*)[?#][^\s"']*/gi;

/** text with the query string and fragment removed from every URL or path in it. */
export function stripQuery(text: string): string {
  return text.replace(URL_WITH_QUERY, "$1");
}

// Span attributes that name the page (on every span), and those that hold the page's own URL (on the pageload
// or navigation span and on Web Vital spans). In the browser they are replaced by the route template. Other
// spans (scripts, images, API calls) keep their URL, without the query string.
const PAGE_NAME_KEYS = new Set(["sentry.transaction", "sentry.segment.name"]);
const PAGE_URL_KEYS = new Set(["url.full", "url.path", "http.url"]);
// Span attributes dropped everywhere: query strings and fragments, never needed to read a trace.
const QUERY_KEY = /(?:^|[._])(?:query|fragment|search)(?:$|[._])/;

type SpanLike = { name: string; is_segment?: boolean; attributes?: Record<string, unknown> };

/** beforeSendSpan (span streaming, the SDK's default trace lifecycle; beforeSendTransaction is not called
 * then): every span, Web Vitals included, has PII masked, query strings removed and request headers limited
 * to SAFE_HEADERS. In the browser, the
 * page's URL is replaced by its route template, and each span carries nearshop.route and the network's
 * effective connection type (network.connection.effective_type: slow-2g, 2g, 3g or 4g). */
export function scrubSpan<T extends SpanLike>(span: T): T {
  const browser = typeof window !== "undefined";
  // The pageload or navigation span, or a Web Vital span (LCP, CLS, INP: browser.web_vital.* attributes).
  const pageSpan = Boolean(span.is_segment) || Object.keys(span.attributes ?? {}).some((k) => k.startsWith("browser.web_vital."));
  const attributes: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(span.attributes ?? {})) {
    if (QUERY_KEY.test(key)) continue;
    // Request and response headers on server spans: only the headers an event may keep (never cookies or
    // the authorization header), as scrubEvent does for events.
    const header = /^http\.(?:request|response)\.header\.(.+)$/.exec(key);
    if (header && !SAFE_HEADERS.has(header[1].toLowerCase())) continue;
    const pageKey = PAGE_NAME_KEYS.has(key) || (pageSpan && PAGE_URL_KEYS.has(key));
    if (Array.isArray(value)) {
      attributes[key] = value.map((v) => (typeof v === "string" ? stripQuery(v) : v));
    } else if (typeof value !== "string") {
      attributes[key] = value;
    } else if (browser && pageKey && (value.startsWith("/") || /^https?:/.test(value))) {
      attributes[key] = routeTemplate(value);
    } else {
      attributes[key] = stripQuery(value);
    }
  }
  let name = stripQuery(span.name);
  if (browser) {
    const route = routeTemplate(window.location.pathname);
    if (span.is_segment && name.startsWith("/")) name = routeTemplate(name);
    attributes["nearshop.route"] = route;
    const connection = (navigator as Navigator & { connection?: { effectiveType?: string } }).connection;
    attributes["network.connection.effective_type"] ??= connection?.effectiveType ?? "unknown";
  }
  return { ...span, name: maskText(name), attributes: maskPii(attributes) };
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
    beforeSendTransaction: scrubEvent, // only with traceLifecycle "static"; spans stream by default
    beforeSendSpan: scrubSpan,
    beforeBreadcrumb: scrubBreadcrumb,
  };
}
