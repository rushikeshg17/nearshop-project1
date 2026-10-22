"use client";

import { useQuery } from "@tanstack/react-query";
import { CalendarDays, FileText, Info, MessageSquareReply, SearchX } from "lucide-react";

import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { useI18n } from "@/components/providers";
import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";
import type { OwnerStatement, OwnerStatements } from "@/lib/types";

import { formatDay, formatMonth, LeadCountList } from "../owner-shell";

/**
 * The shop's monthly statements (P9.11; backend P9.9, plan section 8.18): one card per month with the counts as they
 * were sent, the ₹0-due line or the plan line, and the reply recorded; and the current month so far. A statement states
 * counts of customer actions on NearShop only: NearShop cannot see the shop's sales.
 */
export default function StatementsPage() {
  const { t, language } = useI18n();
  const q = useQuery({ queryKey: ["owner", "statements"], queryFn: () => api.get<OwnerStatements>("/owner/statements") });
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  const d = q.data;
  if (!d) return <ListSkeleton rows={3} />;

  return (
    <>
      <PageTitle title={t("owner.statements.title")} description={t("owner.statements.description")} />

      <p className="mb-6 flex gap-2 rounded-xl bg-info-soft px-3 py-2 text-sm text-info-ink" data-testid="statements-note">
        <Info className="mt-0.5 size-4 shrink-0" /> <span>{t("owner.statements.note")}</span>
      </p>

      <section className="rounded-2xl border bg-card p-5" aria-labelledby="month-so-far-title" data-testid="month-so-far">
        <h2 id="month-so-far-title" className="flex items-center gap-2 font-semibold">
          <CalendarDays className="size-4 text-brand" /> {t("owner.overview.monthSoFar.title")}
          <span className="font-normal text-muted-foreground">· {formatMonth(d.current.month, language)}</span>
        </h2>
        <p className="mt-1 text-xs text-muted-foreground">{t("owner.statements.soFarHint")}</p>
        <LeadCountList counts={d.current} className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5" />
      </section>

      {d.statements.length === 0 ? (
        <EmptyState icon={FileText} title={t("owner.statements.emptyTitle")} description={t("owner.statements.emptyBody")} className="mt-6" />
      ) : (
        <ul className="mt-6 space-y-4">
          {d.statements.map((s, i) => (
            <li key={s.id}>
              <StatementCard s={s} latest={i === 0} />
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

// A WhatsApp reply answers only the shop's latest statement, and only while it is at most this old (backend
// statements.REPLY_WINDOW); a statement that never went out on WhatsApp cannot be answered there at all.
const REPLY_WINDOW_DAYS = 35;
const ON_WHATSAPP = new Set<OwnerStatement["delivery"]>(["queued", "sent", "delivered"]);

function canReplyOnWhatsApp(s: OwnerStatement, latest: boolean) {
  if (!latest || !ON_WHATSAPP.has(s.delivery) || !s.built_at) return false;
  return Date.now() - new Date(s.built_at).getTime() <= REPLY_WINDOW_DAYS * 24 * 60 * 60 * 1000;
}

function StatementCard({ s, latest }: { s: OwnerStatement; latest: boolean }) {
  const { t, language } = useI18n();
  const titleId = `statement-${s.id}-title`;
  return (
    <article className="rounded-2xl border bg-card p-5" aria-labelledby={titleId} data-testid="statement-card" data-month={s.month} data-kind={s.kind}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id={titleId} className="text-lg font-semibold">{t("owner.statements.cardTitle", { month: formatMonth(s.month, language) })}</h2>
        {s.delivery && <Badge variant="outline">{t(`owner.statements.delivery.${s.delivery}`)}</Badge>}
      </div>

      <LeadCountList counts={s.counts} className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5" testId="statement-counts" />

      {s.asked_for.length > 0 && (
        <div className="mt-4 text-sm">
          <p className="flex items-center gap-2 font-medium"><SearchX className="size-4 text-brand" /> {t("owner.statements.askedFor")}</p>
          <ul className="mt-2 flex flex-wrap gap-2">
            {s.asked_for.map((q) => <li key={q} className="rounded-full border px-3 py-1 capitalize">{q}</li>)}
          </ul>
        </div>
      )}

      {/* The price or plan lines as the statement sent them, in the owner's language (the only rupee amounts in it). */}
      <div className="mt-4 space-y-1 border-t pt-4 text-sm" data-testid="statement-price">
        {s.kind === "roi" ? (
          <>
            {s.text.plan && <p><span className="font-medium">{t("owner.statements.yourPlan")}:</span> {s.text.plan}</p>}
            {s.text.guarantee && <p><span className="font-medium">{t("owner.statements.guarantee")}:</span> {s.text.guarantee}</p>}
          </>
        ) : (
          <>
            {s.text.amount_due && <p className="font-medium">{s.text.amount_due}</p>}
            {s.text.price && <p className="text-muted-foreground">{s.text.price}</p>}
          </>
        )}
      </div>

      <div className="mt-4 border-t pt-4 text-sm" data-testid="statement-reply">
        <p className="flex items-center gap-2 font-medium"><MessageSquareReply className="size-4 text-brand" /> {t("owner.statements.reply.label")}</p>
        {s.replies.length ? (
          <ul className="mt-1 space-y-1">
            {s.replies.map((r) => (
              <li key={`${r.kind}-${r.at}`}>
                {r.at
                  ? t("owner.statements.reply.on", { reply: t(`owner.statements.reply.kinds.${r.kind}`), date: formatDay(r.at, language) })
                  : t(`owner.statements.reply.kinds.${r.kind}`)}
                {r.reason && <span className="block text-muted-foreground">{t("owner.statements.reply.reason", { reason: r.reason })}</span>}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-muted-foreground">{t(canReplyOnWhatsApp(s, latest) ? "owner.statements.reply.noneCanReply" : "owner.statements.reply.none")}</p>
        )}
      </div>
    </article>
  );
}
