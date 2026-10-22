"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, BrainCircuit, Database, Filter, Loader2, MapPin, RefreshCw, Search, Sparkles, SplitSquareHorizontal } from "lucide-react";
import { motion, MotionConfig } from "motion/react";
import { useState } from "react";
import { toast } from "sonner";

import { AppIcon } from "@/components/common/app-icon";
import { useI18n } from "@/components/providers";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useMe } from "@/hooks/use-session";
import { api } from "@/lib/api";
import { formatNumber, timeAgo } from "@/lib/format";
import type { Translate } from "@/lib/i18n";
import type { AiModel, SearchCompare } from "@/lib/types";
import { cn } from "@/lib/utils";

// Example queries: what people type, compared as typed, so they are not translated.
const PRESETS = ["phone charging adapter", "bathroom tap leaking", "chappal", "rainy season", "bike oil change", "school reopening"];

const PIPELINE = [
  { icon: Search, step: "understand" },
  { icon: Sparkles, step: "match" },
  { icon: MapPin, step: "locate" },
  { icon: Filter, step: "filter" },
  { icon: SplitSquareHorizontal, step: "rank" },
] as const;

function metricLines(m: AiModel, t: Translate): [string, string][] {
  const x = m.metrics as Record<string, number | null | Record<string, number>>;
  const units = (v: unknown) => (v !== undefined ? t("aiLab.metrics.units", { value: String(v) }) : "-");
  const percent = (v: unknown) => (v !== undefined ? t("aiLab.metrics.percent", { value: String(v) }) : "-");
  switch (m.name) {
    case "embeddings":
      return [
        [t("aiLab.metrics.listingsIndexed"), formatNumber(Number(x.listings_indexed ?? 0))],
        [t("aiLab.metrics.vectorSize"), String(x.dimensions ?? "-")],
      ];
    case "word2vec":
      return [
        [t("aiLab.metrics.trainingSentences"), formatNumber(Number(x.sentences ?? 0))],
        [t("aiLab.metrics.vocabulary"), formatNumber(Number(x.vocabulary ?? 0))],
      ];
    case "demand":
      return [
        [t("aiLab.metrics.mae"), units(x.mae_7d)],
        [t("aiLab.metrics.naiveMae"), units(x.naive_mae_7d)],
        [t("aiLab.metrics.betterThanBaseline"), percent(x.improvement_pct)],
        [t("aiLab.metrics.r2"), String(x.r2 ?? "-")],
      ];
    case "recommendations":
      return [
        [t("aiLab.metrics.baskets"), formatNumber(Number(x.baskets ?? 0))],
        [t("aiLab.metrics.rules"), String(x.rules ?? 0)],
        [t("aiLab.metrics.avgConfidence"), x.avg_confidence ? percent(Math.round(Number(x.avg_confidence) * 100)) : "-"],
        [t("aiLab.metrics.avgLift"), String(x.avg_lift ?? "-")],
      ];
    case "anomalies":
      return [
        [t("aiLab.metrics.listingsCompared"), formatNumber(Number(x.listings_checked ?? 0))],
        [t("aiLab.metrics.itemGroups"), String(x.item_groups ?? 0)],
        [t("aiLab.metrics.isolated"), String(x.isolated_by_forest ?? 0)],
        [t("aiLab.metrics.flagged"), String(x.flagged_for_review ?? 0)],
      ];
    default:
      return [];
  }
}

export function AiLabView() {
  const qc = useQueryClient();
  const { data: me } = useMe();
  const { t, language } = useI18n();
  const [input, setInput] = useState("phone charging adapter");
  const [query, setQuery] = useState("phone charging adapter");

  const models = useQuery({
    queryKey: ["ai", "models"],
    queryFn: () => api.get<{ training: boolean; queued?: boolean; models: AiModel[] }>("/ai/models"),
    // Every 3 s while a retrain runs; once a minute while one waits for the retrain job (up to 30 minutes).
    refetchInterval: (q) => (q.state.data?.training ? 3000 : q.state.data?.queued ? 60000 : false),
  });
  const compare = useQuery({
    queryKey: ["ai", "compare", query],
    queryFn: () => api.get<SearchCompare>("/ai/search-compare", { q: query }),
    enabled: query.length >= 2,
  });
  const retrain = useMutation({
    mutationFn: () => api.post<{ queued: boolean; message?: string }>("/ai/retrain"),
    onSuccess: (r) => {
      toast.info(r.message ?? t("aiLab.models.retrainQueued"));
      qc.invalidateQueries({ queryKey: ["ai", "models"] });
    },
  });

  const demandFeatures = (models.data?.models.find((m) => m.name === "demand")?.metrics.feature_importance ?? {}) as Record<string, number>;

  return (
    <MotionConfig reducedMotion="user">
      <div className="mx-auto max-w-7xl px-4 pt-10 md:px-6 md:pt-14">
        <div className="max-w-3xl">
          <Badge variant="outline" className="gap-1.5"><BrainCircuit className="size-3.5 text-brand" /> {t("aiLab.badge")}</Badge>
          <h1 className="mt-4 text-4xl font-semibold md:text-5xl">{t("aiLab.title")}</h1>
          <p className="mt-4 text-lg text-muted-foreground">{t("aiLab.intro")}</p>
        </div>

        {/* ------------------------------------------------------------ search comparison */}
        <section className="mt-12">
          <h2 className="text-2xl font-semibold">{t("aiLab.compare.title")}</h2>
          <p className="mt-1 text-muted-foreground">{t("aiLab.compare.intro")}</p>
          <form className="mt-5 flex max-w-xl gap-2" onSubmit={(e) => { e.preventDefault(); setQuery(input.trim()); }}>
            <Input value={input} onChange={(e) => setInput(e.target.value)} className="h-11" aria-label={t("aiLab.compare.query")} />
            <Button type="submit" size="lg" className="h-11">{t("aiLab.compare.submit")}</Button>
          </form>
          <div className="mt-3 flex flex-wrap gap-2">
            {PRESETS.map((p) => (
              <button key={p} onClick={() => { setInput(p); setQuery(p); }}
                className={cn("rounded-full border px-3 py-1 text-sm transition-colors", query === p ? "border-primary bg-primary text-primary-foreground" : "bg-card hover:bg-accent")}>{p}</button>
            ))}
          </div>
          <div className="mt-6 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            {(compare.data?.methods ?? Array.from({ length: 4 }).map(() => null)).map((m, i) =>
              m ? (
                <motion.div key={m.key} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.06 }}
                  className={cn("rounded-2xl border bg-card p-4", m.key === "hybrid" && "border-brand/40 ring-4 ring-brand/5")}>
                  <p className="font-semibold">{m.title}</p>
                  <p className="line-clamp-2 min-h-8 text-xs text-muted-foreground">{m.note}</p>
                  <ol className="mt-3 space-y-2">
                    {m.results.map((r, j) => (
                      <li key={`${r.name}-${j}`} className="flex items-center gap-2 text-sm">
                        <span className="grid size-6 shrink-0 place-items-center rounded-md bg-muted"><AppIcon name={r.icon} className="size-3.5" /></span>
                        <span className="min-w-0 flex-1 truncate">{r.name}</span>
                        <span className="text-xs text-muted-foreground tabular">{r.score.toFixed(2)}</span>
                      </li>
                    ))}
                    {!m.results.length && <li className="py-4 text-center text-sm text-muted-foreground">{t("aiLab.compare.noResults")}</li>}
                  </ol>
                </motion.div>
              ) : (
                <Skeleton key={i} className="h-72 rounded-2xl" />
              ),
            )}
          </div>
          <ol className="mt-8 grid gap-3 md:grid-cols-5">
            {PIPELINE.map((s, i) => (
              <li key={s.step} className="relative rounded-2xl border bg-card p-4">
                <s.icon className="size-5 text-brand" />
                <p className="mt-3 text-sm font-semibold">
                  {t("aiLab.pipeline.step", { number: formatNumber(i + 1), title: t(`aiLab.pipeline.${s.step}.title`) })}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">{t(`aiLab.pipeline.${s.step}.body`)}</p>
                {i < PIPELINE.length - 1 && <ArrowRight className="absolute top-1/2 -right-2.5 hidden size-4 -translate-y-1/2 text-muted-foreground md:block" />}
              </li>
            ))}
          </ol>
        </section>

        {/* ------------------------------------------------------------ model cards */}
        <section className="mt-16">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <h2 className="text-2xl font-semibold">{t("aiLab.models.title")}</h2>
              <p className="mt-1 text-muted-foreground">{t("aiLab.models.intro")}</p>
            </div>
            {me?.role === "admin" && (
              <Button onClick={() => retrain.mutate()} disabled={retrain.isPending || models.data?.training || models.data?.queued}>
                {models.data?.training || models.data?.queued ? <Loader2 className="animate-spin" /> : <RefreshCw />}{" "}
                {t(models.data?.training ? "aiLab.models.training" : models.data?.queued ? "aiLab.models.queued" : "aiLab.models.retrain")}
              </Button>
            )}
          </div>
          <div className="mt-6 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {(models.data?.models ?? Array.from({ length: 5 }).map(() => null)).map((m, i) =>
              m ? (
                <article key={m.name} className="flex flex-col rounded-2xl border bg-card p-5">
                  <div className="flex items-start justify-between gap-3">
                    <h3 className="font-semibold">{m.title}</h3>
                    {m.uses_demo_data && <Badge variant="outline" className="shrink-0 text-[10px]">{t("aiLab.models.demoData")}</Badge>}
                  </div>
                  <p className="mt-1 text-sm text-muted-foreground">{m.purpose}</p>
                  <p className="mt-3 font-mono text-xs text-brand-ink">{m.algorithm ?? t("aiLab.models.notTrained")}</p>
                  <dl className="mt-4 grid grid-cols-2 gap-2">
                    {metricLines(m, t).map(([k, v]) => (
                      <div key={k} className="rounded-xl bg-muted/60 p-2.5">
                        <dt className="text-[11px] text-muted-foreground">{k}</dt>
                        <dd className="text-sm font-semibold tabular">{v}</dd>
                      </div>
                    ))}
                  </dl>
                  {m.name === "demand" && Object.keys(demandFeatures).length > 0 && (
                    <div className="mt-4">
                      <p className="text-xs font-medium text-muted-foreground">{t("aiLab.models.drivers")}</p>
                      <ul className="mt-2 space-y-1">
                        {Object.entries(demandFeatures).slice(0, 5).map(([f, v]) => (
                          <li key={f} className="flex items-center gap-2 text-xs">
                            <span className="w-24 shrink-0 font-mono">{f}</span>
                            <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted"><span className="block h-full rounded-full bg-brand" style={{ width: `${v * 100}%` }} /></span>
                            <span className="w-9 text-right tabular text-muted-foreground">{Math.round(v * 100)}%</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <p className="mt-auto flex items-center gap-1.5 pt-4 text-xs text-muted-foreground">
                    <Database className="size-3.5" /> {m.note}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground" suppressHydrationWarning>
                    {m.trained_at
                      ? t("aiLab.models.trained", {
                          ago: timeAgo(m.trained_at, undefined, language),
                          seconds: (m.duration_ms / 1000).toFixed(1),
                        })
                      : t("aiLab.models.notTrainedYet")}
                  </p>
                </article>
              ) : (
                <Skeleton key={i} className="h-72 rounded-2xl" />
              ),
            )}
          </div>
        </section>
      </div>
    </MotionConfig>
  );
}
