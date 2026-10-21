"use client";

import { useMutation } from "@tanstack/react-query";
import { BadgeCheck, Flag, Loader2, MessageSquareReply, Sparkles, Star } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useId, useState } from "react";
import { toast } from "sonner";

import { ResponsiveDialog } from "@/components/common/responsive-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Textarea } from "@/components/ui/textarea";
import { useMe } from "@/hooks/use-session";
import { api } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import type { Review, ReviewReportReason } from "@/lib/types";
import { cn } from "@/lib/utils";

export function Stars({ value, className }: { value: number; className?: string }) {
  return (
    <span role="img" className={cn("inline-flex", className)} aria-label={`${value} out of 5 stars`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <Star key={i} className={cn("size-3.5", i <= value ? "fill-warning text-warning" : "text-muted-foreground/30")} />
      ))}
    </span>
  );
}

/**
 * A shop's rating for the heading of its reviews: stars and the average, or "New shop" while the API has no
 * rating_avg for it (fewer than 5 distinct customers with counted reviews, P3.5, plan section 8.2).
 */
export function RatingSummary({ avg, count, className }: { avg: number | null; count?: number; className?: string }) {
  if (avg === null || avg === undefined) {
    return (
      <span className={cn("inline-flex items-center gap-1.5 text-sm", className)} title="A rating shows once 5 different customers have reviewed this shop">
        <Badge variant="secondary" className="gap-1"><Sparkles className="size-3" aria-hidden /> New shop</Badge>
      </span>
    );
  }
  return (
    <span className={cn("inline-flex items-center gap-2 text-sm", className)}>
      <Stars value={Math.round(avg)} /> <span className="font-medium tabular">{avg.toFixed(1)}</span>
      {count ? <span className="text-muted-foreground tabular">({count})</span> : null}
    </span>
  );
}

const REASONS: { value: ReviewReportReason; label: string; hint: string }[] = [
  { value: "fake", label: "Fake", hint: "Not from a real customer, or written by the shop" },
  { value: "abusive", label: "Abusive", hint: "Insults, threats or hate" },
  { value: "personal_info", label: "Personal information", hint: "A phone number, address or someone's private details" },
  { value: "off_topic", label: "Off-topic", hint: "Not about this shop or this purchase" },
  { value: "other", label: "Something else", hint: "Tell us below" },
];

/** Report a review, or the shop's reply to it (P3.6): one report per person, up to 10 a day. */
function ReportDialog({
  reviewId,
  target,
  open,
  onOpenChange,
  onReported,
}: {
  reviewId: number;
  target: "review" | "reply";
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onReported: () => void;
}) {
  const id = useId();
  const [reason, setReason] = useState<ReviewReportReason | "">("");
  const [note, setNote] = useState("");
  const send = useMutation({
    mutationFn: () =>
      api.post<{ ok: boolean; already_reported: boolean }>(`/reviews/${reviewId}/${target === "reply" ? "reply/report" : "report"}`, {
        reason,
        note: note.trim() || null,
      }),
    onSuccess: (d) => {
      toast.success(d.already_reported ? "You have already reported this." : "Thanks. An admin will look at it within 3 days.");
      onReported();
      onOpenChange(false);
    },
  });
  return (
    <ResponsiveDialog
      open={open}
      onOpenChange={(o) => !send.isPending && onOpenChange(o)}
      title={target === "reply" ? "Report the shop's reply" : "Report this review"}
      description="Reports are private. When several people report the same review, it is hidden until an admin decides."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={send.isPending}>Cancel</Button>
          <Button type="submit" form={`${id}-form`} disabled={!reason || (reason === "other" && !note.trim()) || send.isPending}>
            {send.isPending && <Loader2 className="animate-spin" />} Send report
          </Button>
        </>
      }
    >
      <form
        id={`${id}-form`}
        className="space-y-4 pb-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (reason) send.mutate();
        }}
      >
        <fieldset className="space-y-2">
          <legend className="mb-2 text-sm font-medium">What is wrong with it?</legend>
          <RadioGroup value={reason} onValueChange={(v) => setReason(v as ReviewReportReason)} className="gap-2">
            {REASONS.map((r) => (
              <div key={r.value} className="flex items-start gap-2.5">
                <RadioGroupItem value={r.value} id={`${id}-${r.value}`} className="mt-0.5" />
                <Label htmlFor={`${id}-${r.value}`} className="flex-col items-start gap-0 font-normal">
                  <span className="font-medium">{r.label}</span>
                  <span className="text-xs text-muted-foreground">{r.hint}</span>
                </Label>
              </div>
            ))}
          </RadioGroup>
        </fieldset>
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-note`}>{reason === "other" ? "What is the problem?" : "Anything else? (optional)"}</Label>
          <Textarea id={`${id}-note`} rows={2} value={note} onChange={(e) => setNote(e.target.value.slice(0, 500))} />
        </div>
      </form>
    </ResponsiveDialog>
  );
}

function ReportButton({ reviewId, target }: { reviewId: number; target: "review" | "reply" }) {
  const { data: me } = useMe();
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [reported, setReported] = useState(false);
  const label = target === "reply" ? "Report reply" : "Report";
  if (reported) return <span className="text-xs text-muted-foreground">Reported</span>;
  if (!me) {
    return (
      <Link href={`/login?next=${encodeURIComponent(pathname)}`} className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground hover:underline">
        <Flag className="size-3" aria-hidden /> {label}
      </Link>
    );
  }
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1 rounded text-xs text-muted-foreground hover:text-foreground hover:underline"
        aria-label={target === "reply" ? "Report the shop's reply" : "Report this review"}
      >
        <Flag className="size-3" aria-hidden /> {label}
      </button>
      {open && <ReportDialog reviewId={reviewId} target={target} open onOpenChange={setOpen} onReported={() => setReported(true)} />}
    </>
  );
}

const STATUS_LABEL: Record<string, string> = { hidden: "Hidden while an admin reviews it", removed: "Removed by an admin" };

/**
 * Reviews with the shop's public reply under each one, and a report action (P3.6). With `rating`, a heading line
 * shows the shop's rating, or "New shop" when it has none yet. `owner` is the shop owner's own list: it shows each
 * review's moderation status and leaves the reply to `footer` (the reply editor), and the owner cannot report it.
 */
export function ReviewList({
  reviews,
  empty = "No reviews yet.",
  rating,
  owner = false,
  footer,
}: {
  reviews: Review[];
  empty?: string;
  rating?: { avg: number | null; count?: number };
  owner?: boolean;
  footer?: (review: Review) => React.ReactNode;
}) {
  return (
    <div>
      {rating && <RatingSummary avg={rating.avg} count={rating.count} className="mb-3" />}
      {!reviews.length ? (
        <p className="text-sm text-muted-foreground">{empty}</p>
      ) : (
        <ul className="divide-y">
          {reviews.map((r) => (
            <li key={r.id} id={`review-${r.id}`} className="scroll-mt-24 py-4 first:pt-0">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Stars value={r.rating} />
                  <span className="text-sm font-medium">{r.customer_name}</span>
                </div>
                <span className="text-xs text-muted-foreground" suppressHydrationWarning>{timeAgo(r.created_at)}</span>
              </div>
              {owner && r.status && r.status !== "visible" && (
                <Badge variant="outline" className="mt-1.5">{STATUS_LABEL[r.status] ?? r.status}</Badge>
              )}
              {r.comment && <p className="mt-1.5 text-sm">{r.comment}</p>}
              <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1"><BadgeCheck className="size-3.5 text-success" /> Verified {r.source}</span>
                {r.product_name && <span className="truncate">{r.product_name}</span>}
                {r.accuracy_rating && <span>Item accuracy {r.accuracy_rating}/5</span>}
                {r.delivery_rating && <span>Delivery {r.delivery_rating}/5</span>}
                {!owner && <ReportButton reviewId={r.id} target="review" />}
              </p>
              {!owner && r.reply && (
                <div className="mt-3 rounded-xl border-l-2 border-brand/50 bg-muted/50 px-3 py-2">
                  <p className="flex items-center gap-1.5 text-xs font-medium">
                    <MessageSquareReply className="size-3.5" aria-hidden /> Reply from the shop
                    <span className="font-normal text-muted-foreground" suppressHydrationWarning>· {timeAgo(r.reply.created_at)}{r.reply.edited_at ? " · edited" : ""}</span>
                  </p>
                  <p className="mt-1 text-sm">{r.reply.text}</p>
                  <p className="mt-1"><ReportButton reviewId={r.id} target="reply" /></p>
                </div>
              )}
              {footer?.(r)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
