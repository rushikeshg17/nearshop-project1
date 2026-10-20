import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";

import { absoluteUrl, getShop, indexingEnabled, jsonLdScript, type ApiResult } from "@/lib/server-api";
import type { ShopDetail } from "@/lib/types";

import { ShopView } from "./shop-view";

// Server-rendered (P2.8): the shop comes from the API on the server, shared by every visitor for 60 seconds
// (src/lib/server-api.ts), so the HTML has its name, address, opening hours, rating and stock counts before any
// script runs. The distance and the map need the browser, and the shelf and reviews load there (shop-view.tsx).

// Slugs are slugify() output (backend/app/utils/text.py): letters, digits and hyphens. Anything else is a 404.
const SLUG = /^[A-Za-z0-9-]{1,200}$/;
const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
// The same pattern as the API's open_status (backend/app/utils/hours.py): "9:30 AM - 9:00 PM".
const HOURS = /(\d{1,2})(?::(\d{2}))?\s*([AP]M)\s*[-–to]+\s*(\d{1,2})(?::(\d{2}))?\s*([AP]M)/i;

/** The shop, or notFound() (HTTP 404) when the API has none; null when the API could not be reached, in which
 * case the page falls back to loading it in the browser. */
async function load(slug: string): Promise<ShopDetail | null> {
  if (!SLUG.test(slug)) notFound();
  let result: ApiResult<ShopDetail>;
  try {
    result = await getShop(slug);
  } catch (err) {
    console.error("shop page: server fetch failed", err);
    return null;
  }
  if (result.status === 404) notFound();
  return result.data;
}

function place(s: ShopDetail): string {
  return [s.locality, s.city].filter(Boolean).join(", ");
}

export async function generateMetadata({ params }: PageProps<"/shops/[slug]">): Promise<Metadata> {
  const { slug } = await params;
  const s = await load(slug);
  if (!s) return { title: "Shop" };
  const title = `${s.name}, ${place(s)}`;
  const rating = s.rating_avg !== null && s.rating_count > 0 ? ` Rated ${s.rating_avg.toFixed(1)} from ${s.rating_count} reviews.` : "";
  const description =
    `${s.tagline ? `${s.tagline}. ` : ""}${s.address_line}, ${s.city}.` +
    `${s.opening_hours ? ` Open ${s.opening_hours}${s.closed_on ? `, closed ${s.closed_on}s` : ""}.` : ""}${rating}` +
    " See what is in stock now and reserve for pickup on NearShop.";
  const url = absoluteUrl(`/shops/${s.slug}`);
  return {
    title,
    description,
    alternates: { canonical: url },
    openGraph: {
      type: "website",
      siteName: "NearShop",
      locale: "en_IN",
      url,
      title,
      description,
      images: s.image_url ? [{ url: absoluteUrl(s.image_url), alt: s.name }] : undefined,
    },
    robots: indexingEnabled() ? undefined : { index: false, follow: false },
  };
}

function time24(h: string, m: string | undefined, ampm: string): string {
  const hour = (Number(h) % 12) + (ampm.toUpperCase() === "PM" ? 12 : 0);
  return `${String(hour).padStart(2, "0")}:${m ?? "00"}`;
}

/** schema.org opening hours from the owner's "9:30 AM - 9:00 PM" and weekly closed day, when they parse. */
function openingHours(s: ShopDetail) {
  const m = HOURS.exec(s.opening_hours ?? "");
  if (!m) return undefined;
  const closed = s.closed_on?.trim().toLowerCase();
  return [
    {
      "@type": "OpeningHoursSpecification",
      dayOfWeek: DAYS.filter((d) => d.toLowerCase() !== closed),
      opens: time24(m[1], m[2], m[3]),
      closes: time24(m[4], m[5], m[6]),
    },
  ];
}

function shopJsonLd(s: ShopDetail) {
  const url = absoluteUrl(`/shops/${s.slug}`);
  const hours = openingHours(s);
  return {
    "@context": "https://schema.org",
    "@type": "Store",
    "@id": url,
    name: s.name,
    url,
    ...(s.description || s.tagline ? { description: s.description ?? s.tagline } : {}),
    ...(s.image_url ? { image: [absoluteUrl(s.image_url)] } : {}),
    ...(s.phone ? { telephone: s.phone } : {}),
    address: {
      "@type": "PostalAddress",
      streetAddress: s.address_line,
      addressLocality: s.city,
      ...(s.pincode ? { postalCode: s.pincode } : {}),
      addressCountry: "IN",
    },
    geo: { "@type": "GeoCoordinates", latitude: s.lat, longitude: s.lng },
    ...(hours ? { openingHoursSpecification: hours } : {}),
    ...(s.rating_avg !== null && s.rating_count > 0
      ? {
          aggregateRating: {
            "@type": "AggregateRating",
            ratingValue: s.rating_avg,
            reviewCount: s.rating_count,
            bestRating: 5,
            worstRating: 1,
          },
        }
      : {}),
  };
}

export default async function ShopPage({ params }: PageProps<"/shops/[slug]">) {
  const { slug } = await params;
  const s = await load(slug);
  return (
    <>
      {s && <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: jsonLdScript(shopJsonLd(s)) }} />}
      <Suspense>
        <ShopView slug={slug} initial={s} />
      </Suspense>
    </>
  );
}
