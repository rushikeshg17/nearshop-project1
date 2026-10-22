"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowDownRight, ArrowRight, ArrowUpRight, BrainCircuit, Info, PackagePlus, Scale, SearchX, TrendingUp, Users } from "lucide-react";
import Link from "next/link";

import { AppIcon } from "@/components/common/app-icon";
import { ProductThumb } from "@/components/common/product-thumb";
import { ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { useI18n } from "@/components/providers";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api } from "@/lib/api";
import { formatNumber, formatPrice, timeAgo } from "@/lib/format";
import { type MessageKey, plural } from "@/lib/i18n";
import type { ModelSummary, OwnerInsights } from "@/lib/types";
import { cn } from "@/lib/utils";

// Each module's model gate (P7.3, plan section 8.9): when shown is false, the module has no output and the
// owner sees the reason and the progress towards the threshold instead of an empty chart.
type GateProgress = { label: string; have: number; need: number };
type Gate = { shown: boolean; code: string | null; reason: string | null; progress: GateProgress[] };
type GateName = "demand" | "bundles" | "price_flags" | "area_demand" | "missed_demand";
type GatedInsights = OwnerInsights & {
  gates?: Partial<Record<GateName, Gate>>;
  search_rule?: { window_days: number; min_users: number };
};

// The progress labels the API sends (backend/app/services/analytics.py), in the interface language; a label not
// listed here shows as the API sends it. The gate's reason is the API's sentence (it carries the counts and
// windows), so it is shown as sent until the API localises it.
const PROGRESS_LABELS: Record<string, MessageKey> = {
  days: "owner.insights.gate.progress.days",
  "days with a sale": "owner.insights.gate.progress.saleDays",
  "units sold": "owner.insights.gate.progress.unitsSold",
  "% better than same as last week": "owner.insights.gate.progress.betterThanLastWeek",
  "multi-item purchases": "owner.insights.gate.progress.multiItemPurchases",
  "verified shops": "owner.insights.gate.progress.verifiedShops",
  "flags reviewed": "owner.insights.gate.progress.flagsReviewed",
  "% upheld": "owner.insights.gate.progress.upheld",
  people: "owner.insights.gate.progress.people",
};

function GateNotice({ gate, className }: { gate: Gate; className?: string }) {
  const { t } = useI18n();
  return (
    <div className={cn("rounded-xl bg-muted/60 p-4 text-sm", className)} role="status">
      <p className="flex gap-2 text-muted-foreground"><Info className="mt-0.5 size-4 shrink-0" /> <span>{gate.reason}</span></p>
      {gate.progress.length > 0 && (
        <ul className="mt-3 space-y-2">
          {gate.progress.map((p) => {
            const pct = p.need > 0 ? Math.min(100, Math.round((p.have / p.need) * 100)) : 0;
            return (
              <li key={p.label} className="text-xs">
                <span className="flex justify-between gap-3 text-muted-foreground">
                  <span>{PROGRESS_LABELS[p.label] ? t(PROGRESS_LABELS[p.label]) : p.label}</span>
                  <span className="tabular">{t("owner.insights.gate.haveOfNeed", { have: formatNumber(p.have), need: formatNumber(p.need) })}</span>
                </span>
                <span className="mt-1 block h-1.5 overflow-hidden rounded-full bg-background" aria-hidden="true">
                  <span className={cn("block h-full rounded-full", pct >= 100 ? "bg-success" : "bg-brand")} style={{ width: `${pct}%` }} />
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function ModelNote({ m, children }: { m?: ModelSummary; children: React.ReactNode }) {
  const { t, language } = useI18n();
  if (!m) return null;
  return (
    <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
      <BrainCircuit className="size-3.5 text-brand" /> {t("owner.insights.modelNote", { algorithm: m.algorithm, ago: timeAgo(m.trained_at, undefined, language) })} · {children}
      {m.uses_demo_data && <Badge variant="outline" className="h-5 text-[10px]">{t("owner.insights.demoData")}</Badge>}
    </p>
  );
}

export default function InsightsPage() {
  const { t, language } = useI18n();
  const q = useQuery({ queryKey: ["owner", "insights"], queryFn: () => api.get<GatedInsights>("/owner/insights") });
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  const d = q.data;
  if (!d) return <ListSkeleton rows={5} />;
  const demand = d.models.demand;
  const daysLabel = (n: number) => t(`owner.insights.restock.days.${plural(language, n)}`, { count: formatNumber(n) });
  const gates = d.gates ?? {};
  const closed = (name: GateName): Gate | null => {
    const g = gates[name];
    return g && !g.shown ? g : null;
  };
  const [demandGate, areaGate, missedGate, bundleGate, flagGate] = (["demand", "area_demand", "missed_demand", "bundles", "price_flags"] as const).map(closed);
  const dm = demand?.metrics as { mae_7d?: number; naive_mae_7d?: number; improvement_pct?: number } | undefined;

  return (
    <>
      <PageTitle title={t("owner.insights.title")} description={t("owner.insights.description")} />

      <section className="rounded-2xl border bg-card">
        <div className="border-b p-5">
          <h2 className="flex items-center gap-2 text-lg font-semibold"><TrendingUp className="size-5 text-brand" /> {t("owner.insights.restock.title")}</h2>
          <ModelNote m={demand}>
            {dm?.mae_7d !== undefined && t("owner.insights.restock.error", { mae: formatNumber(dm.mae_7d), pct: formatNumber(dm.improvement_pct ?? 0) })}
          </ModelNote>
        </div>
        {demandGate ? (
          <GateNotice gate={demandGate} className="m-5" />
        ) : d.restock.length ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("owner.insights.restock.product")}</TableHead>
                <TableHead className="text-right">{t("owner.insights.restock.inStock")}</TableHead>
                <TableHead className="text-right">{t("owner.insights.restock.next7")}</TableHead>
                <TableHead className="text-right">{t("owner.insights.restock.runsOut")}</TableHead>
                <TableHead className="text-right">{t("owner.insights.restock.suggested")}</TableHead>
                <TableHead className="hidden md:table-cell">{t("owner.insights.restock.trend")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {d.restock.map((r) => (
                <TableRow key={r.id}>
                  <TableCell>
                    <Link href={`/shop/inventory?focus=${r.id}`} className="flex items-center gap-2 hover:underline">
                      <ProductThumb icon={r.icon} imageUrl={r.image_url} thumbnailUrl={r.thumbnail_url} category={r.category} alt="" size="sm" className="size-8 [&_svg]:size-4" />
                      <span className="max-w-60 truncate">{r.name}</span>
                    </Link>
                  </TableCell>
                  <TableCell className="text-right tabular">{formatNumber(r.quantity)}</TableCell>
                  <TableCell className="text-right tabular">~{formatNumber(Math.round(r.predicted_7d))}</TableCell>
                  <TableCell className={cn("text-right tabular", (r.days_to_stockout ?? 99) < 4 && "font-semibold text-destructive")}>
                    {r.days_to_stockout === null
                      ? "-"
                      : r.quantity === 0
                        ? t("owner.insights.restock.outNow")
                        : daysLabel(Math.max(0, Math.round(r.days_to_stockout)))}
                  </TableCell>
                  <TableCell className="text-right font-medium tabular">{r.recommended_restock ? formatNumber(r.recommended_restock) : "-"}</TableCell>
                  <TableCell className="hidden md:table-cell">
                    <span className={cn("inline-flex items-center gap-1 text-xs", r.trend === "rising" ? "text-success-ink" : r.trend === "falling" ? "text-muted-foreground" : "")}>
                      {r.trend === "rising" ? <ArrowUpRight className="size-3.5" /> : r.trend === "falling" ? <ArrowDownRight className="size-3.5" /> : null}
                      {t("owner.insights.restock.trendLine", { trend: t(`owner.insights.restock.trends.${r.trend}`), confidence: t(`owner.insights.restock.confidence.${r.confidence}`) })}
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <p className="p-6 text-sm text-muted-foreground">{t("owner.insights.restock.empty")}</p>
        )}
      </section>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <section className="rounded-2xl border bg-card p-5">
          <h2 className="flex items-center gap-2 font-semibold"><Users className="size-4 text-brand" /> {t("owner.insights.area.title")}</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            {t("owner.insights.area.hint", {
              days: formatNumber(d.search_rule?.window_days ?? 30),
              people: formatNumber(d.search_rule?.min_users ?? 5),
            })}
          </p>
          {areaGate ? <GateNotice gate={areaGate} className="mt-4" /> : (
          <ul className="mt-4 space-y-2">
            {d.area_demand.map((a) => (
              <li key={a.query} className="flex items-center justify-between gap-3 text-sm">
                <span className="truncate capitalize">{a.query}</span>
                <span className="flex shrink-0 items-center gap-2">
                  <span className="text-muted-foreground tabular">{t(`owner.insights.area.searches.${plural(language, a.searches)}`, { count: formatNumber(a.searches) })}</span>
                  {a.you_stock_it ? <Badge variant="secondary">{t("owner.insights.area.youStock")}</Badge> : <Badge className="bg-warning-soft text-warning-ink">{t("owner.insights.area.opportunity")}</Badge>}
                </span>
              </li>
            ))}
            {!d.area_demand.length && <li className="text-sm text-muted-foreground">{t("owner.insights.area.empty")}</li>}
          </ul>
          )}
        </section>

        <section className="rounded-2xl border bg-card p-5">
          <h2 className="flex items-center gap-2 font-semibold"><SearchX className="size-4 text-brand" /> {t("owner.insights.missed.title")}</h2>
          <p className="mt-1 text-xs text-muted-foreground">{t("owner.insights.missed.hint")}</p>
          {missedGate ? <GateNotice gate={missedGate} className="mt-4" /> : (
          <ul className="mt-4 flex flex-wrap gap-2">
            {d.missed_demand.map((m) => (
              <li key={m.query} className="rounded-full border px-3 py-1 text-sm capitalize">{m.query} <span className="text-muted-foreground tabular">· {formatNumber(m.searches)}</span></li>
            ))}
            {!d.missed_demand.length && <li className="text-sm text-muted-foreground">{t("owner.insights.missed.empty")}</li>}
          </ul>
          )}
        </section>

        <section className="rounded-2xl border bg-card p-5">
          <h2 className="flex items-center gap-2 font-semibold"><PackagePlus className="size-4 text-brand" /> {t("owner.insights.bundles.title")}</h2>
          <ModelNote m={d.models.recommendations}>{t("owner.insights.bundles.note")}</ModelNote>
          {bundleGate ? <GateNotice gate={bundleGate} className="mt-4" /> : (
          <ul className="mt-4 divide-y">
            {d.bundle_gaps.map((g) => (
              <li key={g.item.id} className="flex items-center gap-3 py-2.5 first:pt-0">
                <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted"><AppIcon name={g.item.icon} className="size-4" /></span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">{g.item.name}</p>
                  <p className="truncate text-xs text-muted-foreground">
                    {t("owner.insights.bundles.alsoBuy", { pct: formatNumber(Math.round(g.confidence * 100)), item: g.because_of })}
                  </p>
                </div>
                {g.item.typical_price && <span className="text-xs text-muted-foreground tabular">{formatPrice(g.item.typical_price)}</span>}
              </li>
            ))}
            {!d.bundle_gaps.length && <li className="text-sm text-muted-foreground">{t("owner.insights.bundles.empty")}</li>}
          </ul>
          )}
          {d.bundle_gaps.length > 0 && (
            <Button asChild variant="outline" size="sm" className="mt-3"><Link href="/shop/catalog">{t("owner.insights.bundles.addFromCatalogue")} <ArrowRight /></Link></Button>
          )}
        </section>

        <section className="rounded-2xl border bg-card p-5">
          <h2 className="flex items-center gap-2 font-semibold"><Scale className="size-4 text-brand" /> {t("owner.insights.prices.title")}</h2>
          <ModelNote m={d.models.anomalies}>{t("owner.insights.prices.note")}</ModelNote>
          {flagGate ? <GateNotice gate={flagGate} className="mt-4" /> : (
          <ul className="mt-4 divide-y">
            {d.price_flags.map((f) => (
              <li key={f.id} className="flex items-center gap-3 py-2.5 first:pt-0">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">{f.product.name}</p>
                  <p className="text-xs text-muted-foreground">
                    {t(`owner.insights.prices.line.${plural(language, f.peer_count)}`, {
                      price: formatPrice(f.price),
                      reference: formatPrice(f.reference_price),
                      count: formatNumber(f.peer_count),
                    })}
                  </p>
                </div>
                <Badge className={f.direction === "high" ? "bg-warning-soft text-warning-ink" : "bg-info-soft text-info-ink"}>
                  {f.deviation_pct > 0 ? "+" : ""}{f.deviation_pct}%
                </Badge>
              </li>
            ))}
            {!d.price_flags.length && <li className="text-sm text-muted-foreground">{t("owner.insights.prices.empty")}</li>}
          </ul>
          )}
          <p className="mt-3 flex gap-1.5 text-xs text-muted-foreground"><Info className="mt-0.5 size-3.5 shrink-0" /> {t("owner.insights.prices.footnote")}</p>
        </section>
      </div>
    </>
  );
}
