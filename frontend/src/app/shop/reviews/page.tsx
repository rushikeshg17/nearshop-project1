"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, MessageSquareReply, Pencil, Star } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { useI18n } from "@/components/providers";
import { ReviewList } from "@/components/shop/review-list";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { formatNumber, timeAgo } from "@/lib/format";
import { languageInfo, plural } from "@/lib/i18n";
import type { Review } from "@/lib/types";

const REPLY_EDIT_HOURS = 24; // backend/app/services/reviews.py, REPLY_EDIT_HOURS
const MAX_REPLY = 1000;

function editableUntil(createdAt: string) {
  return new Date(createdAt).getTime() + REPLY_EDIT_HOURS * 3_600_000;
}

/**
 * The shop's one public reply to a review (P3.6, plan section 8.2): written once, editable for 24 hours after it is
 * written and while no one has reported it, then fixed.
 */
function ReplyEditor({ review }: { review: Review }) {
  const { t, language } = useI18n();
  const qc = useQueryClient();
  const id = useId();
  const reply = review.reply;
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(reply?.text ?? "");
  // When the page was opened: past the 24 hours the API refuses an edit anyway (409 reply_locked), with a message.
  const [now] = useState(() => Date.now());
  const save = useMutation({
    mutationFn: () =>
      reply ? api.put(`/owner/reviews/${review.id}/reply`, { text: text.trim() }) : api.post(`/owner/reviews/${review.id}/reply`, { text: text.trim() }),
    onSuccess: () => {
      toast.success(reply ? t("owner.reviews.toast.updated") : t("owner.reviews.toast.posted"));
      setEditing(false);
      qc.invalidateQueries({ queryKey: ["owner", "reviews"] });
    },
  });

  if (review.status === "removed") return null;
  const hours = t(`owner.reviews.editWindow.${plural(language, REPLY_EDIT_HOURS)}`, { count: formatNumber(REPLY_EDIT_HOURS) });
  const canEdit = !!reply && now < editableUntil(reply.created_at) && !reply.under_review && reply.status === "visible";

  if (reply && !editing) {
    return (
      <div className="mt-3 rounded-xl border-l-2 border-brand/50 bg-muted/50 px-3 py-2">
        <p className="flex flex-wrap items-center gap-1.5 text-xs font-medium">
          <MessageSquareReply className="size-3.5" aria-hidden /> {t("owner.reviews.yourReply")}
          <span className="font-normal text-muted-foreground" suppressHydrationWarning>
            · {timeAgo(reply.created_at, undefined, language)}
            {reply.edited_at ? ` · ${t("owner.reviews.edited")}` : ""}
          </span>
          {reply.under_review && <Badge variant="outline">{t("owner.reviews.reported")}</Badge>}
          {reply.status && reply.status !== "visible" && (
            <Badge variant="outline">{reply.status === "hidden" ? t("owner.reviews.hiddenByAdmin") : t("owner.reviews.removedByAdmin")}</Badge>
          )}
        </p>
        <p className="mt-1 text-sm">{reply.text}</p>
        {canEdit ? (
          <Button size="sm" variant="ghost" className="mt-1 h-7 px-2" onClick={() => { setText(reply.text); setEditing(true); }}>
            <Pencil /> {t("owner.reviews.edit")}
            <span className="font-normal text-muted-foreground" suppressHydrationWarning>
              {t("owner.reviews.editUntil", {
                time: new Date(editableUntil(reply.created_at)).toLocaleString(languageInfo(language).htmlLang, {
                  day: "numeric",
                  month: "short",
                  hour: "numeric",
                  minute: "2-digit",
                }),
              })}
            </span>
          </Button>
        ) : (
          <p className="mt-1 text-xs text-muted-foreground">
            {reply.under_review ? t("owner.reviews.lockedReported") : hours}
          </p>
        )}
      </div>
    );
  }

  if (!reply && !editing) {
    return (
      <Button size="sm" variant="outline" className="mt-3" onClick={() => setEditing(true)}>
        <MessageSquareReply /> {t("owner.reviews.replyPublicly")}
      </Button>
    );
  }

  return (
    <form
      className="mt-3 space-y-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (text.trim()) save.mutate();
      }}
    >
      <Label htmlFor={`${id}-reply`}>{reply ? t("owner.reviews.editLabel") : t("owner.reviews.newLabel")}</Label>
      <Textarea id={`${id}-reply`} rows={3} value={text} onChange={(e) => setText(e.target.value.slice(0, MAX_REPLY))}
        aria-describedby={`${id}-hint`} placeholder={t("owner.reviews.placeholder")} />
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">
        {t(`owner.reviews.hint.${plural(language, REPLY_EDIT_HOURS)}`, { count: formatNumber(REPLY_EDIT_HOURS) })}
      </p>
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={!text.trim() || save.isPending}>
          {save.isPending && <Loader2 className="animate-spin" />} {reply ? t("owner.reviews.save") : t("owner.reviews.post")}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)} disabled={save.isPending}>{t("owner.reviews.cancel")}</Button>
      </div>
    </form>
  );
}

export default function OwnerReviewsPage() {
  const { t, language } = useI18n();
  const q = useQuery({ queryKey: ["owner", "reviews"], queryFn: () => api.get<Review[]>("/owner/reviews") });
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  if (!q.data) return <ListSkeleton />;
  const reviews = q.data;
  // The summary counts what customers can see: not the reviews moderation hid or removed.
  const shown = reviews.filter((r) => !r.status || r.status === "visible");
  const avg = shown.length ? shown.reduce((s, r) => s + r.rating, 0) / shown.length : 0;
  const dist = [5, 4, 3, 2, 1].map((n) => ({ n, c: shown.filter((r) => r.rating === n).length }));

  return (
    <>
      <PageTitle title={t("owner.reviews.title")} description={t("owner.reviews.description")} />
      {!reviews.length ? (
        <EmptyState icon={Star} title={t("owner.reviews.emptyTitle")} description={t("owner.reviews.emptyBody")} />
      ) : (
        <div className="grid gap-6 lg:grid-cols-[260px_1fr]">
          <div className="h-fit rounded-2xl border bg-card p-5">
            <p className="font-heading text-4xl font-semibold tabular">{shown.length ? avg.toFixed(1) : "-"}</p>
            <p className="text-sm text-muted-foreground">{t(`owner.reviews.fromLatest.${plural(language, shown.length)}`, { count: formatNumber(shown.length) })}</p>
            <ul className="mt-4 space-y-1.5">
              {dist.map(({ n, c }) => (
                <li key={n} className="flex items-center gap-2 text-xs">
                  <span className="w-3 tabular">{n}</span>
                  <Star className="size-3 fill-warning text-warning" />
                  <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                    <span className="block h-full rounded-full bg-warning" style={{ width: `${shown.length ? (c / shown.length) * 100 : 0}%` }} />
                  </span>
                  <span className="w-6 text-right text-muted-foreground tabular">{c}</span>
                </li>
              ))}
            </ul>
            <p className="mt-4 text-xs text-muted-foreground">
              {t("owner.reviews.ratingNote")}
            </p>
          </div>
          <div className="rounded-2xl border bg-card p-5">
            <ReviewList reviews={reviews} owner footer={(r) => <ReplyEditor review={r} />} />
          </div>
        </div>
      )}
    </>
  );
}
