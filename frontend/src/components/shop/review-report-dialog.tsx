"use client";

import { useMutation } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";

import { ResponsiveDialog } from "@/components/common/responsive-dialog";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError } from "@/lib/api";
import type { ReviewReportReason } from "@/lib/types";

// Loaded by review-list.tsx on the first tap of a report action, so none of this is first-load JavaScript (P2.9).

/** The report reasons, in the order shown; their labels and hints are common.reviews.reasons in src/messages. */
const REASONS: readonly ReviewReportReason[] = ["fake", "abusive", "personal_info", "off_topic", "other"];

/** Report a review, or the shop's reply to it (P3.6): one report per person, up to 10 a day. */
export function ReportDialog({
  reviewId,
  target,
  open,
  onOpenChange,
  onReported,
  onOwn,
}: {
  reviewId: number;
  target: "review" | "reply";
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onReported: () => void;
  /** The API refused because the viewer wrote it (409 own_review / own_reply): the button goes away. */
  onOwn: () => void;
}) {
  const { t } = useI18n();
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
      toast.success(d.already_reported ? t("common.reviews.alreadyReported") : t("common.reviews.thanks"));
      onReported();
      onOpenChange(false);
    },
    onError: (e) => {
      if (e instanceof ApiError && (e.code === "own_review" || e.code === "own_reply")) {
        onOwn();
        onOpenChange(false);
      }
    },
  });
  return (
    <ResponsiveDialog
      open={open}
      onOpenChange={(o) => !send.isPending && onOpenChange(o)}
      title={target === "reply" ? t("common.reviews.reportReplyLabel") : t("common.reviews.reportReviewLabel")}
      description={t("common.reviews.reportDescription")}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={send.isPending}>{t("common.reviews.cancel")}</Button>
          <Button type="submit" form={`${id}-form`} disabled={!reason || (reason === "other" && !note.trim()) || send.isPending}>
            {send.isPending && <Loader2 className="animate-spin" />} {t("common.reviews.sendReport")}
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
          <legend className="mb-2 text-sm font-medium">{t("common.reviews.whatIsWrong")}</legend>
          <RadioGroup value={reason} onValueChange={(v) => setReason(v as ReviewReportReason)} className="gap-2">
            {REASONS.map((r) => (
              <div key={r} className="flex items-start gap-2.5">
                <RadioGroupItem value={r} id={`${id}-${r}`} className="mt-0.5" />
                <Label htmlFor={`${id}-${r}`} className="flex-col items-start gap-0 font-normal">
                  <span className="font-medium">{t(`common.reviews.reasons.${r}.label`)}</span>
                  <span className="text-xs text-muted-foreground">{t(`common.reviews.reasons.${r}.hint`)}</span>
                </Label>
              </div>
            ))}
          </RadioGroup>
        </fieldset>
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-note`}>{reason === "other" ? t("common.reviews.whatProblem") : t("common.reviews.anythingElse")}</Label>
          <Textarea id={`${id}-note`} rows={2} value={note} onChange={(e) => setNote(e.target.value.slice(0, 500))} />
        </div>
      </form>
    </ResponsiveDialog>
  );
}
