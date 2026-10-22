"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, Bike, CalendarDays, ClipboardList, Info, PackageCheck, RefreshCw, Star, TrendingDown, TrendingUp } from "lucide-react";
import { AnimatePresence } from "motion/react";
import Link from "next/link";

import { Freshness, StockPill } from "@/components/common/indicators";
import { ProductThumb } from "@/components/common/product-thumb";
import { ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle, StatTile } from "@/components/dashboard/dashboard-shell";
import { SalesChart } from "@/components/dashboard/sales-chart";
import { OwnerOrderCard, OwnerReservationCard } from "@/components/owner/queue-cards";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatNumber, formatPrice } from "@/lib/format";
import { plural } from "@/lib/i18n";
import type { OwnerOverview, OwnerStatements } from "@/lib/types";

import { formatMonth, LeadCountList } from "./owner-shell";

export default function OwnerOverviewPage() {
  const { t, language } = useI18n();
  const count = (n: number) => ({ count: formatNumber(n) });
  const q = useQuery({ queryKey: ["owner", "overview"], queryFn: () => api.get<OwnerOverview>("/owner/overview"), refetchInterval: 15_000 });
  // This month so far (P9.9 lead counts): the same counts the statement on the 1st will state.
  const statements = useQuery({ queryKey: ["owner", "statements"], queryFn: () => api.get<OwnerStatements>("/owner/statements") });
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  const d = q.data;
  if (!d) return <ListSkeleton rows={4} />;

  const delta = d.revenue_prev_7d ? ((d.revenue_7d - d.revenue_prev_7d) / d.revenue_prev_7d) * 100 : null;
  const needsAction = d.reservation_requests.length + d.pending_orders.length;
  const runsOut = (days: number) => t(`owner.overview.runsOut.${plural(language, days)}`, count(days));
  const hour = new Date().getHours();
  const greeting = hour < 12 ? "morning" : hour < 17 ? "afternoon" : "evening";

  return (
    <>
      <PageTitle
        title={t(`owner.overview.greeting.${greeting}`)}
        description={
          <span className="inline-flex flex-wrap items-center gap-x-2">
            <Freshness at={d.inventory.updated_at} prefix={t("owner.overview.stockUpdated")} />
            <Link href="/shop/inventory" className="font-medium text-foreground underline underline-offset-4">{t("owner.overview.updateNow")}</Link>
          </span>
        }
        action={<Button asChild variant="outline"><Link href={`/shops/${d.shop.slug}`}>{t("owner.overview.viewAsCustomer")} <ArrowRight /></Link></Button>}
      />

      {d.includes_demo_data && (
        <p className="mb-6 flex items-center gap-2 rounded-xl bg-info-soft px-3 py-2 text-xs text-info-ink">
          <Info className="size-3.5 shrink-0" /> {t("owner.overview.demoData")}
        </p>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile label={t("owner.overview.revenueToday")} value={formatPrice(d.today.revenue)}
          hint={t(`owner.overview.unitsSold.${plural(language, d.today.units)}`, count(d.today.units))} icon={TrendingUp} tone="brand" />
        <StatTile
          label={t("owner.overview.last7Days")}
          value={formatPrice(d.revenue_7d)}
          hint={delta !== null ? <span className={delta >= 0 ? "text-success-ink" : "text-destructive"}>{t("owner.overview.vsPreviousWeek", { delta: `${delta >= 0 ? "+" : ""}${delta.toFixed(0)}` })}</span> : undefined}
          icon={delta !== null && delta < 0 ? TrendingDown : TrendingUp}
        />
        <StatTile label={t("owner.overview.pickupsWaiting")} value={d.queue.pickups_waiting}
          hint={t(`owner.overview.newRequests.${plural(language, d.queue.reservation_requests)}`, count(d.queue.reservation_requests))}
          icon={PackageCheck} href="/shop/reservations" />
        <StatTile label={t("owner.overview.lowOrOut")} value={d.inventory.low_stock + d.inventory.out_of_stock}
          hint={t(`owner.overview.ofListings.${plural(language, d.inventory.listings)}`, count(d.inventory.listings))}
          icon={AlertTriangle} tone="warning" href="/shop/inventory?stock=low_stock" />
      </div>

      {statements.data && (
        <section className="mt-8 rounded-2xl border bg-card p-5" aria-labelledby="month-so-far-title" data-testid="month-so-far">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 id="month-so-far-title" className="flex items-center gap-2 text-lg font-semibold">
              <CalendarDays className="size-5 text-brand" /> {t("owner.overview.monthSoFar.title")}
              <span className="text-sm font-normal text-muted-foreground">· {formatMonth(statements.data.current.month, language)}</span>
            </h2>
            <Link href="/shop/statements" className="text-sm text-muted-foreground hover:text-foreground">{t("owner.overview.monthSoFar.statements")}</Link>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">{t("owner.overview.monthSoFar.hint")}</p>
          <LeadCountList counts={statements.data.current} className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5" />
        </section>
      )}

      <div className="mt-8 grid grid-cols-1 gap-8 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
        <section>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-lg font-semibold">
              {t("owner.overview.needsAttention")}
              {needsAction > 0 && <span className="grid size-6 place-items-center rounded-full bg-brand text-xs text-brand-foreground tabular">{needsAction}</span>}
            </h2>
            <Link href="/shop/reservations" className="text-sm text-muted-foreground hover:text-foreground">{t("owner.overview.allReservations")}</Link>
          </div>
          {needsAction === 0 ? (
            <div className="rounded-2xl border border-dashed p-8 text-center">
              <ClipboardList className="mx-auto size-6 text-muted-foreground" />
              <p className="mt-2 text-sm font-medium">{t("owner.overview.noRequestsTitle")}</p>
              <p className="text-sm text-muted-foreground">{t("owner.overview.noRequestsBody")}</p>
            </div>
          ) : (
            <ul className="space-y-3">
              <AnimatePresence>
                {d.reservation_requests.map((r) => <OwnerReservationCard key={`r${r.id}`} r={r} />)}
                {d.pending_orders.map((o) => <OwnerOrderCard key={`o${o.id}`} o={o} />)}
              </AnimatePresence>
            </ul>
          )}
          {d.queue.deliveries_in_progress > 0 && (
            <Link href="/shop/orders" className="mt-3 flex items-center justify-between rounded-2xl border bg-card p-4 text-sm hover:shadow-lift">
              <span className="inline-flex items-center gap-2"><Bike className="size-4 text-brand" />{" "}
                {t(`owner.overview.deliveriesInProgress.${plural(language, d.queue.deliveries_in_progress)}`, count(d.queue.deliveries_in_progress))}
              </span>
              <ArrowRight className="size-4" />
            </Link>
          )}
        </section>

        <section className="rounded-2xl border bg-card p-5">
          <div className="flex items-baseline justify-between">
            <h2 className="font-semibold">{t("owner.overview.sales30d")}</h2>
            <p className="text-sm text-muted-foreground tabular">{formatPrice(d.revenue_30d)}</p>
          </div>
          <SalesChart data={d.sales_series} className="mt-4 h-52 w-full" />
          <div className="mt-4 flex items-center gap-2 border-t pt-4 text-sm">
            <Star className="size-4 fill-warning text-warning" />
            <span className="font-medium">{d.rating.avg?.toFixed(1) ?? "-"}</span>
            <span className="text-muted-foreground">{t(`owner.overview.fromReviews.${plural(language, d.rating.count)}`, count(d.rating.count))}</span>
            <Link href="/shop/reviews" className="ml-auto text-muted-foreground hover:text-foreground">{t("owner.overview.readReviews")}</Link>
          </div>
        </section>
      </div>

      <div className="mt-8 grid grid-cols-1 gap-8 lg:grid-cols-2">
        <section>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-lg font-semibold">{t("owner.overview.runningLow")}</h2>
            <Link href="/shop/insights" className="text-sm text-muted-foreground hover:text-foreground">{t("owner.overview.restockPlan")}</Link>
          </div>
          {d.low_stock.length ? (
            <ul className="divide-y rounded-2xl border bg-card">
              {d.low_stock.map((p) => (
                <li key={p.id} className="flex items-center gap-3 p-3">
                  <ProductThumb icon={p.icon} imageUrl={p.image_url} thumbnailUrl={p.thumbnail_url} category={p.category} alt="" size="sm" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{p.name}</p>
                    <p className="text-xs text-muted-foreground">
                      {p.days_to_stockout !== null
                        ? runsOut(Math.max(0, Math.round(p.days_to_stockout)))
                        : t("owner.overview.noRecentSales")}
                      {p.recommended_restock ? ` · ${t("owner.overview.restock", count(p.recommended_restock))}` : ""}
                    </p>
                  </div>
                  <StockPill status={p.stock_status} quantity={p.quantity} compact />
                </li>
              ))}
            </ul>
          ) : (
            <p className="rounded-2xl border border-dashed p-6 text-center text-sm text-muted-foreground">{t("owner.overview.wellStocked")}</p>
          )}
        </section>
        <section>
          <h2 className="mb-3 text-lg font-semibold">{t("owner.overview.topSellers")}</h2>
          <ul className="divide-y rounded-2xl border bg-card">
            {d.top_products.map((p, i) => (
              <li key={p.id} className="flex items-center gap-3 p-3">
                <span className="w-5 text-center text-sm text-muted-foreground tabular">{i + 1}</span>
                <ProductThumb icon={p.icon} imageUrl={p.image_url} thumbnailUrl={p.thumbnail_url} category={p.category} alt="" size="sm" />
                <p className="min-w-0 flex-1 truncate text-sm font-medium">{p.name}</p>
                <span className="text-right text-sm tabular">
                  <span className="block font-medium">{formatPrice(p.revenue)}</span>
                  <span className="text-xs text-muted-foreground">{t("owner.overview.sold", count(p.units))}</span>
                </span>
              </li>
            ))}
            {!d.top_products.length && <li className="p-6 text-center text-sm text-muted-foreground">{t("owner.overview.noSales")}</li>}
          </ul>
        </section>
      </div>
      <p className="mt-8 flex items-center gap-1.5 text-xs text-muted-foreground"><RefreshCw className="size-3" /> {t("owner.overview.autoRefresh")}</p>
    </>
  );
}
