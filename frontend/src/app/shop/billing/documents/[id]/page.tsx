"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Printer, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { use } from "react";

import { ErrorState, ListSkeleton } from "@/components/common/states";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatPrice } from "@/lib/format";
import { richText } from "@/lib/i18n";
import type { BillingDocument, BillingParty } from "@/lib/types";

import { formatDay } from "../../../owner-shell";

/**
 * One receipt, tax invoice, refund receipt or credit note (P9.10; documents from P9.5), drawn from its immutable
 * snapshot exactly as issued. It prints on A4 (the print style below hides the console around it and sets the page
 * size) and reads in full at 360 px. A document in the draft format shows the draft line at the top, large, as the
 * CA has not approved the format yet (B22). The wording, names and addresses are the snapshot's own text.
 */

/** The plan-terms section of the terms of use (P3.1); its text comes from B24. */
const PLAN_TERMS_HREF = "/terms#plan-terms";
const DOC_ID = "billing-document";

const rupees = (paise: number) => formatPrice(paise / 100, true);

// Printing: only the document, on A4, at its full width, with the console's header and navigation hidden.
const PRINT_CSS = `@media print {
  @page { size: A4; margin: 14mm; }
  body * { visibility: hidden !important; }
  #${DOC_ID}, #${DOC_ID} * { visibility: visible !important; }
  #${DOC_ID} { position: absolute; left: 0; top: 0; width: 100%; max-width: none; margin: 0; border: 0; box-shadow: none; padding: 0; }
  html, body { background: #fff !important; }
}`;

function Party({ label, party }: { label: string; party: BillingParty }) {
  const { t } = useI18n();
  return (
    <div className="min-w-0">
      <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase print:text-neutral-600">{label}</p>
      <p className="mt-1 font-semibold break-words">{party.legal_name}</p>
      <p className="break-words whitespace-pre-line">{party.address}</p>
      {party.gstin && <p className="break-all">{t("owner.billing.document.gstin", { gstin: party.gstin })}</p>}
    </div>
  );
}

function Row({ label, value, strong, testId }: { label: string; value: string; strong?: boolean; testId?: string }) {
  return (
    <div className={`flex justify-between gap-4 py-1.5 ${strong ? "border-t font-semibold" : ""}`}>
      <dt>{label}</dt>
      <dd className="text-right tabular-nums" data-testid={testId}>{value}</dd>
    </div>
  );
}

function DocumentView({ doc }: { doc: BillingDocument }) {
  const { t, language } = useI18n();
  const half = doc.gst_rate_pct / 2;
  const meta: [string, string, string?][] = [
    [t("owner.billing.document.number"), doc.number, "document-number"],
    [t("owner.billing.document.date"), formatDay(doc.issued_at, language)],
    [t("owner.billing.document.paymentDate"), formatDay(doc.payment_date, language)],
    [t("owner.billing.document.placeOfSupply"), doc.place_of_supply],
    [t("owner.billing.document.request"), doc.request_number],
    [t("owner.billing.document.upiReference"), doc.upi_reference],
    ...(doc.original_number ? [[t("owner.billing.document.original"), doc.original_number] as [string, string]] : []),
    ...(doc.refund_reference ? [[t("owner.billing.document.refundReference"), doc.refund_reference] as [string, string]] : []),
  ];
  return (
    <article id={DOC_ID} aria-labelledby="document-title" className="mx-auto w-full max-w-[210mm] rounded-3xl border bg-card p-4 text-sm text-card-foreground sm:p-8 print:rounded-none print:bg-white print:text-black">
      {doc.draft_line && (
        <p role="note" className="mb-4 rounded-xl border-2 border-destructive px-3 py-2 text-center text-base font-bold tracking-wide text-destructive uppercase" data-testid="draft-line">
          {doc.draft_line}
        </p>
      )}
      {!doc.intact && (
        <p role="alert" className="mb-4 flex gap-2 rounded-xl bg-destructive/10 px-3 py-2 text-destructive">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden /> {t("owner.billing.document.altered")}
        </p>
      )}
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h1 id="document-title" className="text-xl font-semibold">{t(`owner.billing.docTypes.${doc.type}`)}</h1>
        <p className="font-mono text-base font-semibold">{doc.number}</p>
      </div>
      <div className="mt-6 grid gap-6 sm:grid-cols-2">
        <Party label={t("owner.billing.document.supplier")} party={doc.supplier} />
        <Party label={t("owner.billing.document.recipient")} party={doc.recipient} />
      </div>
      <dl className="mt-6 grid gap-x-6 gap-y-2 sm:grid-cols-2">
        {meta.map(([label, value, testId]) => (
          <div key={label} className="min-w-0">
            <dt className="text-xs text-muted-foreground print:text-neutral-600">{label}</dt>
            <dd className="break-all" data-testid={testId}>{value}</dd>
          </div>
        ))}
      </dl>
      <div className="mt-6 rounded-xl border p-3 sm:p-4">
        <p className="text-xs text-muted-foreground print:text-neutral-600">{t("owner.billing.document.description")}</p>
        <p className="mt-1 break-words">{doc.description}</p>
        {doc.sac && <p className="mt-1 text-xs">{t("owner.billing.document.sac")} {doc.sac}</p>}
        <dl className="mt-3">
          <Row label={t(doc.gst_rate_pct > 0 ? "owner.billing.document.taxable" : "owner.billing.document.amount")} value={rupees(doc.taxable_paise)} />
          {doc.cgst_paise > 0 && <Row label={t("owner.billing.document.cgst", { rate: half })} value={rupees(doc.cgst_paise)} />}
          {doc.sgst_paise > 0 && <Row label={t("owner.billing.document.sgst", { rate: half })} value={rupees(doc.sgst_paise)} />}
          {doc.igst_paise > 0 && <Row label={t("owner.billing.document.igst", { rate: doc.gst_rate_pct })} value={rupees(doc.igst_paise)} />}
          <Row label={t("owner.billing.document.total")} value={rupees(doc.total_paise)} strong testId="document-total" />
        </dl>
      </div>
      {doc.wording && <p className="mt-6 break-words whitespace-pre-line">{doc.wording}</p>}
    </article>
  );
}

export default function BillingDocumentPage({ params }: PageProps<"/shop/billing/documents/[id]">) {
  const { id } = use(params);
  const { t } = useI18n();
  const doc = useQuery({
    queryKey: ["owner", "billing", "document", id],
    queryFn: () => api.get<BillingDocument>(`/owner/billing/documents/${encodeURIComponent(id)}`),
  });
  return (
    <>
      <style>{PRINT_CSS}</style>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2 print:hidden">
        <Button asChild variant="ghost" className="-ml-2">
          <Link href="/shop/billing"><ArrowLeft /> {t("owner.billing.document.back")}</Link>
        </Button>
        {doc.data && <Button variant="outline" onClick={() => window.print()}><Printer /> {t("owner.billing.document.print")}</Button>}
      </div>
      {doc.isLoading ? (
        <ListSkeleton rows={2} />
      ) : doc.error || !doc.data ? (
        <ErrorState error={doc.error ?? new Error(t("owner.billing.document.notFound"))} onRetry={() => doc.refetch()} />
      ) : (
        <DocumentView doc={doc.data} />
      )}
      <p className="mt-4 text-xs text-muted-foreground print:hidden">
        {richText(t("owner.billing.document.terms"), {
          terms: <Link href={PLAN_TERMS_HREF} className="font-medium text-foreground underline underline-offset-4">{t("owner.billing.termsLink")}</Link>,
        })}
      </p>
    </>
  );
}
