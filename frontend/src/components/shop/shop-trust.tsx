"use client";

import { Check, Clock3, PackageCheck, RefreshCw, Star, Truck } from "lucide-react";

import { useI18n } from "@/components/providers";
import { useNow } from "@/hooks/use-debounce";
import { formatNumber, timeAgo } from "@/lib/format";
import { plural } from "@/lib/i18n";
import type { Reliability } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Factual trust signals. Every number is something the customer can understand, no opaque score. */
export function ShopTrust({ r, className }: { r: Reliability; className?: string }) {
  const { t, language } = useI18n();
  const now = useNow(60_000);
  const purchases = r.rating_count
    ? t(
        r.accuracy_avg
          ? `discovery.trust.purchasesAccuracy.${plural(language, r.rating_count)}`
          : `discovery.trust.purchases.${plural(language, r.rating_count)}`,
        { count: formatNumber(r.rating_count), accuracy: r.accuracy_avg ? r.accuracy_avg.toFixed(1) : "" },
      )
    : t("discovery.trust.reviewsCompletedOnly");
  const facts = [
    {
      key: "inventory",
      icon: RefreshCw,
      label: t("discovery.trust.inventoryUpdated"),
      value: r.inventory_updated_at ? timeAgo(r.inventory_updated_at, now, language) : t("discovery.trust.notYet"),
      sub: r.inventory_update_days_7d
        ? t("discovery.trust.updateDays", { count: r.inventory_update_days_7d })
        : t("discovery.trust.noUpdatesWeek"),
      good: r.frequently_updated,
    },
    {
      key: "confirmed",
      icon: PackageCheck,
      label: t("discovery.trust.reservationsConfirmed"),
      value: r.reservation_requests_90d
        ? t("discovery.trust.confirmedOf", {
            confirmed: formatNumber(r.reservations_confirmed_90d),
            requests: formatNumber(r.reservation_requests_90d),
          })
        : t("discovery.trust.newShop"),
      sub:
        r.acceptance_rate !== null
          ? t("discovery.trust.acceptance", { percent: Math.round(r.acceptance_rate * 100) })
          : t("discovery.trust.noRequests"),
      good: (r.acceptance_rate ?? 0) >= 0.9,
    },
    {
      key: "response",
      icon: Clock3,
      label: t("discovery.trust.typicalConfirmation"),
      value:
        r.median_response_minutes !== null
          ? t("discovery.trust.minutes", { count: Math.max(1, r.median_response_minutes) })
          : t("discovery.trust.noData"),
      sub: t("discovery.trust.confirmationSub"),
      good: (r.median_response_minutes ?? 99) <= 10,
    },
    {
      key: "rating",
      icon: Star,
      label: t("discovery.trust.rating"),
      value: r.rating_avg ? t("discovery.trust.ratingValue", { rating: r.rating_avg.toFixed(1) }) : t("discovery.trust.newShop"),
      sub: !r.rating_avg ? t("discovery.trust.ratingPending") : purchases,
      good: (r.rating_avg ?? 0) >= 4.3,
    },
  ];
  return (
    <div className={cn("space-y-4", className)}>
      {r.highlights.length > 0 && (
        <ul className="flex flex-wrap gap-2">
          {r.highlights.map((h) => (
            <li key={h} className="inline-flex items-center gap-1.5 rounded-full bg-success-soft px-3 py-1 text-xs font-medium text-success-ink">
              <Check className="size-3.5" /> {h}
            </li>
          ))}
        </ul>
      )}
      <dl className="grid grid-cols-2 gap-3">
        {facts.map((f) => (
          <div key={f.key} className="rounded-xl border bg-card p-3">
            <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <f.icon className={cn("size-3.5", f.good && "text-success")} /> {f.label}
            </dt>
            <dd className="mt-1 text-sm font-semibold tabular" suppressHydrationWarning>{f.value}</dd>
            <dd className="text-xs text-muted-foreground">{f.sub}</dd>
          </div>
        ))}
      </dl>
      {(r.orders_delivered_90d > 0 || r.shop_cancellations_90d > 0) && (
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Truck className="size-3.5" />
          {t("discovery.trust.fulfilment", {
            delivered: formatNumber(r.orders_delivered_90d),
            cancelled: formatNumber(r.shop_cancellations_90d),
          })}
        </p>
      )}
    </div>
  );
}
