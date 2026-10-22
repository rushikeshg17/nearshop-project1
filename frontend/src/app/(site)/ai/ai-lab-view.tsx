"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, BrainCircuit, Database, Filter, Loader2, Lock, MapPin, RefreshCw, Search, Sparkles, SplitSquareHorizontal, Unlock } from "lucide-react";
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
import { api, errorMessage } from "@/lib/api";
import { formatNumber, timeAgo } from "@/lib/format";
import { type MessageKey, richText, type Translate } from "@/lib/i18n";
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

type EvalFigures = {
  queries: number;
  scored_queries: number;
  "ndcg@10": number | null;
  "recall@20": number | null;
  mrr: number | null;
  zero_result_rate: number | null;
  latency_p95_ms: number | null;
};
type EvalRun = {
  split: "dev" | "test";
  at: string | null;
  commit: string;
  queries: number;
  synthetic_queries: number;
  embedding: string | null;
  overall: EvalFigures;
  by_language: Record<string, EvalFigures>;
  note: string | null;
  source?: "database" | "published";
};
type SearchEval = { splits: { dev: EvalRun | null; test: EvalRun | null } };

// The query languages the evaluation and the live figures report, in the interface language; another code shows as sent.
const LANGUAGE_NAMES: Record<string, MessageKey> = {
  en: "aiLab.languages.en",
  kn: "aiLab.languages.kn",
  "kn-Latn": "aiLab.languages.knLatn",
  te: "aiLab.languages.te",
  "te-Latn": "aiLab.languages.teLatn",
  hi: "aiLab.languages.hi",
  "hi-Latn": "aiLab.languages.hiLatn",
  misspelt: "aiLab.languages.misspelt",
};

function useLanguageName() {
  const { t } = useI18n();
  return (code: string) => {
    if (code === "all") return t("aiLab.languages.all");
    if (code === "other") return t("aiLab.languages.other");
    return Object.hasOwn(LANGUAGE_NAMES, code) ? t(LANGUAGE_NAMES[code]) : code;
  };
}

const fmt = (v: number | null | undefined, digits = 3) => (v === null || v === undefined ? "-" : v.toFixed(digits));

function EvalTable({ run }: { run: EvalRun }) {
  const { t } = useI18n();
  const languageName = useLanguageName();
  const rows: [string, EvalFigures][] = [["all", run.overall], ...Object.entries(run.by_language)];
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[34rem] text-sm">
        <caption className="sr-only">{t(run.split === "dev" ? "aiLab.quality.captionDev" : "aiLab.quality.captionTest")}</caption>
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th scope="col" className="py-2 pr-3 font-medium">{t("aiLab.quality.language")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.quality.queries")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.quality.ndcg")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.quality.recall")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.quality.mrr")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.quality.noResults")}</th>
            <th scope="col" className="py-2 text-right font-medium">{t("aiLab.quality.p95")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([code, m], i) => (
            <tr key={code} className={cn("border-b last:border-0", i === 0 && "font-semibold")}>
              <th scope="row" className="py-2 pr-3 text-left font-[inherit]">{languageName(code)}</th>
              <td className="py-2 pr-3 text-right tabular">{m.scored_queries}/{m.queries}</td>
              <td className="py-2 pr-3 text-right tabular">{fmt(m["ndcg@10"])}</td>
              <td className="py-2 pr-3 text-right tabular">{fmt(m["recall@20"])}</td>
              <td className="py-2 pr-3 text-right tabular">{fmt(m.mrr)}</td>
              <td className="py-2 pr-3 text-right tabular">{m.zero_result_rate === null ? "-" : `${Math.round(m.zero_result_rate * 100)}%`}</td>
              <td className="py-2 text-right tabular">
                {m.latency_p95_ms === null ? "-" : t("aiLab.quality.ms", { value: formatNumber(Math.round(m.latency_p95_ms)) })}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Live search measurement (P5.6, plan section 8.6): admins only. Each figure carries its sample size (n); a language
// with fewer than min_language_searches searches in the window has no figures ("not enough data").
type Figure = { value: number | null; n: number };
type OnlineRow = {
  language: string;
  enough_data: boolean;
  searches: number;
  history_searches: number;
  history_enough_data?: boolean;
  zero_result_rate_all: Figure;
  success_rate: Figure;
  zero_result_rate: Figure;
  reformulation_rate: Figure;
  click_position: { mean: number | null; median: number | null; top3_share: number | null; n: number };
  conversion_rate: Figure;
};
type OnlineWindow = { days: number; since: string; rows: OnlineRow[] };
type ShadowRow = {
  label: string;
  engine_candidate: boolean;
  searches: number;
  days_covered: number;
  mean_overlap_top10: number;
  zero_result: { production: number; candidate: number; difference: number; difference_pp: number };
  clicked: { searches: number; in_candidate_top10: number; in_candidate_top10_share: number | null; mean_rank_change: number | null };
  time_p95_ms: { candidate: number | null; production: number | null };
  guardrails: { name: string; pass: boolean | null }[];
  passed: boolean;
};
type OnlineReport = {
  at: string;
  ranking: string;
  min_language_searches: number;
  windows: OnlineWindow[];
  shadow: ShadowRow[];
  shadow_settings: { candidate: string | null; share: number };
  snapshots: { id: number; at: string; ranking: string }[];
};

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "-" : `${(v * 100).toFixed(1)}%`);

function SampleSize({ n }: { n: number }) {
  const { t } = useI18n();
  return <span className="text-xs text-muted-foreground">{t("aiLab.live.n", { n: formatNumber(n) })}</span>;
}

function FigureCell({ figure }: { figure: Figure }) {
  return (
    <td className="py-2 pr-3 text-right tabular">
      {pct(figure.value)} <SampleSize n={figure.n} />
    </td>
  );
}

function LiveTable({ window, minSearches }: { window: OnlineWindow; minSearches: number }) {
  const { t } = useI18n();
  const languageName = useLanguageName();
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[52rem] text-sm">
        <caption className="sr-only">{t("aiLab.live.caption", { days: formatNumber(window.days) })}</caption>
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th scope="col" className="py-2 pr-3 font-medium">{t("aiLab.quality.language")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.live.noResultsAll")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.live.measured")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.live.success")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.quality.noResults")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.live.reformulated")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.live.firstClick")}</th>
            <th scope="col" className="py-2 text-right font-medium">{t("aiLab.live.converted")}</th>
          </tr>
        </thead>
        <tbody>
          {window.rows.map((r) => {
            const historyShown = r.language === "all" || r.history_enough_data;
            return (
              <tr key={r.language} className={cn("border-b last:border-0", r.language === "all" && "font-semibold")}>
                <th scope="row" className="py-2 pr-3 text-left font-[inherit]">{languageName(r.language)}</th>
                {historyShown ? (
                  <FigureCell figure={r.zero_result_rate_all} />
                ) : (
                  <td className="py-2 pr-3 text-right text-xs text-muted-foreground">
                    {t("aiLab.live.notEnoughHistory", { n: formatNumber(r.history_searches) })}
                  </td>
                )}
                <td className="py-2 pr-3 text-right tabular">{formatNumber(r.searches)}</td>
                {r.enough_data ? (
                  <>
                    <FigureCell figure={r.success_rate} />
                    <FigureCell figure={r.zero_result_rate} />
                    <FigureCell figure={r.reformulation_rate} />
                    <td className="py-2 pr-3 text-right tabular">
                      {r.click_position.mean ?? "-"}, {r.click_position.median ?? "-"}, {pct(r.click_position.top3_share)}{" "}
                      <SampleSize n={r.click_position.n} />
                    </td>
                    <td className="py-2 text-right tabular">
                      {pct(r.conversion_rate.value)} <SampleSize n={r.conversion_rate.n} />
                    </td>
                  </>
                ) : (
                  <td colSpan={5} className="py-2 text-right text-xs text-muted-foreground">
                    {t("aiLab.live.notEnough", { count: formatNumber(minSearches) })}
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ShadowTable({ rows }: { rows: ShadowRow[] }) {
  const { t } = useI18n();
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[52rem] text-sm">
        <caption className="sr-only">{t("aiLab.shadow.caption")}</caption>
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th scope="col" className="py-2 pr-3 font-medium">{t("aiLab.shadow.candidate")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.shadow.searchesDays")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.shadow.overlap")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.shadow.moreNoResults")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.shadow.clickInTop10")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.shadow.rankChange")}</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">{t("aiLab.shadow.p95")}</th>
            <th scope="col" className="py-2 font-medium">{t("aiLab.shadow.guardrails")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label} className="border-b align-top last:border-0">
              <th scope="row" className="py-2 pr-3 text-left font-mono text-xs font-normal">{r.label}</th>
              <td className="py-2 pr-3 text-right tabular">{formatNumber(r.searches)}, {r.days_covered}</td>
              <td className="py-2 pr-3 text-right tabular">{r.mean_overlap_top10.toFixed(3)}</td>
              <td className="py-2 pr-3 text-right tabular">
                {t("aiLab.shadow.difference", { difference: r.zero_result.difference, pp: r.zero_result.difference_pp })}
              </td>
              <td className="py-2 pr-3 text-right tabular">
                {pct(r.clicked.in_candidate_top10_share)} <SampleSize n={r.clicked.searches} />
              </td>
              <td className="py-2 pr-3 text-right tabular">{r.clicked.mean_rank_change ?? "-"}</td>
              <td className="py-2 pr-3 text-right tabular">
                {t("aiLab.shadow.times", { candidate: r.time_p95_ms.candidate ?? "-", production: r.time_p95_ms.production ?? "-" })}
              </td>
              <td className="py-2">
                <ul className="space-y-0.5 text-xs">
                  {r.guardrails.map((g) => (
                    <li key={g.name}>
                      <span className={cn("font-semibold", g.pass === true && "text-success-ink", g.pass === false && "text-warning-ink")}>
                        {t(g.pass === null ? "aiLab.shadow.notApplicable" : g.pass ? "aiLab.shadow.pass" : "aiLab.shadow.fail")}
                      </span>{" "}
                      {g.name}
                    </li>
                  ))}
                </ul>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Whether owners see a model's output (P7.3, plan section 8.9): null for a model owners never see. The reason is the
// API's sentence (it carries the counts), shown as sent.
type ModelGate = { shown: boolean; code: string | null; reason: string | null; progress: { label: string; have: number; need: number }[] } | null;
type GatedModel = AiModel & { gate?: ModelGate };

function GateLine({ gate }: { gate: ModelGate }) {
  const { t } = useI18n();
  if (!gate) return null;
  return (
    <div className="mt-3 rounded-xl border border-dashed p-2.5 text-xs">
      <p className="flex items-center gap-1.5 font-medium">
        {gate.shown ? <Unlock className="size-3.5 text-success-ink" /> : <Lock className="size-3.5 text-warning-ink" />}
        {t(gate.shown ? "aiLab.gate.shown" : "aiLab.gate.hidden")}
      </p>
      {!gate.shown && gate.reason && <p className="mt-1 text-muted-foreground">{gate.reason}</p>}
    </div>
  );
}

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
    queryFn: () => api.get<{ training: boolean; queued?: boolean; models: GatedModel[] }>("/ai/models"),
    // Every 3 s while a retrain runs; once a minute while one waits for the retrain job (up to 30 minutes).
    refetchInterval: (q) => (q.state.data?.training ? 3000 : q.state.data?.queued ? 60000 : false),
  });
  const compare = useQuery({
    queryKey: ["ai", "compare", query],
    queryFn: () => api.get<SearchCompare>("/ai/search-compare", { q: query }),
    enabled: query.length >= 2,
  });
  const searchEval = useQuery({
    queryKey: ["ai", "search-eval"],
    queryFn: () => api.get<SearchEval>("/ai/search-eval"),
  });
  const isAdmin = me?.role === "admin";
  const online = useQuery({
    queryKey: ["search", "online"],
    queryFn: () => api.get<OnlineReport>("/search/online"),
    enabled: isAdmin,
  });
  const snapshot = useMutation({
    mutationFn: () => api.post<{ id: number; at: string; ranking: string }>("/search/online/snapshot"),
    onSuccess: () => {
      toast.success(t("aiLab.live.snapshotSaved"));
      qc.invalidateQueries({ queryKey: ["search", "online"] });
    },
    onError: (e) => toast.error(errorMessage(e)),
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

        {/* ------------------------------------------------------------ search quality */}
        <section className="mt-16" aria-labelledby="search-quality">
          <h2 id="search-quality" className="text-2xl font-semibold">{t("aiLab.quality.title")}</h2>
          <p className="mt-1 max-w-3xl text-muted-foreground">{t("aiLab.quality.intro")}</p>
          <div className="mt-6 grid gap-4 xl:grid-cols-2">
            {(["dev", "test"] as const).map((split) => {
              const run = searchEval.data?.splits[split];
              if (searchEval.isPending) return <Skeleton key={split} className="h-56 rounded-2xl" />;
              return (
                <article key={split} className="rounded-2xl border bg-card p-5">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <h3 className="font-semibold">{t(split === "dev" ? "aiLab.quality.dev" : "aiLab.quality.test")}</h3>
                    {run && run.synthetic_queries > 0 && <Badge variant="outline" className="shrink-0 text-[10px]">{t("aiLab.quality.examples")}</Badge>}
                  </div>
                  {run ? (
                    <>
                      <p className="mt-1 text-xs text-muted-foreground" suppressHydrationWarning>
                        {richText(t("aiLab.quality.runLine", { count: formatNumber(run.queries) }), {
                          commit: <span className="font-mono">{run.commit || t("aiLab.quality.unknownCommit")}</span>,
                        })}
                        {run.at ? `, ${timeAgo(run.at, undefined, language)}` : ""}
                        {run.embedding ? `, ${run.embedding}` : ""}
                      </p>
                      <div className="mt-4"><EvalTable run={run} /></div>
                      {run.synthetic_queries > 0 && (
                        <p className="mt-3 text-xs text-muted-foreground">{t("aiLab.quality.syntheticNote")}</p>
                      )}
                    </>
                  ) : (
                    <p className="mt-3 text-sm text-muted-foreground">{t(searchEval.isError ? "aiLab.quality.loadFailed" : "aiLab.quality.notMeasured")}</p>
                  )}
                </article>
              );
            })}
          </div>
        </section>

        {/* ------------------------------------------------------------ live search (admins only) */}
        {isAdmin && (
          <section className="mt-12" aria-labelledby="search-live">
            <div className="flex flex-wrap items-end justify-between gap-4">
              <div className="max-w-3xl">
                <h2 id="search-live" className="text-2xl font-semibold">{t("aiLab.live.title")}</h2>
                <p className="mt-1 text-muted-foreground">{t("aiLab.live.intro")}</p>
              </div>
              <Button variant="outline" onClick={() => snapshot.mutate()} disabled={snapshot.isPending || !online.data}>
                {snapshot.isPending ? <Loader2 className="animate-spin" /> : <Database />} {t("aiLab.live.saveSnapshot")}
              </Button>
            </div>
            {online.isPending ? (
              <Skeleton className="mt-6 h-56 rounded-2xl" />
            ) : online.data ? (
              <>
                <div className="mt-6 grid gap-4">
                  {online.data.windows.map((w) => (
                    <article key={w.days} className="rounded-2xl border bg-card p-5">
                      <h3 className="font-semibold">{t("aiLab.live.lastDays", { days: formatNumber(w.days) })}</h3>
                      <p className="mt-1 text-xs text-muted-foreground">{t("aiLab.live.ranking", { ranking: online.data.ranking })}</p>
                      <div className="mt-4"><LiveTable window={w} minSearches={online.data.min_language_searches} /></div>
                    </article>
                  ))}
                  <article className="rounded-2xl border bg-card p-5">
                    <h3 className="font-semibold">{t("aiLab.shadow.title")}</h3>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {online.data.shadow_settings.candidate
                        ? t("aiLab.shadow.on", { candidate: online.data.shadow_settings.candidate, share: pct(online.data.shadow_settings.share) })
                        : t("aiLab.shadow.off")}
                    </p>
                    <div className="mt-4">
                      {online.data.shadow.length ? (
                        <ShadowTable rows={online.data.shadow} />
                      ) : (
                        <p className="text-sm text-muted-foreground">{t("aiLab.shadow.empty")}</p>
                      )}
                    </div>
                  </article>
                </div>
                {online.data.snapshots.length > 0 && (
                  <p className="mt-3 text-xs text-muted-foreground" suppressHydrationWarning>
                    {t("aiLab.live.lastSnapshot", {
                      ago: timeAgo(online.data.snapshots[0].at, undefined, language),
                      ranking: online.data.snapshots[0].ranking,
                    })}
                  </p>
                )}
              </>
            ) : (
              <p className="mt-6 text-sm text-muted-foreground">{t("aiLab.live.loadFailed")}</p>
            )}
          </section>
        )}

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
                    <span className="flex shrink-0 gap-1">
                      {m.gate && !m.gate.shown && <Badge variant="outline" className="text-[10px]">{t("aiLab.gate.badge")}</Badge>}
                      {m.uses_demo_data && <Badge variant="outline" className="text-[10px]">{t("aiLab.models.demoData")}</Badge>}
                    </span>
                  </div>
                  <p className="mt-1 text-sm text-muted-foreground">{m.purpose}</p>
                  <GateLine gate={m.gate ?? null} />
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
