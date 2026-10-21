"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BadgeCheck, Check, ExternalLink, Loader2, PhoneCall, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { ReasonDialog, useVerificationQueue } from "@/app/admin/admin-shell";
import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, errorMessage } from "@/lib/api";
import type { KycType, VerificationQueueItem } from "@/lib/types";
import { cn } from "@/lib/utils";

const TYPE_LABEL: Record<KycType, string> = { gstin: "GSTIN", udyam: "Udyam", trade_licence: "Trade licence" };
const KIND_LABEL: Record<VerificationQueueItem["kind"], string> = {
  new: "New",
  renewal: "Renewal",
  review: "Re-check",
};
const TARGET_WORKING_DAYS = 2; // plan section 8.1: a decision within 2 working days

function submittedOn(iso: string | null) {
  if (!iso) return "";
  return new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit", timeZone: "Asia/Kolkata" });
}

export default function AdminVerificationPage() {
  const queue = useVerificationQueue();
  return (
    <>
      <PageTitle
        title="Verification"
        description="Shops waiting for a KYC decision, oldest first. Check the number online, look at the photos, call the shop back, then approve or reject."
      />
      {queue.isError ? (
        <ErrorState error={queue.error} onRetry={() => queue.refetch()} />
      ) : !queue.data ? (
        <ListSkeleton />
      ) : queue.data.length === 0 ? (
        <EmptyState icon={BadgeCheck} title="Nothing to review" description="New submissions and renewals appear here within a minute." />
      ) : (
        <ul className="space-y-4" aria-label="Verification queue">
          {queue.data.map((s) => (
            <QueueRow key={s.id} s={s} />
          ))}
        </ul>
      )}
    </>
  );
}

function QueueRow({ s }: { s: VerificationQueueItem }) {
  const qc = useQueryClient();
  const [rejecting, setRejecting] = useState(false);
  const done = (message: string) => {
    qc.invalidateQueries({ queryKey: ["admin"] });
    toast.success(message);
  };
  const phone = useMutation({
    mutationFn: () => api.post(`/admin/shops/${s.id}/verification/phone-verified`),
    onSuccess: () => done("Phone call-back recorded"),
    onError: (e) => toast.error(errorMessage(e)),
  });
  const approve = useMutation({
    mutationFn: () => api.post<{ verified_until: string | null }>(`/admin/shops/${s.id}/verification/approve`),
    onSuccess: () => done(`${s.name} is verified`),
    onError: (e) => toast.error(errorMessage(e)),
  });
  const reject = useMutation({
    mutationFn: (reason: string) => api.post(`/admin/shops/${s.id}/verification/reject`, { reason }),
    onSuccess: () => {
      setRejecting(false);
      done(s.kind === "renewal" ? "Renewal rejected; the approval in force stands" : `${s.name} was not approved`);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const busy = phone.isPending || approve.isPending || reject.isPending;
  const age = s.age_working_days ?? 0;
  const late = age > TARGET_WORKING_DAYS;

  return (
    <li className="rounded-2xl border bg-card p-4 md:p-5" aria-labelledby={`shop-${s.id}`} data-testid="verification-row" data-shop-id={s.id}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id={`shop-${s.id}`} className="font-semibold">
              <Link href={`/shops/${s.slug}`} className="hover:underline">{s.name}</Link>
            </h2>
            <Badge variant="outline">{KIND_LABEL[s.kind]}</Badge>
          </div>
          <p className="text-sm text-muted-foreground">
            {[s.address_line, s.locality, s.pincode].filter(Boolean).join(", ")}
          </p>
          {s.owner && <p className="text-sm text-muted-foreground">Owner: {s.owner}</p>}
        </div>
        <div className="text-right text-sm">
          <p className={cn("font-medium tabular", late && "text-destructive")} data-testid="age">
            {age} working {age === 1 ? "day" : "days"}
            {late && <span className="sr-only"> (over the {TARGET_WORKING_DAYS}-day target)</span>}
          </p>
          <p className="text-xs text-muted-foreground">Submitted {submittedOn(s.submitted_at)}</p>
        </div>
      </div>

      <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <dt className="text-xs text-muted-foreground">{s.kyc_type ? TYPE_LABEL[s.kyc_type] : "Evidence"}</dt>
          <dd className="font-mono break-all">{s.kyc_number ?? "None"}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Shop phone</dt>
          <dd>
            {s.phone ?? "None"}{" "}
            {s.phone_verified_at ? (
              <span className="text-xs text-success-ink">(call-back done)</span>
            ) : (
              <span className="text-xs text-muted-foreground">(not called back yet)</span>
            )}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Document photo</dt>
          <dd><EvidenceLink href={s.document_photo_url} label={`Document photo: ${s.name}`} /></dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Storefront photo</dt>
          <dd><EvidenceLink href={s.storefront_photo_url} label={`Storefront photo: ${s.name}`} /></dd>
        </div>
      </dl>
      {s.kind === "renewal" && s.verified_until && (
        <p className="mt-3 text-xs text-muted-foreground">
          Current approval ends {submittedOn(s.verified_until)}. The shop stays visible while this renewal is decided.
        </p>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        {!s.phone_verified_at && (
          <Button size="sm" variant="outline" disabled={busy || !s.phone} onClick={() => phone.mutate()}>
            {phone.isPending ? <Loader2 className="animate-spin" /> : <PhoneCall />} Record call-back
          </Button>
        )}
        <Button size="sm" disabled={busy || !s.phone_verified_at} onClick={() => approve.mutate()} aria-label={`Approve ${s.name}`}>
          {approve.isPending ? <Loader2 className="animate-spin" /> : <Check />} Approve
        </Button>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => setRejecting(true)} aria-label={`Reject ${s.name}`}>
          <X /> Reject
        </Button>
      </div>
      {!s.phone_verified_at && (
        <p className="mt-2 text-xs text-muted-foreground">Approval needs a call-back to the shop phone first.</p>
      )}

      <ReasonDialog
        open={rejecting}
        onOpenChange={setRejecting}
        title={`Reject ${s.name}?`}
        description={
          s.kind === "renewal"
            ? "The approval in force stands until it ends; the owner can submit fresh evidence before then."
            : "The shop stays hidden from customers. The owner sees your reason and can submit again."
        }
        confirmLabel="Reject"
        pending={reject.isPending}
        onConfirm={(reason) => reject.mutate(reason)}
      />
    </li>
  );
}

/** A signed evidence link (valid for 5 minutes; the queue reloads every 30 seconds, so it stays fresh). */
function EvidenceLink({ href, label }: { href: string | null; label: string }) {
  if (!href) return <span className="text-muted-foreground">None</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary underline-offset-3 hover:underline" aria-label={`${label} (opens in a new tab)`}>
      View <ExternalLink className="size-3.5" />
    </a>
  );
}
