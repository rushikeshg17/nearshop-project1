import type { MetadataRoute } from "next";

import { absoluteUrl, getSitemapEntries, indexingEnabled, sitemapCount } from "@/lib/server-api";

// robots.txt (P2.8). Search engines may crawl only when the server-only NEARSHOP_INDEXING is true: production from
// launch day (plan B14). Unset, as in staging, demo and production before launch, everything is disallowed, so the
// synthetic shops production still holds until P7.1 are never indexed. Read when asked, not at build time.
export const dynamic = "force-dynamic";

export default async function robots(): Promise<MetadataRoute.Robots> {
  if (!indexingEnabled()) return { rules: { userAgent: "*", disallow: "/" } };
  let count = 1;
  try {
    count = sitemapCount(await getSitemapEntries());
  } catch {
    // The API is unreachable: list the first sitemap, which always exists.
  }
  return {
    rules: {
      userAgent: "*",
      allow: "/",
      // Signed-in areas and the API. "/shop/" (the owner console) does not cover the public /shops/ pages.
      disallow: ["/account/", "/shop/", "/admin/", "/api/"],
    },
    sitemap: Array.from({ length: count }, (_, id) => absoluteUrl(`/sitemap/${id}.xml`)),
  };
}
