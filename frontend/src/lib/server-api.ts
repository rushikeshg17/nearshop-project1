import "server-only";

import { unstable_cache } from "next/cache";
import { headers } from "next/headers";
import { userAgent } from "next/server";
import { cache } from "react";

import type { ProductDetail, ShopDetail, SitemapEntries } from "@/lib/types";

// Server-only access to the NearShop API for server-rendered pages, the sitemap and robots.txt (P2.8).
//
// It calls NEARSHOP_API_ORIGIN directly, not through the site's own /api rewrite, with the two headers that
// src/proxy.ts adds to the browser's requests (P2.3, backend/app/core/ratelimit.py):
// - X-NearShop-Edge carries NEARSHOP_EDGE_SECRET, without which staging and production answer 403. It is a
//   server-only variable, never NEXT_PUBLIC_, and this module never reaches the browser bundle;
// - X-NearShop-Client-IP carries the visitor's address, so the API's per-IP limits count the visitor whose
//   page view caused the call, not the Vercel function that makes it.
// It never forwards the visitor's cookies or session: what it fetches is the same for everyone, so it is
// cached across visitors (for REVALIDATE_SECONDS), and the pages hold nothing personal.
//
// Decision (P2.8, to carry into P2.9): what is cached is the API data, not the HTML. Reading the visitor's
// address calls headers(), which makes the product and shop pages render on every view (Cache-Control:
// private, no-store), as a function run with the data from the cache. The alternative, a page cached whole
// at the CDN, would have to call the API with no visitor address, so every cache miss, a crawler's included,
// would count against the address of Vercel's function, which all visitors share. Per-IP limits on the
// visitor (P2.3) were chosen over a CDN-cached page, so the "cached page" time to first byte is a warm
// function plus a data-cache hit; P2.9 measures it and revisits this if it misses 200 ms.

const API_ORIGIN = (process.env.NEARSHOP_API_ORIGIN ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
const EDGE_HEADER = "x-nearshop-edge";
const CLIENT_IP_HEADER = "x-nearshop-client-ip";
const TIMEOUT_MS = 8000;

/** How long product and shop data is reused before the next visitor's view fetches it again. */
export const REVALIDATE_SECONDS = 60;
/** How long the sitemap's list of shops and listings is reused (the API caches it for as long). */
export const SITEMAP_REVALIDATE_SECONDS = 3600;

/** A cacheable answer: the data, or the API's 404. Other failures throw and are never cached. */
export type ApiResult<T> = { status: 200; data: T } | { status: 404 };

export class ServerApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** The site's public origin, for canonical URLs, Open Graph, structured data and the sitemap. NEARSHOP_SITE_URL
 * when set (production's own domain); otherwise what Vercel says this deployment is; locally, next start. */
export function siteOrigin(): string {
  const configured = process.env.NEARSHOP_SITE_URL?.trim();
  if (configured) return configured.replace(/\/+$/, "");
  const vercel =
    process.env.VERCEL_ENV === "production"
      ? process.env.VERCEL_PROJECT_PRODUCTION_URL
      : (process.env.VERCEL_BRANCH_URL ?? process.env.VERCEL_URL);
  return vercel ? `https://${vercel}` : "http://localhost:3000";
}

/** An absolute URL on this site for a path, or the URL itself when it is already absolute. */
export function absoluteUrl(pathOrUrl: string): string {
  return /^https?:\/\//i.test(pathOrUrl) ? pathOrUrl : `${siteOrigin()}${pathOrUrl.startsWith("/") ? "" : "/"}${pathOrUrl}`;
}

/** Whether search engines may index the site: only when the server-only NEARSHOP_INDEXING is true. It stays
 * unset in staging, in demo and in production until launch day (plan B14). */
export function indexingEnabled(): boolean {
  return process.env.NEARSHOP_INDEXING?.trim().toLowerCase() === "true";
}

/** The visitor's address as Vercel gives it to the function (as src/proxy.ts reads it), or undefined. */
async function visitorAddress(): Promise<string | undefined> {
  const h = await headers();
  const realIp = h.get("x-real-ip")?.trim();
  if (realIp) return realIp;
  return h.get("x-forwarded-for")?.split(",")[0]?.trim() || undefined;
}

async function fetchFromApi<T>(path: string, clientIp: string | undefined): Promise<ApiResult<T>> {
  const requestHeaders: Record<string, string> = { accept: "application/json" };
  const secret = process.env.NEARSHOP_EDGE_SECRET;
  if (secret) {
    requestHeaders[EDGE_HEADER] = secret;
    // Believed by the API only with the secret (as from the proxy), so it is sent only with it.
    if (clientIp) requestHeaders[CLIENT_IP_HEADER] = clientIp;
  }
  let res: Response;
  try {
    // no-store: the cache around this call (unstable_cache, below) keys on the path alone. A fetch cache would
    // key on the headers too, the visitor's address among them, and so never be shared between visitors.
    res = await fetch(`${API_ORIGIN}/api${path}`, {
      headers: requestHeaders,
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (err) {
    throw new ServerApiError(`NearShop API unreachable for ${path}: ${(err as Error).name}`, 0);
  }
  if (res.status === 404) return { status: 404 };
  if (!res.ok) throw new ServerApiError(`NearShop API answered ${res.status} for ${path}`, res.status);
  return { status: 200, data: (await res.json()) as T };
}

type Stamped<T> = { fetchedAt: number; result: ApiResult<T> };

/** GET a location-independent, public API path, shared by every visitor for at most `revalidate` seconds. On a
 * miss, the call carries the address of the visitor whose request made it.
 *
 * unstable_cache is stale-while-revalidate: past `revalidate`, it hands the old entry to one more request and
 * refreshes it in the background. A page visited rarely (as crawlers visit) would then get data as old as its
 * last visit, whether hours or days: a removed listing or a paused shop answering 200 with its old price and
 * stock. So each entry carries the time it was fetched, and one older than `revalidate` is not used: that
 * request fetches the data itself, while unstable_cache's own background refresh renews the entry for the next. */
async function cachedGet<T>(path: string, revalidate: number, clientIp: string | undefined): Promise<ApiResult<T>> {
  const stamped = await unstable_cache(
    async (): Promise<Stamped<T>> => ({ fetchedAt: Date.now(), result: await fetchFromApi<T>(path, clientIp) }),
    ["nearshop-api-v2", path],
    { revalidate },
  )();
  if (Date.now() - stamped.fetchedAt <= revalidate * 1000) return stamped.result;
  return fetchFromApi<T>(path, clientIp);
}

/** Whether this request comes from a known crawler (Next.js's own list, as it uses for metadata streaming). */
export async function isCrawler(): Promise<boolean> {
  return userAgent({ headers: await headers() }).isBot;
}

// React's cache: generateMetadata and the page ask once per request.

/** One listing, without the visitor's location or session (no distance, delivery quote or reservation). */
export const getProduct = cache(async (id: number): Promise<ApiResult<ProductDetail>> => {
  return cachedGet<ProductDetail>(`/products/${id}`, REVALIDATE_SECONDS, await visitorAddress());
});

/** One shop, without the visitor's location (no distance). */
export const getShop = cache(async (slug: string): Promise<ApiResult<ShopDetail>> => {
  return cachedGet<ShopDetail>(`/shops/${encodeURIComponent(slug)}`, REVALIDATE_SECONDS, await visitorAddress());
});

/** The live shops and active listings for the sitemap. No visitor is involved (crawlers and the build ask). */
export const getSitemapEntries = cache(async (): Promise<SitemapEntries> => {
  const result = await cachedGet<SitemapEntries>("/sitemap", SITEMAP_REVALIDATE_SECONDS, undefined);
  if (result.status !== 200) throw new ServerApiError("NearShop API has no sitemap endpoint", 404);
  return result.data;
});

/** At most this many URLs per sitemap file (the sitemap protocol's limit); above it, sitemap.ts splits. */
export const SITEMAP_MAX_URLS = 50000;

/** Every URL the sitemap lists, home first, then shops and listings, each with its last update. */
export function sitemapUrls(entries: SitemapEntries): { url: string; lastModified?: string }[] {
  const at = (updated: string | null) => (updated ? { lastModified: updated } : {});
  return [
    { url: absoluteUrl("/") },
    ...entries.shops.map((s) => ({ url: absoluteUrl(`/shops/${encodeURIComponent(s.slug)}`), ...at(s.updated_at) })),
    ...entries.listings.map((p) => ({ url: absoluteUrl(`/product/${p.id}`), ...at(p.updated_at) })),
  ];
}

/** How many sitemap files the entries need (at least one). */
export function sitemapCount(entries: SitemapEntries): number {
  return Math.max(1, Math.ceil(sitemapUrls(entries).length / SITEMAP_MAX_URLS));
}

/** JSON for a script type="application/ld+json" element. Every < is escaped (the Next.js JSON-LD guide), so no
 * owner-entered string can close the element or open a tag or a comment, whatever it contains. */
export function jsonLdScript(data: object): string {
  return JSON.stringify(data).replace(/</g, "\\u003c");
}
