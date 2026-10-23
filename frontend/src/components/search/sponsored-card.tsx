"use client";

import { Store } from "lucide-react";
import Link from "next/link";
import type { MouseEvent } from "react";

import { Distance, Freshness, Rating, StockPill } from "@/components/common/indicators";
import { ProductThumb } from "@/components/common/product-thumb";
import { useI18n } from "@/components/providers";
import { FreshnessLabel, type StockFreshness } from "@/components/search/result-card";
import { formatPrice } from "@/lib/format";
import type { SponsoredCard as Sponsored } from "@/lib/types";

// The sponsored card is its own module so the search view can load it only when a results page carries one: almost
// no search does, and the route's first-load JavaScript (P2.9) does not pay for it.

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
