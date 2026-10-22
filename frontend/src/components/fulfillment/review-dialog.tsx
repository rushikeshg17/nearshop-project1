"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, Loader2, Star } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { ResponsiveDialog } from "@/components/common/responsive-dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError, errorMessage } from "@/lib/api";
import type { Order, Reservation } from "@/lib/types";
import { cn } from "@/lib/utils";

function StarInput({ value, onChange, label }: { value: number; onChange: (v: number) => void; label: string }) {
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
              aria-label={`${i} star${i > 1 ? "s" : ""}`}
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
 * when one of them refuses a review. The dialog lists them all and marks the one that applied.
 */
const RULES: { code: string; text: string; whenRefused?: boolean }[] = [
  { code: "review_window_closed", text: "Reviews can be written within 14 days of the pickup or delivery." },
  { code: "account_too_new", text: "Your account must have been created at least 3 days before the purchase." },
  { code: "review_not_allowed", text: "A shop cannot be reviewed from the shop's or its owner's phone number." },
  // Enforced only once phone sign-in arrives (P4.1), so listed only when it is the reason.
  { code: "phone_not_verified", text: "Your phone number must be verified.", whenRefused: true },
  { code: "duplicate_review", text: "Each pickup or delivery can be reviewed once." },
];

function RefusedReview({ error }: { error: unknown }) {
  const code = error instanceof ApiError ? error.code : "";
  const known = RULES.some((r) => r.code === code);
  return (
    <div role="alert" className="rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-sm">
      <p className="flex items-start gap-2 font-medium">
        <CircleAlert className="mt-0.5 size-4 shrink-0 text-destructive" aria-hidden /> {errorMessage(error)}
      </p>
      {known && (
        <>
          <p className="mt-2 text-muted-foreground">To keep ratings honest, NearShop accepts a review only when:</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-muted-foreground">
            {RULES.filter((r) => !r.whenRefused || r.code === code).map((r) => (
              <li key={r.code} className={cn(r.code === code && "font-medium text-foreground")}>
                {r.text}
                {r.code === code && <span className="sr-only"> (this is why the review was not accepted)</span>}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-muted-foreground">
            Only some reviews count toward a shop&apos;s rating: one per customer per shop every 30 days.
          </p>
        </>
      )}
    </div>
  );
}

export function ReviewDialog({ item, open, onOpenChange }: { item: Reservation | Order; open: boolean; onOpenChange: (o: boolean) => void }) {
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
      toast.success("Thanks! Your review helps neighbours pick the right shop.");
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: [item.kind === "order" ? "order" : "reservation", item.id] });
      onOpenChange(false);
    },
  });

  return (
    <ResponsiveDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`How was ${item.shop.name}?`}
      description="Reviews are only accepted for completed purchases, one per visit."
      footer={
        <Button className="w-full" size="lg" disabled={!rating || submit.isPending} onClick={() => submit.mutate()}>
          {submit.isPending && <Loader2 className="animate-spin" />} Post review
        </Button>
      }
    >
      <div className="space-y-3 pb-2">
        {submit.isError && <RefusedReview error={submit.error} />}
        <StarInput label="Overall experience" value={rating} onChange={setRating} />
        <StarInput label="Item matched the listing" value={accuracy} onChange={setAccuracy} />
        {isOrder && <StarInput label="Delivery" value={delivery} onChange={setDelivery} />}
        <div className="space-y-1.5 pt-2">
          <Label htmlFor="rv">Anything others should know? (optional)</Label>
          <Textarea id="rv" rows={3} value={comment} onChange={(e) => setComment(e.target.value.slice(0, 1000))} placeholder="Was it ready on time? Was the stock count right?" />
        </div>
      </div>
    </ResponsiveDialog>
  );
}
