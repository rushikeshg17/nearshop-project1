"use client";

import { Bike, ChevronRight, Clock3, Footprints, Store } from "lucide-react";
import Link from "next/link";
import type { MouseEvent } from "react";

import { Distance, Freshness, Rating, StockPill } from "@/components/common/indicators";
import { ProductThumb } from "@/components/common/product-thumb";
import { useI18n } from "@/components/providers";
import { formatDistance, formatNumber, formatPrice, travelTime } from "@/lib/format";
import { plural } from "@/lib/i18n";
import type { ResultGroup, SponsoredCard as Sponsored } from "@/lib/types";
import { cn } from "@/lib/utils";

/** A listing's stock freshness from the API (P7.4): fresh within 48 hours (no label), aging up to 7 days
 * ("updated N days ago", ranked lower) and stale beyond ("stock not confirmed recently", ranked lower still). */
type StockFreshness = { band: "fresh" | "aging" | "stale"; label: string | null };

function FreshnessLabel({ freshness }: { freshness: StockFreshness }) {
  const { t, language } = useI18n();
  if (!freshness.label) return null;
  const stale = freshness.band === "stale";
  // The API's label is English ("updated N days ago"); the text shown is the interface language's, with the same N.
  const days = Number(/(\d+)/.exec(freshness.label)?.[1] ?? Number.NaN);
  const text = stale
    ? t("discovery.result.freshness.stale")
    : Number.isFinite(days)
      ? t(`discovery.result.freshness.aging.${plural(language, days)}`, { count: formatNumber(days) })
      : t("discovery.result.freshness.stale");
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs whitespace-nowrap",
        stale ? "bg-warning-soft text-warning-ink" : "bg-muted text-muted-foreground",
      )}
    >
      <Clock3 className="size-3" aria-hidden />
      {text}
    </span>
  );
}

/** Report that a customer opened a search result or one of its offers (P5.6). The API records it only for a
 * signed-in customer with the search-measurement consent, and answers 204 either way. keepalive lets it finish
 * after the page navigates away; nothing waits for it, and a failure is ignored. */
function reportClick(listingId: number, group: string, position: number) {
  try {
    void fetch("/api/search/click", {
      method: "POST",
      keepalive: true,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-NearShop-Client": "web" },
      body: JSON.stringify({ listing_id: listingId, group, position }),
    }).catch(() => undefined);
  } catch {
    // Never let measurement get in the way of opening the result.
  }
}

export function ResultCard({
  group,
  index = 0,
  highlighted,
  onHover,
}: {
  group: ResultGroup;
  index?: number;
  highlighted?: boolean;
  onHover?: (shopIds: number[] | null) => void;
}) {
  const { t, language } = useI18n();
  const best = group.best_offer;
  const travel = travelTime(best.shop.distance_km);
  const freshness = (best as typeof best & { freshness?: StockFreshness }).freshness;
  const others = group.offers.filter((o) => o.id !== best.id).slice(0, 3);
  const outOfStock = group.in_stock_count === 0;
  // index is the card's 0-based place in the loaded list; positions are 1-based. Middle clicks open a tab too.
  const opened = (listingId: number) => ({
    onClick: () => reportClick(listingId, group.key, index + 1),
    onAuxClick: (e: MouseEvent) => {
      if (e.button === 1) reportClick(listingId, group.key, index + 1);
    },
  });

  return (
    // CSS entrance (tw-animate-css), staggered for the first rows; no animation library on first load (P2.9).
    <article
      style={{ animationDelay: `${Math.min(index, 8) * 35}ms` }}
      onMouseEnter={() => onHover?.(group.offers.map((o) => o.shop.id))}
      onMouseLeave={() => onHover?.(null)}
      className={cn(
        "group relative animate-in rounded-2xl border bg-card fill-mode-both duration-300 ease-out fade-in slide-in-from-bottom-3",
        "transition-[box-shadow,border-color] hover:border-foreground/15 hover:shadow-lift",
        highlighted && "border-brand/40 shadow-lift",
        outOfStock && "opacity-80",
      )}
    >
      <Link href={`/product/${best.id}`} {...opened(best.id)} className="flex gap-4 rounded-2xl p-4" aria-label={t("discovery.result.label", { name: group.name, price: formatPrice(group.min_price) })}>
        <ProductThumb icon={group.icon} imageUrl={group.image_url} thumbnailUrl={group.thumbnail_url} category={group.category} alt="" size="lg" className="max-sm:size-20" />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              {group.brand && <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{group.brand}</p>}
              <h3 className="line-clamp-2 leading-snug font-semibold">{group.name}</h3>
            </div>
            <div className="shrink-0 text-right">
              <p className="text-lg font-semibold tracking-tight tabular">{formatPrice(group.min_price)}</p>
              {group.max_price > group.min_price && (
                <p className="text-xs text-muted-foreground tabular">{t("discovery.result.upTo", { price: formatPrice(group.max_price) })}</p>
              )}
            </div>
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <StockPill status={best.stock_status} quantity={best.quantity} />
            {freshness?.label ? <FreshnessLabel freshness={freshness} /> : <Freshness at={best.stock_updated_at} />}
          </div>

          <div className="mt-3 flex items-center gap-2 text-sm">
            <Store className="size-4 shrink-0 text-muted-foreground" />
            <span className="truncate font-medium">{best.shop.name}</span>
            <span className="shrink-0 text-muted-foreground">·</span>
            <Distance km={best.shop.distance_km} className="shrink-0 text-sm" />
            {travel && (
              <span className="hidden shrink-0 text-xs text-muted-foreground sm:inline">
                ({t(`common.travel.${travel.mode}`, { minutes: travel.minutes })})
              </span>
            )}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <Rating value={best.shop.rating_avg} count={best.shop.rating_count} />
            {best.shop.offers_pickup && (
              <span className="inline-flex items-center gap-1">
                <Footprints className="size-3.5" /> {t("discovery.result.pickup")}
              </span>
            )}
            {best.shop.offers_delivery && (
              <span className="inline-flex items-center gap-1">
                <Bike className="size-3.5" /> {t("discovery.result.delivery")}
              </span>
            )}
          </div>
        </div>
      </Link>

      {group.shop_count > 1 && (
        <div className="border-t px-4 py-2.5">
          <p className="mb-1.5 text-xs font-medium text-muted-foreground">
            {t(`discovery.result.shopsHaveThis.${plural(language, group.shop_count)}`, { count: formatNumber(group.shop_count) })}
            {group.in_stock_count < group.shop_count &&
              ` · ${t("discovery.result.inStock", { count: formatNumber(group.in_stock_count) })}`}
          </p>
          <ul className="flex flex-wrap gap-1.5">
            {others.map((o) => (
              <li key={o.id}>
                <Link
                  href={`/product/${o.id}`}
                  {...opened(o.id)}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors hover:bg-accent",
                    o.quantity <= 0 && "text-muted-foreground line-through decoration-muted-foreground/40",
                  )}
                >
                  <span className="max-w-36 truncate">{o.shop.name}</span>
                  <span className="font-medium tabular">{formatPrice(o.price)}</span>
                  <span className="text-muted-foreground tabular">{formatDistance(o.shop.distance_km)}</span>
                </Link>
              </li>
            ))}
            {group.shop_count > 4 && (
              <li>
                <Link href={`/product/${best.id}#compare`} {...opened(best.id)} className="inline-flex items-center gap-0.5 rounded-full px-2 py-1 text-xs font-medium hover:bg-accent">
                  {t("discovery.result.compareAll")} <ChevronRight className="size-3" />
                </Link>
              </li>
            )}
          </ul>
        </div>
      )}
    </article>
  );
}

/** Report that a customer opened the sponsored card (Boost, P10.12): its token goes to the API, which counts the
 * click for the pack and remembers it for this browser session only. Nothing names the customer; 204 either way. */
function reportSponsoredClick(token: string) {
  try {
    void fetch("/api/search/sponsored-click", {
      method: "POST",
      keepalive: true,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-NearShop-Client": "web" },
      body: JSON.stringify({ token }),
    }).catch(() => undefined);
  } catch {
    // Never let counting get in the way of opening the listing.
  }
}

/**
 * The sponsored variant of the result card (Boost, P10.13; plan section 8.22). A results page has at most one, and
 * it must never pass for an organic result: its first line is the label ("Sponsored", the wording counsel reviewed
 * under B31) on a tinted band, the card has its own coloured border, and the link's accessible name starts with the
 * label, so a screen reader announces it before anything else. The label is text in rem units that wraps (never
 * truncated), so it stays readable at the largest Android font size; the label's colours (info-ink on info-soft)
 * have a contrast of 4.5:1 or more in both themes, and the rest of the card sits on the ordinary card background.
 */
export function SponsoredResultCard({ card, onHover }: { card: Sponsored; onHover?: (shopIds: number[] | null) => void }) {
  const { t } = useI18n();
  const best = card.listing;
  const label = t("discovery.result.sponsored.label");
  const freshness = (best as typeof best & { freshness?: StockFreshness }).freshness;
  const opened = {
    onClick: () => reportSponsoredClick(card.token),
    onAuxClick: (e: MouseEvent) => {
      if (e.button === 1) reportSponsoredClick(card.token);
    },
  };
  return (
    <article
      data-testid="sponsored-card"
      data-sponsored="true"
      onMouseEnter={() => onHover?.([best.shop.id])}
      onMouseLeave={() => onHover?.(null)}
      className="relative overflow-hidden rounded-2xl border-2 border-info/70 bg-card transition-shadow hover:shadow-lift"
    >
      <p className="bg-info-soft px-4 py-1.5 text-sm font-semibold break-words text-info-ink" data-testid="sponsored-label">
        {label}
      </p>
      <Link
        href={`/product/${best.id}`}
        {...opened}
        className="flex gap-4 rounded-b-2xl p-4"
        aria-label={t("discovery.result.sponsored.name", { label, name: card.name, price: formatPrice(best.price) })}
      >
        <ProductThumb icon={card.icon} imageUrl={card.image_url} thumbnailUrl={card.thumbnail_url} category={card.category} alt="" size="lg" className="max-sm:size-20" />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              {card.brand && <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{card.brand}</p>}
              <h3 className="line-clamp-2 leading-snug font-semibold">{card.name}</h3>
            </div>
            <p className="shrink-0 text-right text-lg font-semibold tracking-tight tabular">{formatPrice(best.price)}</p>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <StockPill status={best.stock_status} quantity={best.quantity} />
            {freshness?.label ? <FreshnessLabel freshness={freshness} /> : <Freshness at={best.stock_updated_at} />}
          </div>
          <div className="mt-3 flex items-center gap-2 text-sm">
            <Store className="size-4 shrink-0 text-muted-foreground" />
            <span className="truncate font-medium">{best.shop.name}</span>
            <span className="shrink-0 text-muted-foreground">·</span>
            <Distance km={best.shop.distance_km} className="shrink-0 text-sm" />
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <Rating value={best.shop.rating_avg} count={best.shop.rating_count} />
          </div>
        </div>
      </Link>
    </article>
  );
}
