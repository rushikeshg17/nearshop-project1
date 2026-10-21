"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, MessageSquareReply, Pencil, Star } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { ReviewList } from "@/components/shop/review-list";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { timeAgo } from "@/lib/format";
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
      toast.success(reply ? "Reply updated." : "Reply posted. Customers see it under the review.");
      setEditing(false);
      qc.invalidateQueries({ queryKey: ["owner", "reviews"] });
    },
  });

  if (review.status === "removed") return null;
  const canEdit = !!reply && now < editableUntil(reply.created_at) && !reply.under_review && reply.status === "visible";

  if (reply && !editing) {
    return (
      <div className="mt-3 rounded-xl border-l-2 border-brand/50 bg-muted/50 px-3 py-2">
        <p className="flex flex-wrap items-center gap-1.5 text-xs font-medium">
          <MessageSquareReply className="size-3.5" aria-hidden /> Your reply
          <span className="font-normal text-muted-foreground" suppressHydrationWarning>· {timeAgo(reply.created_at)}{reply.edited_at ? " · edited" : ""}</span>
          {reply.under_review && <Badge variant="outline">Reported, waiting for an admin</Badge>}
          {reply.status && reply.status !== "visible" && <Badge variant="outline">{reply.status === "hidden" ? "Hidden" : "Removed"} by an admin</Badge>}
        </p>
        <p className="mt-1 text-sm">{reply.text}</p>
        {canEdit ? (
          <Button size="sm" variant="ghost" className="mt-1 h-7 px-2" onClick={() => { setText(reply.text); setEditing(true); }}>
            <Pencil /> Edit
            <span className="font-normal text-muted-foreground" suppressHydrationWarning>
              (until {new Date(editableUntil(reply.created_at)).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })})
            </span>
          </Button>
        ) : (
          <p className="mt-1 text-xs text-muted-foreground">
            {reply.under_review ? "A reported reply cannot be edited while an admin reviews it." : `Replies can be edited for ${REPLY_EDIT_HOURS} hours after they are posted.`}
          </p>
        )}
      </div>
    );
  }

  if (!reply && !editing) {
    return (
      <Button size="sm" variant="outline" className="mt-3" onClick={() => setEditing(true)}>
        <MessageSquareReply /> Reply publicly
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
      <Label htmlFor={`${id}-reply`}>{reply ? "Edit your reply" : "Your public reply"}</Label>
      <Textarea id={`${id}-reply`} rows={3} value={text} onChange={(e) => setText(e.target.value.slice(0, MAX_REPLY))}
        aria-describedby={`${id}-hint`} placeholder="Thank the customer, or explain what you have fixed." />
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">
        Shown under the review on your shop page. One reply per review, editable for {REPLY_EDIT_HOURS} hours.
      </p>
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={!text.trim() || save.isPending}>
          {save.isPending && <Loader2 className="animate-spin" />} {reply ? "Save reply" : "Post reply"}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)} disabled={save.isPending}>Cancel</Button>
      </div>
    </form>
  );
}

export default function OwnerReviewsPage() {
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
      <PageTitle title="Reviews" description="Only customers who completed a pickup or delivery can review, once per purchase. Reply once to each review, in public." />
      {!reviews.length ? (
        <EmptyState icon={Star} title="No reviews yet" description="After a pickup or delivery is completed, the customer is invited to rate it." />
      ) : (
        <div className="grid gap-6 lg:grid-cols-[260px_1fr]">
          <div className="h-fit rounded-2xl border bg-card p-5">
            <p className="font-heading text-4xl font-semibold tabular">{shown.length ? avg.toFixed(1) : "-"}</p>
            <p className="text-sm text-muted-foreground">from the latest {shown.length} reviews</p>
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
              Your public rating counts one review per customer every 30 days, and shows once 5 customers have reviewed you.
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
