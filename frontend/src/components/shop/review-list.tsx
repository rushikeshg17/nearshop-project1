"use client";

import { BadgeCheck, Flag, MessageSquareReply, Sparkles, Star } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { useI18n } from "@/components/providers";
import { Badge } from "@/components/ui/badge";
import { useNow } from "@/hooks/use-debounce";
import { useMe } from "@/hooks/use-session";
import { timeAgo } from "@/lib/format";
import type { Review } from "@/lib/types";
import { cn } from "@/lib/utils";

export function Stars({ value, className }: { value: number; className?: string }) {
  const { t } = useI18n();
  return (
    <span role="img" className={cn("inline-flex", className)} aria-label={t("common.reviews.stars", { value })}>
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
  const { t } = useI18n();
  if (avg === null || avg === undefined) {
    return (
      <span className={cn("inline-flex items-center gap-1.5 text-sm", className)} title={t("common.reviews.newShopHint")}>
        <Badge variant="secondary" className="gap-1"><Sparkles className="size-3" aria-hidden /> {t("common.rating.newShop")}</Badge>
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

// The report dialog loads on the first tap of a report action (P2.9: the dialog, the drawer it uses on a phone, the
// radio group and the toast library are not first-load JavaScript on the product and shop pages).
const ReportDialog = dynamic(() => import("@/components/shop/review-report-dialog").then((m) => m.ReportDialog), { ssr: false });

/** `shopId` is the shop the list belongs to: its owner sees no report action on the shop's own replies. */
function ReportButton({ reviewId, target, shopId }: { reviewId: number; target: "review" | "reply"; shopId?: number }) {
  const { t } = useI18n();
  const { data: me } = useMe();
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [reported, setReported] = useState(false);
  const [own, setOwn] = useState(false);
  const label = target === "reply" ? t("common.reviews.reportReply") : t("common.reviews.report");
  // The backend refuses reports on your own content (P3.6): the shop's owner on its replies, a customer on their
  // own review. The owner is known up front; a customer's own review only once the API says so (public lists do
  // not say who wrote a review), and then the action goes away instead of staying on screen.
  if (own || (target === "reply" && shopId !== undefined && me?.shop?.id === shopId)) return null;
  if (reported) return <span className="text-xs text-muted-foreground">{t("common.reviews.reported")}</span>;
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
        aria-label={target === "reply" ? t("common.reviews.reportReplyLabel") : t("common.reviews.reportReviewLabel")}
      >
        <Flag className="size-3" aria-hidden /> {label}
      </button>
      {open && (
        <ReportDialog reviewId={reviewId} target={target} open onOpenChange={setOpen} onReported={() => setReported(true)}
          onOwn={() => setOwn(true)} />
      )}
    </>
  );
}

/** The moderation statuses with a label (common.reviews.status in src/messages); another status shows as it is. */
const STATUS_LABELLED = new Set(["hidden", "removed"]);

/**
 * Reviews with the shop's public reply under each one, and a report action (P3.6). With `rating`, a heading line
 * shows the shop's rating, or "New shop" when it has none yet. `owner` is the shop owner's own list: it shows each
 * review's moderation status and leaves the reply to `footer` (the reply editor), and the owner cannot report it.
 * `shopId` is the shop the reviews belong to: its signed-in owner sees no report action on the shop's replies.
 */
export function ReviewList({
  reviews,
  empty,
  rating,
  owner = false,
  shopId,
  footer,
}: {
  reviews: Review[];
  empty?: string;
  rating?: { avg: number | null; count?: number };
  owner?: boolean;
  shopId?: number;
  footer?: (review: Review) => React.ReactNode;
}) {
  const { t, language } = useI18n();
  const now = useNow(60_000);
  return (
    <div>
      {rating && <RatingSummary avg={rating.avg} count={rating.count} className="mb-3" />}
      {!reviews.length ? (
        <p className="text-sm text-muted-foreground">{empty ?? t("common.reviews.empty")}</p>
      ) : (
        <ul className="divide-y">
          {reviews.map((r) => (
            <li key={r.id} id={`review-${r.id}`} className="scroll-mt-24 py-4 first:pt-0">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Stars value={r.rating} />
                  <span className="text-sm font-medium">{r.customer_name}</span>
                </div>
                <span className="text-xs text-muted-foreground" suppressHydrationWarning>{timeAgo(r.created_at, now, language)}</span>
              </div>
              {owner && r.status && r.status !== "visible" && (
                <Badge variant="outline" className="mt-1.5">{STATUS_LABELLED.has(r.status) ? t(`common.reviews.status.${r.status as "hidden" | "removed"}`) : r.status}</Badge>
              )}
              {r.comment && <p className="mt-1.5 text-sm">{r.comment}</p>}
              <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1"><BadgeCheck className="size-3.5 text-success" /> {t(`common.reviews.verified.${r.source}`)}</span>
                {r.product_name && <span className="truncate">{r.product_name}</span>}
                {r.accuracy_rating && <span>{t("common.reviews.itemAccuracy", { value: r.accuracy_rating })}</span>}
                {r.delivery_rating && <span>{t("common.reviews.delivery", { value: r.delivery_rating })}</span>}
                {!owner && <ReportButton reviewId={r.id} target="review" shopId={shopId} />}
              </p>
              {!owner && r.reply && (
                <div className="mt-3 rounded-xl border-l-2 border-brand/50 bg-muted/50 px-3 py-2">
                  <p className="flex items-center gap-1.5 text-xs font-medium">
                    <MessageSquareReply className="size-3.5" aria-hidden /> {t("common.reviews.replyFromShop")}
                    <span className="font-normal text-muted-foreground" suppressHydrationWarning>· {timeAgo(r.reply.created_at, now, language)}{r.reply.edited_at ? ` · ${t("common.reviews.edited")}` : ""}</span>
                  </p>
                  <p className="mt-1 text-sm">{r.reply.text}</p>
                  <p className="mt-1"><ReportButton reviewId={r.id} target="reply" shopId={shopId} /></p>
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
