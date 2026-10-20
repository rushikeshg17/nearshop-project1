import type { MetadataRoute } from "next";

import { getSitemapEntries, SITEMAP_MAX_URLS, sitemapCount, sitemapUrls } from "@/lib/server-api";

// The sitemap (P2.8): the home page, every live shop and every active listing, from the API's /api/sitemap, which
// applies the same customer-visibility rules as every catalogue read (so P3.3's unverified shops never appear).
// One file per 50,000 URLs, at /sitemap/0.xml, /sitemap/1.xml and so on; robots.ts lists them all.
//
// Built when asked, never at build time (the build has no API), from the list the API and src/lib/server-api.ts
// each keep for an hour. If the API cannot be reached the request fails, so a crawler retries later instead of
// keeping an empty sitemap.
export const dynamic = "force-dynamic";

export async function generateSitemaps() {
  let count = 1;
  try {
    count = sitemapCount(await getSitemapEntries());
  } catch {
    // next build asks too, with no API to reach; at request time the sitemap itself then reports the failure.
  }
  return Array.from({ length: count }, (_, id) => ({ id }));
}

export default async function sitemap({ id }: { id: Promise<string> }): Promise<MetadataRoute.Sitemap> {
  const part = Number(await id);
  const urls = sitemapUrls(await getSitemapEntries());
  return urls.slice(part * SITEMAP_MAX_URLS, (part + 1) * SITEMAP_MAX_URLS);
}
