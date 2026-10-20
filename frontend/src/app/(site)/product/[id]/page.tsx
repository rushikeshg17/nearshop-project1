import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { formatPrice } from "@/lib/format";
import { absoluteUrl, getProduct, indexingEnabled, isCrawler, jsonLdScript, type ApiResult } from "@/lib/server-api";
import type { ProductDetail } from "@/lib/types";

import { ProductView } from "./product-view";

// Server-rendered (P2.8): the listing comes from the API on the server, shared by every visitor for 60 seconds
// (src/lib/server-api.ts), so the HTML has the item, its price, stock and freshness, and the shop's name,
// address, hours and rating before any script runs. Distance, nearby alternatives, recommendations and the map
// need the visitor's location, which only the browser knows, so they load after hydration (product-view.tsx).

const AVAILABILITY = {
  in_stock: "https://schema.org/InStock",
  low_stock: "https://schema.org/LimitedAvailability",
  out_of_stock: "https://schema.org/OutOfStock",
} as const;

function productId(raw: string): number | null {
  return /^\d{1,15}$/.test(raw) ? Number(raw) : null;
}

/** The listing, or notFound() (HTTP 404) when the API has none; null when the API could not be reached, in
 * which case the page falls back to loading it in the browser (crawlers get HTTP 500 instead). */
async function load(raw: string): Promise<ProductDetail | null> {
  const id = productId(raw);
  if (id === null) notFound();
  let result: ApiResult<ProductDetail>;
  try {
    result = await getProduct(id);
  } catch (err) {
    console.error("product page: server fetch failed", err);
    // A crawler gets the error, so HTTP 500: it keeps what it has and retries later, instead of indexing an
    // empty shell under a generic title. A visitor gets the page, which then loads in the browser.
    if (await isCrawler()) throw err;
    return null;
  }
  if (result.status === 404) notFound();
  return result.data;
}

function stockText(p: ProductDetail): string {
  if (p.stock_status === "out_of_stock") return "Out of stock";
  return p.stock_status === "low_stock" ? `Only ${p.quantity} left` : `${p.quantity} in stock`;
}

export async function generateMetadata({ params }: PageProps<"/product/[id]">): Promise<Metadata> {
  const { id } = await params;
  const p = await load(id);
  if (!p) return { title: "Product", robots: { index: false, follow: false } };
  const shop = p.shop;
  const title = `${p.name} at ${shop.name}, ${shop.city}`;
  const description =
    `${formatPrice(p.price)} at ${shop.name}, ${shop.address_line}, ${shop.city}. ${stockText(p)}. ` +
    "Reserve for pickup on NearShop and pay at the shop.";
  const url = absoluteUrl(`/product/${p.id}`);
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
      images: p.image_url ? [{ url: absoluteUrl(p.image_url), alt: p.name }] : undefined,
    },
    robots: indexingEnabled() ? undefined : { index: false, follow: false },
  };
}

function productJsonLd(p: ProductDetail) {
  const url = absoluteUrl(`/product/${p.id}`);
  const shopUrl = absoluteUrl(`/shops/${p.shop.slug}`);
  return {
    "@context": "https://schema.org",
    "@type": "Product",
    "@id": url,
    name: p.name,
    url,
    ...(p.description ? { description: p.description } : {}),
    ...(p.image_url ? { image: [absoluteUrl(p.image_url)] } : {}),
    ...(p.brand ? { brand: { "@type": "Brand", name: p.brand } } : {}),
    ...(p.sku ? { sku: p.sku } : {}),
    category: p.category_detail.name,
    offers: {
      "@type": "Offer",
      url,
      price: p.price.toFixed(2),
      priceCurrency: "INR",
      availability: AVAILABILITY[p.stock_status],
      itemCondition: "https://schema.org/NewCondition",
      seller: { "@type": "Store", "@id": shopUrl, name: p.shop.name, url: shopUrl },
    },
  };
}

export default async function ProductPage({ params }: PageProps<"/product/[id]">) {
  const { id } = await params;
  const p = await load(id);
  return (
    <>
      {p && <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: jsonLdScript(productJsonLd(p)) }} />}
      <ProductView id={Number(id)} initial={p} />
    </>
  );
}
