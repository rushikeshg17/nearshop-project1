"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Gift, Loader2, Megaphone, PauseCircle, Search, Smartphone, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { useI18n } from "@/components/providers";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useMeta } from "@/hooks/use-session";
import { api, errorMessage } from "@/lib/api";
import { formatNumber, formatPrice } from "@/lib/format";
import type { MessageKey } from "@/lib/i18n";
import type {
  BoostEligibility,
  BoostPack,
  BoostPackKind,
  BoostPackStatus,
  BoostPaymentRequest,
  BoostQuote,
  OwnerListing,
} from "@/lib/types";
import { upiPaymentLink, useUpiQr } from "@/lib/upi";
import { cn } from "@/lib/utils";

import { formatDay } from "../owner-shell";

/**
 * Boost (P10.13, plan section 8.22). The owner sees whether their city may sell packs (the daily check against the
 * three thresholds), buys a pack for up to its number of item groups (categories or catalogue items) and pays
 * NearShop by UPI through the pack's payment request (P9.5), shown as a upi://pay link and a QR code with the
 * request number as the note, as on Plan and billing (P9.10). Each pack lists its period, its impressions, clicks
 * and customer requests, and its make-good: a period with no requests earns the next period free, automatically.
 *
 * The QR library loads only once a payment block is on screen (useUpiQr).
 */

type Group = { type: "category" | "item"; id: number; name: string };

const rupees = (paise: number) => formatPrice(paise / 100, true);

const REASONS: Record<string, MessageKey> = {
  sales_off: "owner.boost.reasons.salesOff",
  shop_not_live: "owner.boost.reasons.shopNotLive",
  city_no_recent_check: "owner.boost.reasons.noRecentCheck",
  city_verified_shops: "owner.boost.reasons.verifiedShops",
  city_median_stock_age: "owner.boost.reasons.medianStockAge",
  city_customer_weeks: "owner.boost.reasons.customerWeeks",
  city_monthly_customers: "owner.boost.reasons.monthlyCustomers",
};

const STATUS: Record<BoostPackStatus, MessageKey> = {
  pending: "owner.boost.status.pending",
  active: "owner.boost.status.active",
  paused: "owner.boost.status.paused",
  ended: "owner.boost.status.ended",
  make_good: "owner.boost.status.makeGood",
};

const KIND: Record<BoostPackKind, MessageKey> = {
  standard: "owner.boost.kind.standard",
  festive: "owner.boost.kind.festive",
};

export default function BoostPage() {
  const { t } = useI18n();
  const eligibility = useQuery({
    queryKey: ["owner", "boost", "eligibility"],
    queryFn: () => api.get<BoostEligibility>("/owner/boost/eligibility"),
  });
  const packs = useQuery({
    queryKey: ["owner", "boost", "packs"],
    queryFn: () => api.get<{ packs: BoostPack[] }>("/owner/boost/packs"),
  });
  // Open payment requests for packs waiting for their payment, so the pay block is still there after a reload.
  const billing = useQuery({
    queryKey: ["owner", "billing"],
    queryFn: () => api.get<{ requests: BoostPaymentRequest[] }>("/owner/billing"),
  });
  const [bought, setBought] = useState<BoostPaymentRequest | null>(null);

  const open = [
    ...(bought ? [bought] : []),
    ...(billing.data?.requests ?? []).filter((r) => r.purpose === "boost" && r.boost_pack_id !== null && r.id !== bought?.id),
  ];
  const pendingIds = new Set((packs.data?.packs ?? []).filter((p) => p.status === "pending").map((p) => p.id));
  const known = new Set((packs.data?.packs ?? []).map((p) => p.id));
  // A request shows while its pack waits for the payment (or before the list has caught up with a purchase).
  const toPay = open.filter((r) => r.boost_pack_id !== null && (pendingIds.has(r.boost_pack_id) || (r.id === bought?.id && !known.has(r.boost_pack_id))));

  return (
    <div className="space-y-6">
      <PageTitle title={t("owner.boost.title")} description={t("owner.boost.subtitle")} />
      {eligibility.isError ? (
        <ErrorState error={eligibility.error} onRetry={() => eligibility.refetch()} />
      ) : !eligibility.data ? (
        <ListSkeleton rows={3} />
      ) : (
        <>
          <CityStatus e={eligibility.data} />
          <HowItWorks e={eligibility.data} />
          {toPay.map((r) => (
            <PayNearShop key={r.id} req={r} />
          ))}
          <BuyPack e={eligibility.data} onBought={setBought} />
        </>
      )}
      <section aria-labelledby="boost-packs-title" className="space-y-3">
        <h2 id="boost-packs-title" className="text-lg font-semibold">{t("owner.boost.packs.heading")}</h2>
        {packs.isError ? (
          <ErrorState error={packs.error} onRetry={() => packs.refetch()} />
        ) : !packs.data ? (
          <ListSkeleton rows={2} />
        ) : packs.data.packs.length === 0 ? (
          <p className="rounded-2xl border bg-card p-5 text-sm text-muted-foreground">{t("owner.boost.packs.none")}</p>
        ) : (
          packs.data.packs.map((p) => <PackCard key={p.id} pack={p} />)
        )}
      </section>
    </div>
  );
}

function Figure({ label, value, target, ok }: { label: string; value: string; target: string; ok: boolean | null }) {
  return (
    <div className="rounded-xl border p-3">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 flex items-center gap-1.5 text-lg font-semibold tabular">
        {ok === null ? null : ok ? (
          <Check className="size-4 text-success" aria-hidden />
        ) : (
          <X className="size-4 text-destructive" aria-hidden />
        )}
        {value}
      </dd>
      <dd className="text-xs text-muted-foreground">{target}</dd>
    </div>
  );
}

/** The shop's city against the three thresholds, from the latest daily check, and why the shop cannot buy now. */
function CityStatus({ e }: { e: BoostEligibility }) {
  const { t, language } = useI18n();
  const c = e.city;
  const th = c.thresholds;
  const dash = t("owner.boost.city.none");
  const age = c.median_stock_age_hours;
  const lowest = c.lowest_monthly_customers;
  return (
    <section aria-labelledby="boost-city-title" className="rounded-3xl border bg-card p-5" data-testid="boost-city" data-eligible={c.eligible}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="boost-city-title" className="font-semibold">{t("owner.boost.city.heading", { city: c.name })}</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {c.as_of ? t("owner.boost.city.asOf", { date: formatDay(`${c.as_of}T12:00:00Z`, language) }) : t("owner.boost.city.notChecked")}
          </p>
        </div>
        <Badge variant={c.eligible ? "default" : "secondary"}>{t(c.eligible ? "owner.boost.city.eligible" : "owner.boost.city.notEligible")}</Badge>
      </div>
      <dl className="mt-4 grid gap-3 sm:grid-cols-3">
        <Figure
          label={t("owner.boost.city.verifiedShops")}
          value={c.verified_shops === null ? dash : formatNumber(c.verified_shops)}
          target={t("owner.boost.city.atLeast", { count: formatNumber(th.verified_shops) })}
          ok={c.verified_shops === null ? null : c.verified_shops >= th.verified_shops}
        />
        <Figure
          label={t("owner.boost.city.monthlyCustomers", { weeks: formatNumber(th.customer_weeks) })}
          value={lowest === null ? dash : formatNumber(lowest)}
          target={t("owner.boost.city.atLeast", { count: formatNumber(th.monthly_customers) })}
          ok={lowest === null ? null : lowest >= th.monthly_customers && !c.reasons.includes("customer_weeks")}
        />
        <Figure
          label={t("owner.boost.city.medianStockAge")}
          value={age === null ? dash : t("owner.boost.city.hours", { count: formatNumber(Math.round(age)) })}
          target={t("owner.boost.city.atMost", { count: formatNumber(th.median_stock_age_hours) })}
          ok={age === null ? null : age <= th.median_stock_age_hours}
        />
      </dl>
      {e.reasons.length > 0 && (
        <div className="mt-4 rounded-xl bg-muted/60 p-4 text-sm" role="status" data-testid="boost-reasons">
          <p className="font-medium">{t("owner.boost.reasons.heading")}</p>
          <ul className="mt-1.5 list-disc space-y-1 pl-5">
            {e.reasons.map((r) => (
              <li key={r}>{REASONS[r] ? t(REASONS[r]) : r}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function HowItWorks({ e }: { e: BoostEligibility }) {
  const { t } = useI18n();
  return (
    <section aria-labelledby="boost-how-title" className="rounded-3xl border bg-card p-5">
      <h2 id="boost-how-title" className="font-semibold">{t("owner.boost.how.heading")}</h2>
      <ul className="mt-2 space-y-1.5 text-sm">
        {[
          t("owner.boost.how.label", { label: t("owner.boost.how.labelText"), position: formatNumber(e.slot_position) }),
          t("owner.boost.how.fresh", { hours: formatNumber(e.fresh_hours) }),
          t("owner.boost.how.organic"),
          t("owner.boost.how.makeGood"),
          t("owner.boost.how.pause"),
        ].map((line) => (
          <li key={line} className="flex gap-2"><Check className="mt-0.5 size-4 shrink-0 text-brand" aria-hidden /> {line}</li>
        ))}
      </ul>
    </section>
  );
}

/** Choosing a pack and its item groups, then buying it: a pending pack and its payment request. */
function BuyPack({ e, onBought }: { e: BoostEligibility; onBought: (r: BoostPaymentRequest) => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const { data: meta } = useMeta();
  const [kind, setKind] = useState<BoostPackKind>("standard");
  const [groups, setGroups] = useState<Group[]>([]);
  const [q, setQ] = useState("");
  const quote = e.packs.find((p) => p.kind === kind) ?? e.packs[0];
  const max = quote?.max_groups ?? 0;
  const items = useQuery({
    queryKey: ["owner", "boost", "items", q],
    queryFn: () => api.get<{ items: OwnerListing[] }>("/owner/products", { q, page_size: 20 }),
    enabled: e.can_buy && q.trim().length >= 2,
  });
  const buy = useMutation({
    mutationFn: () =>
      api.post<{ pack: BoostPack; payment_request: BoostPaymentRequest }>("/owner/boost/packs", {
        kind,
        groups: groups.slice(0, max).map(({ type, id }) => ({ type, id })),
      }),
    onSuccess: (r) => {
      onBought(r.payment_request);
      setGroups([]);
      toast.success(t("owner.boost.buy.done", { number: r.payment_request.number }));
      void qc.invalidateQueries({ queryKey: ["owner", "boost", "packs"] });
      void qc.invalidateQueries({ queryKey: ["owner", "billing"] });
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  if (!e.can_buy) return null;
  const chosen = (g: Group) => groups.some((x) => x.type === g.type && x.id === g.id);
  const toggle = (g: Group) =>
    setGroups((list) => (chosen(g) ? list.filter((x) => !(x.type === g.type && x.id === g.id)) : list.length >= max ? list : [...list, g]));
  const full = groups.length >= max;
  const catalogue = (items.data?.items ?? []).filter((i) => i.catalog_item_id !== null);
  const seen = new Set<number>();
  const itemGroups: Group[] = [];
  for (const i of catalogue) {
    if (seen.has(i.catalog_item_id!)) continue;
    seen.add(i.catalog_item_id!);
    itemGroups.push({ type: "item", id: i.catalog_item_id!, name: i.name });
  }

  return (
    <section aria-labelledby="boost-buy-title" className="rounded-3xl border bg-card p-5" data-testid="boost-buy">
      <h2 id="boost-buy-title" className="font-semibold">{t("owner.boost.buy.heading")}</h2>
      <div role="radiogroup" aria-label={t("owner.boost.buy.kindLabel")} className="mt-3 grid gap-3 sm:grid-cols-2">
        {e.packs.map((p) => (
          <QuoteOption key={p.kind} quote={p} selected={p.kind === kind} onSelect={() => setKind(p.kind)} />
        ))}
      </div>

      <h3 className="mt-5 text-sm font-semibold">{t("owner.boost.buy.groupsHeading", { count: formatNumber(max) })}</h3>
      <p className="mt-0.5 text-sm text-muted-foreground">{t("owner.boost.buy.groupsHint")}</p>
      {groups.length > 0 && (
        <ul className="mt-3 flex flex-wrap gap-2" aria-label={t("owner.boost.buy.chosen")}>
          {groups.map((g) => (
            <li key={`${g.type}${g.id}`}>
              <button
                type="button"
                onClick={() => toggle(g)}
                className="inline-flex min-h-11 items-center gap-1.5 rounded-full border border-primary bg-primary px-3 text-sm text-primary-foreground"
                aria-label={t("owner.boost.buy.remove", { name: g.name })}
              >
                {g.name} <X className="size-3.5" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}
      <h4 className="mt-4 text-xs font-medium text-muted-foreground">{t("owner.boost.buy.categories")}</h4>
      <div className="mt-2 flex flex-wrap gap-2">
        {(meta?.categories ?? []).map((c) => {
          const g: Group = { type: "category", id: c.id, name: c.name };
          const on = chosen(g);
          return (
            <button
              key={c.id}
              type="button"
              aria-pressed={on}
              disabled={!on && full}
              onClick={() => toggle(g)}
              className={cn(
                "inline-flex min-h-11 items-center rounded-full border px-3 text-sm transition-colors disabled:opacity-50",
                on ? "border-primary bg-primary text-primary-foreground" : "bg-card hover:bg-accent",
              )}
            >
              {c.name}
            </button>
          );
        })}
      </div>
      <h4 className="mt-4 text-xs font-medium text-muted-foreground">{t("owner.boost.buy.items")}</h4>
      <label className="relative mt-2 block max-w-sm">
        <span className="sr-only">{t("owner.boost.buy.searchLabel")}</span>
        <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <Input value={q} onChange={(ev) => setQ(ev.target.value)} placeholder={t("owner.boost.buy.searchPlaceholder")} className="h-11 pl-9" />
      </label>
      {q.trim().length >= 2 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {items.isLoading ? (
            <Loader2 className="size-4 animate-spin" aria-hidden />
          ) : itemGroups.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("owner.boost.buy.noItems")}</p>
          ) : (
            itemGroups.map((g) => {
              const on = chosen(g);
              return (
                <button
                  key={g.id}
                  type="button"
                  aria-pressed={on}
                  disabled={!on && full}
                  onClick={() => toggle(g)}
                  className={cn(
                    "inline-flex min-h-11 items-center rounded-full border px-3 text-sm transition-colors disabled:opacity-50",
                    on ? "border-primary bg-primary text-primary-foreground" : "bg-card hover:bg-accent",
                  )}
                >
                  {g.name}
                </button>
              );
            })
          )}
        </div>
      )}
      <div className="mt-5 flex flex-wrap items-center gap-3">
        <Button className="h-11" disabled={groups.length === 0 || groups.length > max || buy.isPending} onClick={() => buy.mutate()} data-testid="boost-buy-button">
          {buy.isPending ? <Loader2 className="animate-spin" /> : <Megaphone />}
          {t("owner.boost.buy.button", { price: quote ? rupees(quote.amount_paise) : "" })}
        </Button>
        <p className="text-xs text-muted-foreground">{t("owner.boost.buy.gstNote")}</p>
      </div>
    </section>
  );
}

function QuoteOption({ quote, selected, onSelect }: { quote: BoostQuote; selected: boolean; onSelect: () => void }) {
  const { t } = useI18n();
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={onSelect}
      data-testid={`boost-quote-${quote.kind}`}
      className={cn("rounded-2xl border p-4 text-left transition-colors", selected ? "border-primary ring-2 ring-primary/30" : "hover:bg-accent")}
    >
      <span className="block font-semibold">{t(KIND[quote.kind])}</span>
      <span className="mt-1 block text-lg font-semibold tabular">
        {rupees(quote.amount_paise)}
        {quote.discount_pct > 0 && (
          <span className="ml-2 text-sm font-normal text-muted-foreground line-through">{rupees(quote.list_price_paise)}</span>
        )}
      </span>
      <span className="mt-1 block text-sm text-muted-foreground">
        {t("owner.boost.buy.terms", { days: formatNumber(quote.days), count: formatNumber(quote.max_groups) })}
      </span>
      {quote.discount_pct > 0 && (
        <span className="mt-1 block text-sm text-brand-ink">{t("owner.boost.buy.discount", { pct: formatNumber(quote.discount_pct) })}</span>
      )}
    </button>
  );
}

/** "Pay NearShop by UPI" for a pack's payment request (the pattern of P9.10's pay block): the upi://pay link to
 * NearShop's own UPI ID with the total and the request number as the note, and the same link as a QR code. */
function PayNearShop({ req }: { req: BoostPaymentRequest }) {
  const { t } = useI18n();
  const amount = Number(req.pay_to.amount);
  const price = formatPrice(amount, true);
  const payee = req.pay_to.payee_name;
  const link = upiPaymentLink({ upiId: req.pay_to.upi_id, payeeName: payee, amount, note: req.pay_to.note });
  const qr = useUpiQr(link);
  return (
    <section aria-labelledby={`boost-pay-${req.id}`} className="rounded-3xl border bg-card p-5" data-testid="pay-block">
      <div className="flex items-start gap-3">
        <Smartphone className="mt-0.5 size-5 shrink-0 text-primary" aria-hidden />
        <div>
          <h2 id={`boost-pay-${req.id}`} className="font-semibold">{t("owner.boost.pay.title")}</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">{t("owner.boost.pay.body", { price, payee })}</p>
        </div>
      </div>
      <div className="mt-4 flex flex-col items-center gap-4 sm:flex-row sm:items-start">
        <div className="shrink-0 rounded-xl border bg-white p-2">
          {qr ? (
            <svg role="img" aria-label={t("owner.boost.pay.qrLabel", { price, payee })} viewBox={`-4 -4 ${qr.size + 8} ${qr.size + 8}`}
              shapeRendering="crispEdges" className="size-40" data-testid="pay-qr">
              <path d={qr.path} fill="#000" />
            </svg>
          ) : (
            <div aria-hidden className="size-40 animate-pulse rounded-lg bg-neutral-200" />
          )}
        </div>
        <div className="w-full min-w-0 space-y-3 text-sm">
          <Button asChild className="h-11 w-full sm:w-auto">
            <a href={link} data-testid="pay-link"><Smartphone /> {t("owner.boost.pay.button", { price })}</a>
          </Button>
          <p className="text-muted-foreground">{t("owner.boost.pay.scanHint")}</p>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
            <dt className="text-muted-foreground">{t("owner.boost.pay.payee")}</dt>
            <dd data-testid="pay-payee">{payee}</dd>
            <dt className="text-muted-foreground">{t("owner.boost.pay.upiId")}</dt>
            <dd className="break-all font-mono">{req.pay_to.upi_id}</dd>
            <dt className="text-muted-foreground">{t("owner.boost.pay.amount")}</dt>
            <dd data-testid="pay-amount">
              {price}
              {req.gst_paise > 0 && (
                <span className="block text-xs text-muted-foreground">{t("owner.boost.pay.gst", { amount: rupees(req.amount_paise), gst: rupees(req.gst_paise) })}</span>
              )}
            </dd>
            <dt className="text-muted-foreground">{t("owner.boost.pay.note")}</dt>
            <dd className="font-mono" data-testid="pay-note">{req.pay_to.note}</dd>
          </dl>
          <p className="text-xs text-muted-foreground">{t("owner.boost.pay.after")}</p>
        </div>
      </div>
    </section>
  );
}

/** One pack: its item groups, period, price, figures and make-good. */
function PackCard({ pack }: { pack: BoostPack }) {
  const { t, language } = useI18n();
  const ctr = pack.totals.impressions > 0 ? Math.round((pack.totals.clicks / pack.totals.impressions) * 1000) / 10 : null;
  return (
    <article className="rounded-3xl border bg-card p-5" data-testid="boost-pack" data-pack={pack.id} data-status={pack.status}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-semibold">{t(KIND[pack.kind])}</h3>
          <p className="mt-0.5 text-sm break-words text-muted-foreground">{pack.groups.map((g) => g.name).join(", ")}</p>
        </div>
        <Badge variant={pack.status === "active" || pack.status === "make_good" ? "default" : "secondary"}>{t(STATUS[pack.status])}</Badge>
      </div>
      <p className="mt-2 text-sm">
        {pack.period_start && pack.ends_at
          ? t("owner.boost.packs.period", { start: formatDay(pack.period_start, language), end: formatDay(pack.ends_at, language) })
          : pack.payment_request_number
            ? t("owner.boost.packs.awaiting", { number: pack.payment_request_number })
            : null}
      </p>
      <p className="mt-0.5 text-xs text-muted-foreground">
        {t("owner.boost.packs.price", { price: rupees(pack.amount_paise), days: formatNumber(pack.days) })}
        {pack.discount_pct > 0 && ` · ${t("owner.boost.buy.discount", { pct: formatNumber(pack.discount_pct) })}`}
      </p>
      <dl className="mt-4 grid grid-cols-3 gap-3" data-testid="boost-figures">
        <div className="rounded-xl border p-3">
          <dt className="text-xs text-muted-foreground">{t("owner.boost.packs.impressions")}</dt>
          <dd className="mt-1 text-lg font-semibold tabular" data-testid="boost-impressions">{formatNumber(pack.totals.impressions)}</dd>
        </div>
        <div className="rounded-xl border p-3">
          <dt className="text-xs text-muted-foreground">{t("owner.boost.packs.clicks")}</dt>
          <dd className="mt-1 text-lg font-semibold tabular" data-testid="boost-clicks">{formatNumber(pack.totals.clicks)}</dd>
          {ctr !== null && <dd className="text-xs text-muted-foreground">{t("owner.boost.packs.ctr", { pct: formatNumber(ctr) })}</dd>}
        </div>
        <div className="rounded-xl border p-3">
          <dt className="text-xs text-muted-foreground">{t("owner.boost.packs.requests")}</dt>
          <dd className="mt-1 text-lg font-semibold tabular" data-testid="boost-requests">{formatNumber(pack.totals.requests)}</dd>
        </div>
      </dl>
      <div className="mt-3 space-y-1.5 text-sm" data-testid="boost-make-good">
        {pack.status === "make_good" ? (
          <p className="flex gap-2"><Gift className="mt-0.5 size-4 shrink-0 text-brand" aria-hidden /> {t("owner.boost.packs.makeGoodRunning")}</p>
        ) : pack.make_good_used ? (
          <p className="flex gap-2"><Gift className="mt-0.5 size-4 shrink-0 text-brand" aria-hidden /> {t("owner.boost.packs.makeGoodEarned")}</p>
        ) : pack.status === "active" ? (
          <p className="text-muted-foreground">{t("owner.boost.packs.makeGoodOpen", { count: formatNumber(pack.period_requests) })}</p>
        ) : null}
        {pack.status === "paused" && (
          <p className="flex gap-2"><PauseCircle className="mt-0.5 size-4 shrink-0" aria-hidden /> {t("owner.boost.packs.paused")}</p>
        )}
        {pack.paused_days_added > 0 && (
          <p className="text-muted-foreground">{t("owner.boost.packs.daysAdded", { days: formatNumber(pack.paused_days_added) })}</p>
        )}
      </div>
      {pack.by_day.length > 0 && (
        <details className="mt-3 text-sm">
          <summary className="min-h-11 cursor-pointer py-2.5 font-medium">{t("owner.boost.packs.byDay")}</summary>
          <div className="overflow-x-auto">
            <table className="w-full text-left tabular">
              <thead className="text-xs text-muted-foreground">
                <tr>
                  <th scope="col" className="py-1 pr-3 font-medium">{t("owner.boost.packs.day")}</th>
                  <th scope="col" className="py-1 pr-3 font-medium">{t("owner.boost.packs.impressions")}</th>
                  <th scope="col" className="py-1 pr-3 font-medium">{t("owner.boost.packs.clicks")}</th>
                  <th scope="col" className="py-1 font-medium">{t("owner.boost.packs.requests")}</th>
                </tr>
              </thead>
              <tbody>
                {[...pack.by_day].reverse().map((d) => (
                  <tr key={d.day} className="border-t">
                    <td className="py-1 pr-3">{formatDay(`${d.day}T12:00:00Z`, language)}</td>
                    <td className="py-1 pr-3">{formatNumber(d.impressions)}</td>
                    <td className="py-1 pr-3">{formatNumber(d.clicks)}</td>
                    <td className="py-1">{formatNumber(d.requests)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </article>
  );
}
