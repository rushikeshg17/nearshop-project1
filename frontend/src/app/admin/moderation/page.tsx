"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, EyeOff, Flag, Hand, Loader2, Trash2 } from "lucide-react";
import Link from "next/link";
import { useId, useState } from "react";
import { toast } from "sonner";

import { useModerationQueue } from "@/app/admin/admin-shell";
import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { Stars } from "@/components/shop/review-list";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import type { ModerationQueueItem, ReviewReportReason, ReviewSla } from "@/lib/types";
import { cn } from "@/lib/utils";

const REASON_LABEL: Record<ReviewReportReason, string> = {
  fake: "Fake",
  abusive: "Abusive",
  personal_info: "Personal information",
  off_topic: "Off-topic",
  other: "Other",
};
const FLAG_LABEL: Record<ModerationQueueItem["flags"][number], string> = {
  reported: "Reported",
  held_self_dealing: "Held: self-dealing",
  hidden_by_reports: "Hidden by reports",
};
const DECISIONS = [
  { value: "keep", label: "Keep", icon: Check, done: "Kept. It is visible again and counts toward the rating." },
  { value: "hide", label: "Hide", icon: EyeOff, done: "Hidden from customers." },
  { value: "remove", label: "Remove", icon: Trash2, done: "Removed." },
] as const;

function age(hours: number) {
  if (hours < 1) return "under 1 h";
  if (hours < 48) return `${Math.floor(hours)} h`;
  return `${Math.floor(hours / 24)} d ${Math.floor(hours % 24)} h`;
}

function share(value: number | null) {
  return value === null ? "-" : `${Math.round(value * 100)}%`;
}

/** This month in India time, as YYYY-MM (the SLA endpoint's default). */
function thisMonth() {
  return new Date(Date.now() + 5.5 * 3_600_000).toISOString().slice(0, 7);
}

function SlaPanel() {
  const id = useId();
  const [month, setMonth] = useState(thisMonth);
  const sla = useQuery({
    queryKey: ["admin", "moderation", "sla", month],
    queryFn: () => api.get<ReviewSla>("/admin/reviews/sla", { month }),
  });
  const d = sla.data;
  return (
    <section aria-labelledby={`${id}-title`} className="mb-6 rounded-2xl border bg-card p-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id={`${id}-title`} className="font-semibold">Monthly SLA</h2>
          <p className="text-sm text-muted-foreground">Targets: triage within 24 hours, a decision within 72 hours. Items still inside a deadline are not counted yet.</p>
        </div>
        <div className="space-y-1">
          <Label htmlFor={`${id}-month`} className="text-xs">Month</Label>
          <Input id={`${id}-month`} type="month" value={month} max={thisMonth()} onChange={(e) => e.target.value && setMonth(e.target.value)} className="h-9 w-40" />
        </div>
      </div>
      {sla.isError ? (
        <p className="mt-3 text-sm text-destructive">Could not load the SLA figures.</p>
      ) : !d ? (
        <ListSkeleton rows={1} />
      ) : (
        <dl className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="rounded-xl bg-muted/60 p-3">
            <dt className="text-xs text-muted-foreground">Triaged within 24 h</dt>
            <dd className="font-heading text-2xl font-semibold tabular">{share(d.triaged_share)}</dd>
            <dd className="text-xs text-muted-foreground tabular">{d.triaged_within_24h} of {d.triage_counted}</dd>
          </div>
          <div className="rounded-xl bg-muted/60 p-3">
            <dt className="text-xs text-muted-foreground">Decided within 72 h</dt>
            <dd className="font-heading text-2xl font-semibold tabular">{share(d.decided_share)}</dd>
            <dd className="text-xs text-muted-foreground tabular">{d.decided_within_72h} of {d.decision_counted}</dd>
          </div>
          <div className="rounded-xl bg-muted/60 p-3">
            <dt className="text-xs text-muted-foreground">Entered the queue</dt>
            <dd className="font-heading text-2xl font-semibold tabular">{d.items}</dd>
          </div>
          <div className="rounded-xl bg-muted/60 p-3">
            <dt className="text-xs text-muted-foreground">Still undecided</dt>
            <dd className="font-heading text-2xl font-semibold tabular">{d.open}</dd>
          </div>
        </dl>
      )}
    </section>
  );
}

function QueueItem({ item, triageHours, decideHours }: { item: ModerationQueueItem; triageHours: number; decideHours: number }) {
  const qc = useQueryClient();
  const body = { target: item.target };
  const triage = useMutation({
    mutationFn: () => api.post(`/admin/reviews/${item.review_id}/triage`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "moderation"] }),
  });
  const decide = useMutation({
    mutationFn: (decision: (typeof DECISIONS)[number]["value"]) => api.post(`/admin/reviews/${item.review_id}/decision`, { ...body, decision }),
    onSuccess: (_d, decision) => {
      toast.success(DECISIONS.find((x) => x.value === decision)?.done ?? "Decided.");
      qc.invalidateQueries({ queryKey: ["admin", "moderation"] });
    },
  });
  const busy = triage.isPending || decide.isPending;
  const overdue = item.age_hours > decideHours || (!item.triaged_at && item.age_hours > triageHours);
  const reasons = Object.entries(item.reports.reasons) as [ReviewReportReason, number][];
  const label = item.target === "reply" ? `The shop's reply to review ${item.review_id}` : `Review ${item.review_id}`;

  return (
    <li className="rounded-2xl border bg-card p-4" aria-label={label}>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Badge variant={item.target === "reply" ? "secondary" : "outline"}>{item.target === "reply" ? "Shop reply" : "Review"}</Badge>
        {item.flags.map((f) => (
          <Badge key={f} className={cn(f === "held_self_dealing" ? "bg-warning-soft text-warning-ink" : "bg-destructive/10 text-destructive")}>{FLAG_LABEL[f] ?? f}</Badge>
        ))}
        <span className={cn("ml-auto tabular", overdue ? "font-semibold text-destructive" : "text-muted-foreground")}>
          Waiting {age(item.age_hours)}{overdue && " (over target)"}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
        <Stars value={item.rating} />
        {item.shop ? (
          <Link href={`/shops/${item.shop.slug}#reviews`} className="font-medium hover:underline">{item.shop.name}</Link>
        ) : (
          <span className="text-muted-foreground">Shop {item.shop_id}</span>
        )}
      </div>
      {item.comment && <p className={cn("mt-1.5 text-sm", item.target === "reply" && "text-muted-foreground")}>{item.comment}</p>}
      {item.target === "reply" && item.reply_text && (
        <p className="mt-2 rounded-xl border-l-2 border-brand/50 bg-muted/50 px-3 py-2 text-sm">
          <span className="block text-xs font-medium">Reply from the shop</span>
          {item.reply_text}
        </p>
      )}
      {item.reports.count > 0 && (
        <div className="mt-2 text-xs text-muted-foreground">
          <p className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
            <Flag className="size-3.5" aria-hidden /> {item.reports.count} report{item.reports.count === 1 ? "" : "s"}:
            {reasons.map(([r, n]) => <span key={r}>{REASON_LABEL[r] ?? r} {n}</span>)}
          </p>
          {item.reports.notes.length > 0 && (
            <ul className="mt-1 list-disc pl-5">
              {item.reports.notes.map((n, i) => <li key={i}>&ldquo;{n}&rdquo;</li>)}
            </ul>
          )}
        </div>
      )}
      <div className="mt-3 flex flex-wrap gap-2 border-t pt-3">
        {!item.triaged_at && (
          <Button size="sm" variant="ghost" onClick={() => triage.mutate()} disabled={busy}>
            {triage.isPending ? <Loader2 className="animate-spin" /> : <Hand />} Pick up
          </Button>
        )}
        {DECISIONS.filter((d) => item.actions.includes(d.value)).map((d) => (
          <Button key={d.value} size="sm" variant={d.value === "keep" ? "outline" : d.value === "remove" ? "destructive" : "secondary"}
            onClick={() => decide.mutate(d.value)} disabled={busy}>
            {decide.isPending && decide.variables === d.value ? <Loader2 className="animate-spin" /> : <d.icon />} {d.label}
          </Button>
        ))}
      </div>
    </li>
  );
}

export default function AdminModerationPage() {
  const queue = useModerationQueue();
  return (
    <>
      <PageTitle
        title="Moderation"
        description="Reported reviews and shop replies, and reviews held out of a rating by the self-dealing rule, oldest first. Keep puts a review back; hide and remove take it off the shop's page. Every decision recomputes the shop's rating."
      />
      <SlaPanel />
      {queue.isError ? (
        <ErrorState error={queue.error} onRetry={() => queue.refetch()} />
      ) : !queue.data ? (
        <ListSkeleton />
      ) : !queue.data.items.length ? (
        <EmptyState icon={Flag} title="Nothing to moderate" description="Reported reviews and replies, and held reviews, appear here." />
      ) : (
        <>
          {queue.data.total > queue.data.items.length && (
            <p className="mb-3 text-sm text-muted-foreground">
              Showing the oldest {queue.data.items.length} of {queue.data.total}. Decide these and the rest move up.
            </p>
          )}
          <ul className="space-y-3">
            {queue.data.items.map((item) => (
              <QueueItem key={`${item.target}-${item.review_id}`} item={item} triageHours={queue.data.triage_hours} decideHours={queue.data.decide_hours} />
            ))}
          </ul>
        </>
      )}
    </>
  );
}
