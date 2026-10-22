"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Check, CreditCard, FileText, Gift, Loader2, Repeat, ShieldCheck, Smartphone } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { useI18n } from "@/components/providers";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { formatNumber, formatPrice } from "@/lib/format";
import { type MessageKey, richText } from "@/lib/i18n";
import type { AutopayMandate, OwnerBilling, OwnerPlan, PaymentRequest, PlanEntitlements, PlanVersion } from "@/lib/types";
import { upiPaymentLink, useUpiQr } from "@/lib/upi";

import { formatDay } from "../owner-shell";

/**
 * Plan and billing (P9.10, plan section 8.18). The owner sees their plan in plain words, the tools that stay free on
 * every plan (plan section 8.17), the end date and the next reminder, the founding offer and this quarter's outcome
 * guarantee. Growth is paid for a year by UPI straight to NearShop's own account through a payment request (P9.5),
 * shown as a upi://pay link and a QR code with the request number as the note, or by the month through UPI Autopay
 * (P9.6). Payments and their receipts or invoices are listed at the bottom, each document on its own printable page.
 *
 * The QR library loads only once a payment block is on screen (useUpiQr), so it is never first-load JavaScript.
 */

/** The plan-terms section of the terms of use (P3.1); its text comes from B24. The document page links it too. */
const PLAN_TERMS_HREF = "/terms#plan-terms";

/** Reminder days before a plan's end date (backend/app/services/plans.py, REMINDER_DAYS): a monthly plan gets only
 * the stages shorter than its shortest term (7 and 1), and one Autopay renews gets none (the gateway's notice). */
const REMINDER_DAYS = { year: [30, 7, 1], month: [7, 1] } as const;
const DAY = 86_400_000;

/** The tools free on every plan, lapsed or not (plan section 8.17), in the order the page lists them. */
const FREE_TOOLS = ["listings", "import", "whatsapp", "alerts", "standee", "trust", "statement"] as const;

const rupees = (paise: number) => formatPrice(paise / 100, true);

/** The next reminder date for a plan ending at endsAt, or null when none is left. */
function nextReminder(endsAt: string, period: "year" | "month", now: number) {
  const end = new Date(endsAt).getTime();
  const due = REMINDER_DAYS[period].map((d) => end - d * DAY).find((at) => at > now);
  return due === undefined ? null : new Date(due).toISOString();
}

function PlanIncludes({ e, needed }: { e: PlanEntitlements; needed: number }) {
  const { t } = useI18n();
  const lines: string[] = [
    t(e.demand_report === "full" ? "owner.billing.includes.demandFull" : "owner.billing.includes.demandTeaser"),
    ...(e.low_stock_alerts ? [t("owner.billing.includes.lowStock")] : []),
    t("owner.billing.includes.logins", { count: formatNumber(e.logins) }),
    ...(e.roi_statement ? [t("owner.billing.includes.roi")] : []),
    ...(e.priority_support ? [t("owner.billing.includes.support")] : []),
    ...(e.boost_discount_pct > 0 ? [t("owner.billing.includes.boost", { pct: formatNumber(e.boost_discount_pct) })] : []),
    ...(e.outcome_guarantee ? [t("owner.billing.includes.guarantee")] : []),
    ...(e.pro_insights ? [t("owner.billing.includes.proInsights")] : []),
    ...(e.outlets > 1 ? [t("owner.billing.includes.outlets", { count: formatNumber(e.outlets) })] : []),
  ];
  return (
    <div>
      <h3 className="text-sm font-semibold">{t("owner.billing.includes.heading")}</h3>
      <ul className="mt-2 space-y-1.5 text-sm" data-testid="plan-includes" data-needed={needed}>
        {lines.map((line) => (
          <li key={line} className="flex gap-2"><Check className="mt-0.5 size-4 shrink-0 text-brand" aria-hidden /> {line}</li>
        ))}
      </ul>
    </div>
  );
}

function FreeTools() {
  const { t } = useI18n();
  return (
    <section aria-labelledby="free-tools-title" className="rounded-3xl border bg-card p-5" data-testid="free-tools">
      <div className="flex items-start gap-3">
        <ShieldCheck className="mt-0.5 size-5 shrink-0 text-brand" aria-hidden />
        <div>
          <h2 id="free-tools-title" className="font-semibold">{t("owner.billing.freeTools.heading")}</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">{t("owner.billing.freeTools.body")}</p>
        </div>
      </div>
      <ul className="mt-3 grid gap-1.5 text-sm sm:grid-cols-2">
        {FREE_TOOLS.map((k) => (
          <li key={k} className="flex gap-2"><Check className="mt-0.5 size-4 shrink-0 text-brand" aria-hidden /> {t(`owner.billing.freeTools.${k}`)}</li>
        ))}
      </ul>
    </section>
  );
}

/** "Pay NearShop by UPI": the request's upi://pay link to NearShop's own UPI ID, with the total and the request
 * number as the note, and the same link as a QR code for paying from another phone. */
function PayNearShop({ req }: { req: PaymentRequest }) {
  const { t } = useI18n();
  const amount = Number(req.pay_to.amount);
  const price = formatPrice(amount, true);
  const payee = req.pay_to.payee_name;
  const link = upiPaymentLink({ upiId: req.pay_to.upi_id, payeeName: payee, amount, note: req.pay_to.note });
  const qr = useUpiQr(link);
  return (
    <section aria-labelledby="pay-nearshop-title" className="rounded-3xl border bg-card p-5" data-testid="pay-block">
      <div className="flex items-start gap-3">
        <Smartphone className="mt-0.5 size-5 shrink-0 text-primary" aria-hidden />
        <div>
          <h2 id="pay-nearshop-title" className="font-semibold">{t("owner.billing.block.title")}</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">{t("owner.billing.block.body", { price, payee })}</p>
        </div>
      </div>
      <div className="mt-4 flex flex-col items-center gap-4 sm:flex-row sm:items-start">
        <div className="shrink-0 rounded-xl border bg-white p-2">
          {qr ? (
            <svg role="img" aria-label={t("owner.billing.block.qrLabel", { price, payee })} viewBox={`-4 -4 ${qr.size + 8} ${qr.size + 8}`}
              shapeRendering="crispEdges" className="size-40" data-testid="pay-qr">
              <path d={qr.path} fill="#000" />
            </svg>
          ) : (
            <div aria-hidden className="size-40 animate-pulse rounded-lg bg-neutral-200" />
          )}
        </div>
        <div className="w-full min-w-0 space-y-3 text-sm">
          <Button asChild className="h-11 w-full sm:w-auto">
            <a href={link} data-testid="pay-link"><Smartphone /> {t("owner.billing.block.payButton", { price })}</a>
          </Button>
          <p className="text-muted-foreground">{t("owner.billing.block.scanHint")}</p>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
            <dt className="text-muted-foreground">{t("owner.billing.block.payee")}</dt>
            <dd data-testid="pay-payee">{payee}</dd>
            <dt className="text-muted-foreground">{t("owner.billing.block.upiId")}</dt>
            <dd className="break-all font-mono">{req.pay_to.upi_id}</dd>
            <dt className="text-muted-foreground">{t("owner.billing.block.amount")}</dt>
            <dd data-testid="pay-amount">
              {price}
              {req.gst_paise > 0 && (
                <span className="block text-xs text-muted-foreground">{t("owner.billing.pay.gst", { amount: rupees(req.amount_paise), gst: rupees(req.gst_paise) })}</span>
              )}
            </dd>
            <dt className="text-muted-foreground">{t("owner.billing.block.note")}</dt>
            <dd className="font-mono" data-testid="pay-note">{req.pay_to.note}</dd>
          </dl>
          {req.gst_paise === 0 && <p className="text-xs text-muted-foreground">{t("owner.billing.pay.noGst")}</p>}
          <p className="text-xs text-muted-foreground">{t("owner.billing.block.after")}</p>
        </div>
      </div>
    </section>
  );
}

function AutopayBlock({ billing, plan, monthlyOk, beforeStart }: { billing: OwnerBilling; plan: OwnerPlan; monthlyOk: boolean; beforeStart: boolean }) {
  const { t, language } = useI18n();
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: ["owner", "billing"] });
  const start = useMutation({
    mutationFn: () => api.post<AutopayMandate>("/owner/billing/autopay"),
    onSuccess: (m) => {
      refresh();
      if (m.checkout_url) window.location.assign(m.checkout_url);
    },
  });
  const cancel = useMutation({
    mutationFn: () => api.post<AutopayMandate>("/owner/billing/autopay/cancel"),
    onSuccess: () => {
      toast.success(t("owner.billing.pay.autopayCancelled"));
      setConfirm(false);
      refresh();
      qc.invalidateQueries({ queryKey: ["owner", "plan"] });
    },
  });
  const { available, price_month_paise: price, mandate } = billing.autopay;
  const live = mandate && (mandate.status === "created" || mandate.status === "authorised") ? mandate : null;
  if (!live && !monthlyOk) return null;
  return (
    <div className="rounded-2xl border p-4" data-testid="autopay">
      <h3 className="flex items-center gap-2 text-sm font-semibold"><Repeat className="size-4" aria-hidden /> {t("owner.billing.pay.monthly")}</h3>
      {price !== null && <p className="mt-1 text-sm">{t("owner.billing.pay.monthlyPrice", { price: rupees(live ? live.total_paise : price) })}</p>}
      {live?.status === "authorised" ? (
        <div className="mt-3 space-y-2">
          <p className="text-sm">{t("owner.billing.pay.autopayOn", { price: rupees(live.total_paise) })}</p>
          <Button variant="outline" onClick={() => setConfirm(true)}>{t("owner.billing.pay.autopayCancel")}</Button>
        </div>
      ) : live?.status === "created" ? (
        <div className="mt-3 space-y-2">
          <p className="text-sm text-muted-foreground">{t("owner.billing.pay.autopayWaiting")}</p>
          <div className="flex flex-wrap gap-2">
            {live.checkout_url && <Button asChild><a href={live.checkout_url}>{t("owner.billing.pay.autopayApprove")}</a></Button>}
            <Button variant="outline" onClick={() => setConfirm(true)}>{t("owner.billing.pay.autopayCancel")}</Button>
          </div>
        </div>
      ) : (
        <div className="mt-3 space-y-2">
          {mandate?.status === "halted" && <p className="text-sm text-destructive">{t("owner.billing.pay.autopayHalted")}</p>}
          {available ? (
            <Button onClick={() => start.mutate()} disabled={start.isPending}>
              {start.isPending && <Loader2 className="animate-spin" />} {t("owner.billing.pay.autopayStart")}
            </Button>
          ) : (
            <p className="text-sm text-muted-foreground">
              {beforeStart ? t("owner.billing.pay.autopayLater", { date: formatDay(plan.billing_starts_on, language) }) : t("owner.billing.pay.autopayUnavailable")}
            </p>
          )}
        </div>
      )}
      <Dialog open={confirm} onOpenChange={(o) => !cancel.isPending && setConfirm(o)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("owner.billing.pay.autopayCancelTitle")}</DialogTitle>
            <DialogDescription>{t("owner.billing.pay.autopayCancelBody")}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild><Button variant="outline" disabled={cancel.isPending}>{t("owner.billing.pay.autopayKeep")}</Button></DialogClose>
            <Button variant="destructive" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
              {cancel.isPending && <Loader2 className="animate-spin" />} {t("owner.billing.pay.autopayCancel")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function PaySection({ plan, billing, lapsed, beforeStart }: { plan: OwnerPlan; billing: OwnerBilling; lapsed: boolean; beforeStart: boolean }) {
  const { t, language } = useI18n();
  const qc = useQueryClient();
  const request = useMutation({
    mutationFn: () => api.post<PaymentRequest>("/owner/billing/requests", { plan_code: "growth", period: "year" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["owner", "billing"] }),
  });
  const growth: PlanVersion | undefined = plan.on_sale.find((p) => p.code === "growth");
  const sub = plan.subscription;
  if (plan.advance || !growth) return null;
  const mandate = billing.autopay.mandate;
  const autopayLive = !!mandate && (mandate.status === "created" || mandate.status === "authorised");
  const yearlyOk = !autopayLive && growth.price_year_paise !== null && (!sub || (sub.plan_code === "growth" && sub.period === "year"));
  const monthlyOk = !sub || (sub.plan_code === "growth" && sub.period === "month");
  if (!yearlyOk && !monthlyOk && !autopayLive) return null;
  const open = billing.requests.find((r) => r.status === "open" && r.plan_code === "growth" && r.period === "year");
  const founding = !sub && plan.founding.eligible && growth.founding_year_paise !== null;
  const yearPrice = founding ? growth.founding_year_paise! : growth.price_year_paise!;
  const renewing = !!sub || lapsed;
  return (
    <section aria-labelledby="pay-title" className="space-y-4 rounded-3xl border bg-card p-5" data-testid="pay-section">
      <h2 id="pay-title" className="flex items-center gap-2 font-semibold">
        <CreditCard className="size-5 text-primary" aria-hidden /> {t(renewing ? "owner.billing.pay.headingRenew" : "owner.billing.pay.heading")}
      </h2>
      {yearlyOk && (
        <div className="rounded-2xl border p-4">
          <h3 className="text-sm font-semibold">{t("owner.billing.pay.yearly")}</h3>
          <p className="mt-1 text-sm">
            {founding
              ? t("owner.billing.pay.foundingPrice", { price: rupees(yearPrice), list: rupees(growth.price_year_paise!) })
              : t("owner.billing.pay.yearlyPrice", { price: rupees(yearPrice) })}
          </p>
          {beforeStart && <p className="mt-1 text-xs text-muted-foreground">{t("owner.billing.pay.advanceNote", { date: formatDay(plan.billing_starts_on, language) })}</p>}
          {!billing.configured ? (
            <p className="mt-3 text-sm text-muted-foreground">{t("owner.billing.pay.notOpen")}</p>
          ) : !open ? (
            <Button className="mt-3" onClick={() => request.mutate()} disabled={request.isPending} data-testid="request-payment">
              {request.isPending && <Loader2 className="animate-spin" />} {t(lapsed ? "owner.billing.current.renew" : "owner.billing.pay.requestButton")}
            </Button>
          ) : null}
        </div>
      )}
      {yearlyOk && open && <PayNearShop req={open} />}
      <AutopayBlock billing={billing} plan={plan} monthlyOk={monthlyOk} beforeStart={beforeStart} />
    </section>
  );
}

function Guarantee({ plan }: { plan: OwnerPlan }) {
  const { t, language } = useI18n();
  const g = plan.guarantee;
  return (
    <div className="rounded-2xl border p-4" data-testid="guarantee">
      <h3 className="flex items-center gap-2 text-sm font-semibold"><ShieldCheck className="size-4" aria-hidden /> {t("owner.billing.guarantee.heading")}</h3>
      {g.applies ? (
        <div className="mt-1 space-y-1 text-sm">
          <p>{t("owner.billing.guarantee.progress", { count: formatNumber(g.customer_requests ?? 0), needed: formatNumber(g.needed) })}</p>
          {g.quarter_end && <p className="text-muted-foreground">{t("owner.billing.guarantee.quarterEnds", { date: formatDay(g.quarter_end, language) })}</p>}
          <p className="text-muted-foreground">
            {g.met ? t("owner.billing.guarantee.met") : t("owner.billing.guarantee.notMet", { needed: formatNumber(g.needed), months: formatNumber(g.credit_months ?? 3) })}
          </p>
        </div>
      ) : (
        <p className="mt-1 text-sm text-muted-foreground">{t("owner.billing.guarantee.notApplies", { needed: formatNumber(g.needed) })}</p>
      )}
    </div>
  );
}

function Founding({ plan }: { plan: OwnerPlan }) {
  const { t } = useI18n();
  const f = plan.founding;
  if (plan.plan.status !== "free" && !f.eligible) return null;
  const text = f.eligible && f.price_year_paise !== null
    ? t("owner.billing.founding.eligible", { price: rupees(f.price_year_paise), remaining: formatNumber(f.remaining), cap: formatNumber(f.cap) })
    : f.already_claimed ? t("owner.billing.founding.claimed")
      : !f.in_city ? t("owner.billing.founding.notInCity")
        : t("owner.billing.founding.full", { cap: formatNumber(f.cap) });
  return (
    <div className="rounded-2xl border p-4" data-testid="founding" data-eligible={f.eligible}>
      <h3 className="flex items-center gap-2 text-sm font-semibold"><Gift className="size-4" aria-hidden /> {t("owner.billing.founding.heading")}</h3>
      <p className="mt-1 text-sm">{text}</p>
    </div>
  );
}

function History({ billing }: { billing: OwnerBilling }) {
  const { t, language } = useI18n();
  return (
    <section aria-labelledby="history-title" className="rounded-3xl border bg-card p-5">
      <h2 id="history-title" className="flex items-center gap-2 font-semibold"><FileText className="size-5 text-primary" aria-hidden /> {t("owner.billing.history.heading")}</h2>
      {billing.payments.length === 0 ? (
        <p className="mt-2 text-sm text-muted-foreground">{t("owner.billing.history.empty")}</p>
      ) : (
        <ul className="mt-3 divide-y">
          {billing.payments.map((p) => (
            <li key={p.id} className="flex flex-wrap items-start justify-between gap-2 py-3 text-sm">
              <div className="min-w-0">
                <p className="font-medium">
                  {rupees(p.amount_paise)} {p.status === "refunded" && <Badge variant="destructive" className="ml-1">{t("owner.billing.history.refunded")}</Badge>}
                </p>
                <p className="text-muted-foreground">{t("owner.billing.history.paidOn", { date: formatDay(p.paid_at, language) })}</p>
                <p className="break-all text-xs text-muted-foreground">{t("owner.billing.history.upiReference", { reference: p.upi_reference })}</p>
              </div>
              <div className="flex flex-col items-start gap-1 sm:items-end">
                {p.documents.map((d) => (
                  <Link key={d.id} href={`/shop/billing/documents/${d.id}`} className="font-medium text-primary underline underline-offset-4">
                    {t(`owner.billing.docTypes.${d.type}`)} {d.number}
                  </Link>
                ))}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function BillingPage() {
  const { t, language } = useI18n();
  const plan = useQuery({ queryKey: ["owner", "plan"], queryFn: () => api.get<OwnerPlan>("/owner/plan") });
  const billing = useQuery({ queryKey: ["owner", "billing"], queryFn: () => api.get<OwnerBilling>("/owner/billing") });
  // When the page was opened: the next reminder is worked out from it.
  const [now] = useState(() => Date.now());
  const terms = richText(t("owner.billing.terms"), {
    terms: <Link href={PLAN_TERMS_HREF} className="font-medium text-foreground underline underline-offset-4">{t("owner.billing.termsLink")}</Link>,
  });

  const header = <PageTitle title={t("owner.billing.title")} description={<>{t("owner.billing.description")} {terms}</>} />;
  if (plan.isLoading || billing.isLoading) return <>{header}<ListSkeleton rows={3} /></>;
  if (plan.error || billing.error || !plan.data || !billing.data) {
    return <>{header}<ErrorState error={plan.error ?? billing.error} onRetry={() => { plan.refetch(); billing.refetch(); }} /></>;
  }
  const p = plan.data;
  const b = billing.data;
  const current = p.plan;
  const sub = p.subscription;
  const mandate = b.autopay.mandate;
  // owner_plan names only an active subscription: a Free shop that has paid NearShop before (a payment not refunded,
  // or an Autopay mandate that stopped) has had a plan that ended.
  const lapsed = current.status === "free" && !p.advance
    && (b.payments.some((x) => x.status === "matched") || mandate?.status === "halted" || mandate?.status === "cancelled");
  const autopayRenews = mandate?.status === "authorised" && sub?.period === "month";
  const reminder = sub?.ends_at && !autopayRenews ? nextReminder(sub.ends_at, sub.period, now) : null;
  const statusKey: MessageKey = `owner.billing.status.${lapsed ? "lapsed" : current.status}`;

  return (
    <>
      {header}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="space-y-6">
          <section aria-labelledby="current-plan-title" className="space-y-4 rounded-3xl border bg-card p-5" data-testid="current-plan" data-plan={current.code} data-status={lapsed ? "lapsed" : current.status}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <p className="text-sm text-muted-foreground" id="current-plan-title">{t("owner.billing.current.heading")}</p>
                <p className="text-2xl font-semibold" data-testid="plan-name">{current.name}</p>
              </div>
              <Badge variant={current.status === "active" ? "default" : "secondary"}>{t(statusKey)}</Badge>
            </div>
            {lapsed && <p className="text-sm" data-testid="lapsed-note">{t("owner.billing.current.lapsed")}</p>}
            <div className="space-y-1 text-sm">
              {current.ends_at ? (
                <p className="flex items-center gap-2"><CalendarClock className="size-4" aria-hidden /> {t("owner.billing.current.endsOn", { date: formatDay(current.ends_at, language) })}</p>
              ) : (
                <p className="text-muted-foreground">{t("owner.billing.current.freeNoEnd")}</p>
              )}
              {autopayRenews && <p className="text-muted-foreground">{t("owner.billing.current.renewsByAutopay")}</p>}
              {reminder && <p className="text-muted-foreground">{t("owner.billing.current.nextReminder", { date: formatDay(reminder, language) })}</p>}
            </div>
            <PlanIncludes e={current.entitlements} needed={p.guarantee.needed} />
            {p.advance && (
              <div className="rounded-2xl border p-4" data-testid="advance">
                <h3 className="text-sm font-semibold">{t("owner.billing.advance.heading")}</h3>
                <p className="mt-1 text-sm">{t("owner.billing.advance.body", { date: formatDay(p.advance.starts_at ?? p.billing_starts_on, language) })}</p>
              </div>
            )}
            <Founding plan={p} />
            <Guarantee plan={p} />
          </section>
          <FreeTools />
        </div>
        <div className="space-y-6">
          <PaySection plan={p} billing={b} lapsed={lapsed} beforeStart={now < new Date(`${p.billing_starts_on}T00:00:00+05:30`).getTime()} />
          <History billing={b} />
        </div>
      </div>
    </>
  );
}
