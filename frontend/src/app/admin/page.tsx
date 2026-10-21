"use client";

import { useQuery } from "@tanstack/react-query";
import { ClipboardList, Clock3, Database, IndianRupee, Info, Package, Plus, Scale, Search, Store, Users } from "lucide-react";
import Link from "next/link";
import { Bar, BarChart, CartesianGrid, XAxis } from "recharts";

import { ErrorState, ListSkeleton } from "@/components/common/states";
import { RESERVATION_LABELS, ORDER_LABELS } from "@/components/common/status";
import { PageTitle, StatTile } from "@/components/dashboard/dashboard-shell";
import { SalesChart } from "@/components/dashboard/sales-chart";
import { type ChartConfig, ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { api } from "@/lib/api";
import { formatCompactPrice, formatNumber, formatPrice } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { AdminOverview } from "@/lib/types";

const activityConfig = {
  searches: { label: "Searches", color: "var(--chart-2)" },
  inventory_updates: { label: "Stock updates", color: "var(--chart-1)" },
} satisfies ChartConfig;

function StatusBars({ data, labels }: { data: Record<string, number>; labels: Record<string, { label: string }> }) {
  const total = Object.values(data).reduce((a, b) => a + b, 0) || 1;
  return (
    <ul className="space-y-2">
      {Object.entries(data).sort((a, b) => b[1] - a[1]).map(([k, v]) => (
        <li key={k} className="text-sm">
          <div className="flex justify-between"><span>{labels[k]?.label ?? k}</span><span className="text-muted-foreground tabular">{formatNumber(v)}</span></div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-chart-1" style={{ width: `${(v / total) * 100}%` }} /></div>
        </li>
      ))}
    </ul>
  );
}

/** GET /api/admin/storage: the latest daily storage snapshot against the Atlas tier's limit (P2.2). */
type StorageReport = {
  limit_mb: number;
  snapshot_day: string | null;
  taken_at: string | null;
  total_mb: number | null;
  data_mb: number | null;
  index_mb: number | null;
  used_pct: number | null;
  thresholds: { pct: number; mb: number }[];
  growth_mb_per_day: number | null;
  growth_days: number;
  projection: "projected" | "reached" | "not_growing" | "beyond_horizon" | "insufficient" | "no_data";
  projected_85_date: string | null;
  largest_collections: { name: string; documents: number; data_mb: number; index_mb: number; total_mb: number }[];
};

const mb = (value: number) => (value >= 1024 ? `${(value / 1024).toFixed(2)} GB` : `${value.toFixed(1)} MB`);
const day = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });

function projectionText(s: StorageReport) {
  switch (s.projection) {
    case "projected":
      return `Reaches 85% on ${day(s.projected_85_date!)}`;
    case "reached":
      return "Already above 85%: move to a larger tier or archive (runbook)";
    case "not_growing":
      return "Not growing over the last 30 days: no date";
    case "beyond_horizon":
      return "85% is more than 10 years away at this rate";
    case "insufficient":
      return "A projected date needs snapshots from two different days";
    default:
      return "No snapshot yet: the first is taken within a minute of the API starting";
  }
}

function StorageCard() {
  const q = useQuery({ queryKey: ["admin", "storage"], queryFn: () => api.get<StorageReport>("/admin/storage") });
  const s = q.data;
  const pct = s?.used_pct ?? 0;
  const [warn, alert] = s?.thresholds ?? [];
  const tone = alert && pct >= alert.pct ? "bg-destructive" : warn && pct >= warn.pct ? "bg-warning" : "bg-chart-1";
  return (
    <section className="mt-6 rounded-2xl border bg-card p-5" aria-labelledby="storage-heading">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="storage-heading" className="flex items-center gap-2 font-semibold"><Database className="size-4 text-muted-foreground" /> Database storage</h2>
        {s?.snapshot_day && <span className="text-xs text-muted-foreground">Snapshot of {day(s.snapshot_day)}</span>}
      </div>
      {q.isError ? (
        <p className="mt-3 text-sm text-muted-foreground">Storage figures could not be loaded. <button type="button" className="underline" onClick={() => q.refetch()}>Retry</button></p>
      ) : !s ? (
        <div className="mt-4 h-24 animate-pulse rounded-xl bg-muted" />
      ) : (
        <div className="mt-4 grid gap-6 lg:grid-cols-2">
          <div>
            <p className="text-sm">
              {s.total_mb === null ? "No snapshot yet" : <><span className="text-2xl font-semibold tabular">{mb(s.total_mb)}</span> <span className="text-muted-foreground">of {mb(s.limit_mb)} ({s.used_pct}%)</span></>}
            </p>
            <div className="relative mt-3 h-2.5 overflow-hidden rounded-full bg-muted" role="img" aria-label={`${pct}% of the storage limit used`}>
              <div className={cn("h-full rounded-full", tone)} style={{ width: `${Math.min(pct, 100)}%` }} />
              {s.thresholds.map((t) => <span key={t.pct} className="absolute inset-y-0 w-px bg-foreground/40" style={{ left: `${t.pct}%` }} />)}
            </div>
            <p className="mt-1.5 text-xs text-muted-foreground">Alerts at {s.thresholds.map((t) => `${t.pct}% (${mb(t.mb)})`).join(" and ")}</p>
            <dl className="mt-4 grid grid-cols-2 gap-3 text-sm">
              <div>
                <dt className="text-xs text-muted-foreground">Growth per day{s.growth_days ? `, last ${s.growth_days} days` : ""}</dt>
                <dd className="tabular">{s.growth_mb_per_day === null ? "Not enough data" : `${s.growth_mb_per_day >= 0 ? "+" : ""}${s.growth_mb_per_day.toFixed(2)} MB`}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Data and indexes</dt>
                <dd className="tabular">{s.data_mb === null || s.index_mb === null ? "–" : `${mb(s.data_mb)} + ${mb(s.index_mb)}`}</dd>
              </div>
            </dl>
            <p className={cn("mt-4 rounded-xl px-3 py-2 text-sm", s.projection === "reached" ? "bg-destructive/10 text-destructive" : "bg-info-soft text-info-ink")}>
              {projectionText(s)}
            </p>
          </div>
          <div>
            <h3 className="mb-2 text-sm font-medium">Largest collections</h3>
            {s.largest_collections.length === 0 ? <p className="text-sm text-muted-foreground">None recorded yet.</p> : (
              <ol className="space-y-2">
                {s.largest_collections.map((c) => (
                  <li key={c.name} className="text-sm">
                    <div className="flex justify-between gap-3"><span className="truncate font-mono text-xs">{c.name}</span><span className="shrink-0 text-muted-foreground tabular">{mb(c.total_mb)} · {formatNumber(c.documents)} docs</span></div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-chart-2" style={{ width: `${s.total_mb ? Math.min(100, (c.total_mb / s.total_mb) * 100) : 0}%` }} /></div>
                  </li>
                ))}
              </ol>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

/** GET /api/admin/freshness: stock freshness per live shop and the city median listing age, check 10.3's metric (P7.4). */
type FreshnessShop = {
  id: number;
  name: string;
  slug: string;
  locality: string | null;
  city: string | null;
  is_verified: boolean;
  in_city_metric: boolean;
  listings: number;
  median_age_hours: number | null;
  fresh_share: number | null;
  inventory_updated_at: string | null;
};
type FreshnessReport = {
  as_of: string;
  fresh_hours: number;
  stale_days: number;
  city: string;
  target_shops: number;
  live_shops: number;
  verified_live_shops: number;
  listings: number;
  city_median_age_hours: number | null;
  city_fresh_share: number | null;
  meets_check_10_3: boolean;
  shops: FreshnessShop[];
  stale_shops: FreshnessShop[];
};

const age = (hours: number | null) =>
  hours === null ? "No listings" : hours < 48 ? `${Math.round(hours)} h` : `${(hours / 24).toFixed(1)} days`;
const share = (value: number | null) => (value === null ? "–" : `${Math.round(value * 100)}%`);

function FreshnessCard() {
  const q = useQuery({ queryKey: ["admin", "freshness"], queryFn: () => api.get<FreshnessReport>("/admin/freshness") });
  const f = q.data;
  const medianMet = f && f.city_median_age_hours !== null && f.city_median_age_hours <= f.fresh_hours;
  return (
    <section className="mt-6 rounded-2xl border bg-card p-5" aria-labelledby="freshness-heading">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="freshness-heading" className="flex items-center gap-2 font-semibold"><Clock3 className="size-4 text-muted-foreground" /> Stock freshness</h2>
        {f && <span className="text-xs text-muted-foreground">{formatNumber(f.live_shops)} live shops · {formatNumber(f.verified_live_shops)} verified in {f.city} · {formatNumber(f.listings)} of their listings</span>}
      </div>
      {q.isError ? (
        <p className="mt-3 text-sm text-muted-foreground">Freshness figures could not be loaded. <button type="button" className="underline" onClick={() => q.refetch()}>Retry</button></p>
      ) : !f ? (
        <div className="mt-4 h-24 animate-pulse rounded-xl bg-muted" />
      ) : (
        <div className="mt-4 grid gap-6 lg:grid-cols-3">
          <div>
            <p className="text-xs text-muted-foreground">{f.city} median listing age (verified live shops)</p>
            <p className="mt-1 text-2xl font-semibold tabular">{age(f.city_median_age_hours)}</p>
            <p className="mt-1 text-xs text-muted-foreground">{share(f.city_fresh_share)} of listings confirmed within {f.fresh_hours} hours</p>
            <p className={cn("mt-3 rounded-xl px-3 py-2 text-sm", f.meets_check_10_3 ? "bg-success-soft text-success-ink" : "bg-warning-soft text-warning-ink")}>
              {f.meets_check_10_3
                ? `Check 10.3 met: ${formatNumber(f.verified_live_shops)} verified live shops and a median within ${f.fresh_hours} hours`
                : `Check 10.3 not met: median ${medianMet ? "within" : "above"} the ${f.fresh_hours}-hour target; ${formatNumber(f.verified_live_shops)} of ${formatNumber(f.target_shops)} verified live shops`}
            </p>
            <h3 className="mt-5 mb-2 text-sm font-medium">Shops beyond {f.stale_days} days</h3>
            {f.stale_shops.length === 0 ? <p className="text-sm text-muted-foreground">None.</p> : (
              <ul className="space-y-1.5">
                {f.stale_shops.map((s) => (
                  <li key={s.id} className="flex justify-between gap-3 text-sm">
                    <Link href={`/shops/${s.slug}`} className="min-w-0 truncate hover:underline">{s.name}</Link>
                    <span className="shrink-0 text-muted-foreground tabular">{age(s.median_age_hours)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="lg:col-span-2">
            <h3 className="mb-2 text-sm font-medium">Per shop, stalest first</h3>
            <div className="max-h-80 overflow-auto rounded-xl border">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-card text-xs text-muted-foreground">
                  <tr className="border-b">
                    <th scope="col" className="px-3 py-2 text-left font-medium">Shop</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Median age</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Within {f.fresh_hours} h</th>
                    <th scope="col" className="hidden px-3 py-2 text-right font-medium sm:table-cell">Listings</th>
                  </tr>
                </thead>
                <tbody>
                  {f.shops.map((s) => (
                    <tr key={s.id} className="border-b last:border-0">
                      <td className="max-w-48 truncate px-3 py-1.5"><Link href={`/shops/${s.slug}`} className="hover:underline">{s.name}</Link></td>
                      <td className={cn("px-3 py-1.5 text-right tabular", s.median_age_hours !== null && s.median_age_hours > f.stale_days * 24 && "text-warning-ink")}>{age(s.median_age_hours)}</td>
                      <td className="px-3 py-1.5 text-right tabular">{share(s.fresh_share)}</td>
                      <td className="hidden px-3 py-1.5 text-right tabular sm:table-cell">{formatNumber(s.listings)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

export default function AdminOverviewPage() {
  const q = useQuery({ queryKey: ["admin", "overview"], queryFn: () => api.get<AdminOverview>("/admin/overview") });
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  const d = q.data;
  if (!d) return <ListSkeleton rows={5} />;
  const t = d.totals;
  const activity = d.activity.map((a) => ({ ...a, label: new Date(`${a.date}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short" }) }));
  const maxCat = Math.max(...d.categories.map((c) => c.revenue_30d), 1);

  return (
    <>
      <PageTitle title="Platform overview" description="Everything happening across NearShop." />
      <p className="mb-6 flex items-center gap-2 rounded-xl bg-info-soft px-3 py-2 text-xs text-info-ink">
        <Info className="size-3.5 shrink-0" /> 30-day revenue {formatPrice(t.revenue_30d)}, of which {formatPrice(t.revenue_30d_real)} is from real transactions. The rest is seeded demo history.
      </p>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile label="Customers" value={formatNumber(t.customers)} icon={Users} />
        <StatTile label="Shops" value={formatNumber(t.shops)} hint={`${t.shops_verified} verified`} icon={Store} href="/admin/shops" />
        <StatTile label="Listings" value={formatNumber(t.listings)} hint={`${formatNumber(t.in_stock)} in stock`} icon={Package} />
        <StatTile label="Revenue, 30 days" value={formatCompactPrice(t.revenue_30d)} icon={IndianRupee} tone="brand" />
        <StatTile label="Reservations" value={formatNumber(t.reservations)} icon={ClipboardList} />
        <StatTile label="Delivery orders" value={formatNumber(t.orders)} icon={Package} />
        <StatTile label="Searches, 30 days" value={formatNumber(t.searches_30d)} icon={Search} />
        <StatTile label="Prices to review" value={t.open_price_flags} icon={Scale} tone="warning" href="/admin/anomalies" />
      </div>

      <FreshnessCard />

      <StorageCard />

      <div className="mt-6 grid gap-6 xl:grid-cols-2">
        <section className="rounded-2xl border bg-card p-5">
          <h2 className="font-semibold">Sales across all shops</h2>
          <SalesChart data={d.sales_series} className="mt-4 h-56 w-full" />
        </section>
        <section className="rounded-2xl border bg-card p-5">
          <h2 className="font-semibold">Activity, last 14 days</h2>
          <ChartContainer config={activityConfig} className="mt-4 h-56 w-full">
            <BarChart data={activity} accessibilityLayer>
              <CartesianGrid vertical={false} strokeDasharray="3 3" />
              <XAxis dataKey="label" tickLine={false} axisLine={false} minTickGap={20} />
              <ChartTooltip content={<ChartTooltipContent />} />
              <ChartLegend content={<ChartLegendContent />} />
              <Bar dataKey="searches" fill="var(--color-searches)" radius={[4, 4, 0, 0]} />
              <Bar dataKey="inventory_updates" fill="var(--color-inventory_updates)" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ChartContainer>
        </section>
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        <section className="rounded-2xl border bg-card p-5">
          <h2 className="mb-4 font-semibold">Reservations by status</h2>
          <StatusBars data={d.reservations_by_status} labels={RESERVATION_LABELS} />
        </section>
        <section className="rounded-2xl border bg-card p-5">
          <h2 className="mb-4 font-semibold">Deliveries by status</h2>
          <StatusBars data={d.orders_by_status} labels={ORDER_LABELS} />
        </section>
        <section className="rounded-2xl border bg-card p-5">
          <h2 className="mb-4 font-semibold">Categories, 30 days</h2>
          <ul className="space-y-2.5">
            {d.categories.map((c) => (
              <li key={c.slug} className="text-sm">
                <div className="flex justify-between"><span>{c.name}</span><span className="text-muted-foreground tabular">{formatCompactPrice(c.revenue_30d)}</span></div>
                <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
                  <div className="h-full rounded-full" style={{ width: `${(c.revenue_30d / maxCat) * 100}%`, background: `oklch(0.65 0.13 ${c.color_hue})` }} />
                </div>
              </li>
            ))}
          </ul>
        </section>
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        <section className="rounded-2xl border bg-card p-5 lg:col-span-1">
          <h2 className="mb-3 font-semibold">Top shops, 30 days</h2>
          <ol className="space-y-2">
            {d.top_shops.map((s, i) => (
              <li key={s.id} className="flex items-center gap-3 text-sm">
                <span className="w-4 text-muted-foreground tabular">{i + 1}</span>
                <Link href={`/shops/${s.slug}`} className="min-w-0 flex-1 truncate hover:underline">{s.name}</Link>
                <span className="text-muted-foreground tabular">{formatCompactPrice(s.revenue_30d)}</span>
              </li>
            ))}
          </ol>
        </section>
        <section className="rounded-2xl border bg-card p-5">
          <h2 className="mb-3 font-semibold">Popular searches</h2>
          <ol className="space-y-2">
            {d.popular_searches.map((p) => (
              <li key={p.query} className="flex justify-between gap-3 text-sm">
                <Link href={`/search?q=${encodeURIComponent(p.query)}`} className="truncate capitalize hover:underline">{p.query}</Link>
                <span className="shrink-0 text-muted-foreground tabular">{p.count}</span>
              </li>
            ))}
          </ol>
        </section>
        <section className="rounded-2xl border bg-card p-5">
          <h2 className="mb-1 font-semibold">Searches with no results</h2>
          <p className="mb-3 text-xs text-muted-foreground">Recruit shops or categories that meet this demand, or add a synonym when the item is listed under another name.</p>
          <ul className="flex flex-wrap gap-2">
            {d.zero_result_searches.map((z) => (
              <li key={z.query} className="flex items-center gap-1 rounded-full border py-1 pr-1 pl-3 text-sm">
                <span className="capitalize">{z.query}</span> <span className="text-muted-foreground tabular">· {z.count}</span>
                <Link href={`/admin/synonyms?term=${encodeURIComponent(z.query)}`} className="ml-1 inline-flex items-center gap-0.5 rounded-full bg-muted px-2 py-0.5 text-xs hover:bg-accent" aria-label={`Add synonym for ${z.query}`}>
                  <Plus className="size-3" /> Add synonym
                </Link>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </>
  );
}
