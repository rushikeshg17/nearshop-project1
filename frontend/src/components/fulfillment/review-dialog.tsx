"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, Loader2, Star } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { ResponsiveDialog } from "@/components/common/responsive-dialog";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError, errorMessage } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { plural } from "@/lib/i18n";
import type { Order, Reservation } from "@/lib/types";
import { cn } from "@/lib/utils";

function StarInput({ value, onChange, label }: { value: number; onChange: (v: number) => void; label: string }) {
  const { t, language } = useI18n();
  const [hover, setHover] = useState(0);
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-sm">{label}</span>
      {/* The hover preview is for the mouse only: the stars are buttons, reached and pressed with the keyboard. */}
      <div className="flex" onMouseLeave={() => setHover(0)}>
        <div className="flex" role="radiogroup" aria-label={label}>
          {[1, 2, 3, 4, 5].map((i) => (
            <button
              key={i}
              type="button"
              role="radio"
              aria-checked={value === i}
              aria-label={t(`transactions.review.stars.${plural(language, i)}`, { count: formatNumber(i) })}
              onMouseEnter={() => setHover(i)}
              onClick={() => onChange(i)}
              className="p-1 transition-transform active:scale-90"
            >
              <Star className={cn("size-6 transition-colors", i <= (hover || value) ? "fill-warning text-warning" : "text-muted-foreground/40")} />
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

/**
 * The review rules (plan section 8.2, backend/app/services/reviews.py), keyed by the error code the API returns
 * when one of them refuses a review (their text: transactions.review.rules in src/messages). The dialog lists them
 * all and marks the one that applied.
 */
const RULES: { code: "review_window_closed" | "account_too_new" | "review_not_allowed" | "phone_not_verified" | "duplicate_review"; whenRefused?: boolean }[] = [
  { code: "review_window_closed" },
  { code: "account_too_new" },
  { code: "review_not_allowed" },
  // Enforced only once phone sign-in arrives (P4.1), so listed only when it is the reason.
  { code: "phone_not_verified", whenRefused: true },
  { code: "duplicate_review" },
];

function RefusedReview({ error }: { error: unknown }) {
  const { t } = useI18n();
  const code = error instanceof ApiError ? error.code : "";
  const known = RULES.some((r) => r.code === code);
  return (
    <div role="alert" className="rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-sm">
      <p className="flex items-start gap-2 font-medium">
        <CircleAlert className="mt-0.5 size-4 shrink-0 text-destructive" aria-hidden /> {errorMessage(error)}
      </p>
      {known && (
        <>
          <p className="mt-2 text-muted-foreground">{t("transactions.review.rulesIntro")}</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-muted-foreground">
            {RULES.filter((r) => !r.whenRefused || r.code === code).map((r) => (
              <li key={r.code} className={cn(r.code === code && "font-medium text-foreground")}>
                {t(`transactions.review.rules.${r.code}`)}
                {r.code === code && <span className="sr-only"> {t("transactions.review.whyRefused")}</span>}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-muted-foreground">
            {t("transactions.review.countsNote")}
          </p>
        </>
      )}
    </div>
  );
}

export function ReviewDialog({ item, open, onOpenChange }: { item: Reservation | Order; open: boolean; onOpenChange: (o: boolean) => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [rating, setRating] = useState(0);
  const [accuracy, setAccuracy] = useState(0);
  const [delivery, setDelivery] = useState(0);
  const [comment, setComment] = useState("");
  const isOrder = item.kind === "order";

  const submit = useMutation({
    mutationFn: () =>
      api.post("/reviews", {
        reservation_id: isOrder ? null : item.id,
        order_id: isOrder ? item.id : null,
        rating,
        accuracy_rating: accuracy || null,
        delivery_rating: isOrder && delivery ? delivery : null,
        comment: comment.trim() || null,
      }),
    // A refused review is explained inside the dialog, not in a toast.
    meta: { silent: true },
    onSuccess: () => {
      toast.success(t("transactions.review.thanks"));
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: [item.kind === "order" ? "order" : "reservation", item.id] });
      onOpenChange(false);
    },
  });

  return (
    <ResponsiveDialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("transactions.review.title", { shop: item.shop.name })}
      description={t("transactions.review.description")}
      footer={
        <Button className="w-full" size="lg" disabled={!rating || submit.isPending} onClick={() => submit.mutate()}>
          {submit.isPending && <Loader2 className="animate-spin" />} {t("transactions.review.post")}
        </Button>
      }
    >
      <div className="space-y-3 pb-2">
        {submit.isError && <RefusedReview error={submit.error} />}
        <StarInput label={t("transactions.review.overall")} value={rating} onChange={setRating} />
        <StarInput label={t("transactions.review.accuracy")} value={accuracy} onChange={setAccuracy} />
        {isOrder && <StarInput label={t("transactions.review.delivery")} value={delivery} onChange={setDelivery} />}
        <div className="space-y-1.5 pt-2">
          <Label htmlFor="rv">{t("transactions.review.commentLabel")}</Label>
          <Textarea id="rv" rows={3} value={comment} onChange={(e) => setComment(e.target.value.slice(0, 1000))} placeholder={t("transactions.review.commentPlaceholder")} />
        </div>
      </div>
    </ResponsiveDialog>
  );
}
