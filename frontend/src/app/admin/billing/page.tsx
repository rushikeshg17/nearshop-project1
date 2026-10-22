"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Undo2, Upload } from "lucide-react";
import { useId, useRef, useState } from "react";
import { toast } from "sonner";

import { ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { formatPrice } from "@/lib/format";
import type {
  AdminSubscription,
  BankCredit,
  BillingPayment,
  FoundingCounter,
  PaymentRequest,
  StatementImport,
  SubscriptionStatus,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Billing for admins (P9.10, in English; backend P9.4 to P9.6, docs/runbooks/billing.md): the founding counter,
 * advance subscriptions with the days since payment (red from 300: an advance must be used within 365 days), the
 * bank-statement upload with the unmatched queue and matching by hand, payment requests, payments and refunds, and
 * subscriptions by status. Every change asks for a reason, which the backend keeps with the change.
 */

const MIN_REASON = 10; // backend/app/schemas/requests.py, BILLING_REASON
const MIN_BANK_REFERENCE = 4; // BANK_REFERENCE
const ADVANCE_ALERT_DAYS = 300; // NEARSHOP_ADVANCE_ALERT_DAYS (plans.alert_unused_advances)
const DAY = 86_400_000;
const STATUSES: SubscriptionStatus[] = ["active", "advance", "lapsed", "cancelled", "refunded"];

const rupees = (paise: number) => formatPrice(paise / 100, true);

function formatDate(iso: string | null | undefined) {
  return iso ? new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" }) : "-";
}

function Section({ title, description, children, id }: { title: string; description?: string; children: React.ReactNode; id: string }) {
  return (
    <section aria-labelledby={id} className="rounded-3xl border bg-card p-5">
      <h2 id={id} className="font-semibold">{title}</h2>
      {description && <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

function Reason({ id, value, onChange, hint = "At least 10 characters. Kept with the change." }: { id: string; value: string; onChange: (v: string) => void; hint?: string }) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>Reason</Label>
      <Textarea id={id} rows={2} value={value} onChange={(e) => onChange(e.target.value.slice(0, 500))} aria-describedby={`${id}-hint`} />
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">{hint}</p>
    </div>
  );
}

// ------------------------------------------------------------------ founding counter and advances

function FoundingSection() {
  const counter = useQuery({ queryKey: ["admin", "billing", "founding"], queryFn: () => api.get<FoundingCounter>("/admin/founding") });
  const c = counter.data;
  return (
    <Section id="founding-title" title="Founding offer" description="Growth at the founding price for the first year, for the first shops in the founding cities, counted in order of first payment.">
      {counter.isError ? <ErrorState error={counter.error} onRetry={() => counter.refetch()} /> : !c ? <ListSkeleton rows={1} /> : (
        <div data-testid="founding-counter">
          <dl className="grid grid-cols-3 gap-3 text-center">
            <div className="rounded-2xl border p-3"><dt className="text-xs text-muted-foreground">Taken</dt><dd className="text-2xl font-semibold tabular-nums">{c.taken}</dd></div>
            <div className="rounded-2xl border p-3"><dt className="text-xs text-muted-foreground">Remaining</dt><dd className="text-2xl font-semibold tabular-nums">{c.remaining}</dd></div>
            <div className="rounded-2xl border p-3"><dt className="text-xs text-muted-foreground">Cap</dt><dd className="text-2xl font-semibold tabular-nums">{c.cap}</dd></div>
          </dl>
          <p className="mt-2 text-xs text-muted-foreground">Cities: {c.cities.join(", ") || "-"}</p>
        </div>
      )}
    </Section>
  );
}

function AdvancesSection() {
  const advances = useQuery({
    queryKey: ["admin", "billing", "subscriptions", "advance"],
    queryFn: () => api.get<AdminSubscription[]>("/admin/subscriptions", { status: "advance", limit: 500 }),
  });
  const [now] = useState(() => Date.now());
  const rows = advances.data;
  return (
    <Section id="advances-title" title="Advance subscriptions" description={`Paid before billing starts. Each must become active within 365 days of payment; from ${ADVANCE_ALERT_DAYS} days the count is red.`}>
      {advances.isError ? <ErrorState error={advances.error} onRetry={() => advances.refetch()} /> : !rows ? <ListSkeleton rows={2} /> : !rows.length ? (
        <p className="text-sm text-muted-foreground">No advance subscriptions.</p>
      ) : (
        <ul className="divide-y text-sm" data-testid="advances">
          {rows.map((s) => {
            const days = s.paid_at ? Math.floor((now - new Date(s.paid_at).getTime()) / DAY) : null;
            const late = days !== null && days >= ADVANCE_ALERT_DAYS;
            return (
              <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 py-2" data-late={late}>
                <span className="min-w-0">
                  <span className="font-medium">{s.shops.map((x) => x.name ?? `Shop ${x.id}`).join(", ")}</span>
                  <span className="block text-xs text-muted-foreground">Subscription {s.id} · {rupees(s.price_paise)}{s.founding ? " · founding" : ""} · paid {formatDate(s.paid_at)}</span>
                </span>
                <span className={cn("tabular-nums", late ? "font-semibold text-destructive" : "text-muted-foreground")}>
                  {days === null ? "-" : `${days} days since payment`}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------ bank credits: statement upload, unmatched queue, matching by hand

function StatementUpload() {
  const qc = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const upload = useMutation({
    mutationFn: (f: File) => api.upload<StatementImport>("/admin/billing/statements", f),
    onSuccess: (r) => {
      toast.success(`Statement read: ${r.matched} matched, ${r.unmatched} unmatched, ${r.duplicates} duplicates.`);
      setFile(null);
      if (input.current) input.current.value = "";
      qc.invalidateQueries({ queryKey: ["admin", "billing"] });
    },
  });
  const result = upload.data;
  return (
    <div className="space-y-3">
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (file) upload.mutate(file);
        }}
      >
        <div className="min-w-0 flex-1 space-y-1.5">
          <Label htmlFor="statement-file">Bank statement export (CSV, the layout in docs/runbooks/billing.md)</Label>
          <Input id="statement-file" ref={input} type="file" accept=".csv,text/csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </div>
        <Button type="submit" disabled={!file || upload.isPending}>
          {upload.isPending ? <Loader2 className="animate-spin" /> : <Upload />} Upload and match
        </Button>
      </form>
      {result && (
        <div className="rounded-2xl border p-3 text-sm" data-testid="statement-result">
          <p className="font-medium">
            Statement {result.statement_id}: {result.credits} credits, {result.matched} matched, {result.unmatched} unmatched, {result.duplicates} duplicates
            {result.debits_skipped ? `, ${result.debits_skipped} debits skipped` : ""}{result.unreadable ? `, ${result.unreadable} rows unreadable` : ""}.
          </p>
          <ul className="mt-2 space-y-1 text-xs">
            {result.rows.filter((r) => r.result !== "matched").map((r, i) => (
              <li key={`${r.upi_reference}-${i}`}>
                <Badge variant={r.result === "duplicate" ? "secondary" : "destructive"}>{r.result}</Badge> {r.upi_reference} · {rupees(r.amount_paise)} · {r.credited_on}
                {r.reason ? ` · ${r.reason}` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

type MatchDraft = { upi_reference: string; request_number: string };

function MatchByHand({ draft, onDone }: { draft: MatchDraft; onDone: () => void }) {
  const id = useId();
  const qc = useQueryClient();
  const [upi, setUpi] = useState(draft.upi_reference);
  const [number, setNumber] = useState(draft.request_number);
  const [reason, setReason] = useState("");
  const [amount, setAmount] = useState("");
  const [creditedOn, setCreditedOn] = useState("");
  const offStatement = amount.trim() !== "" || creditedOn !== "";
  const amountPaise = Math.round(Number(amount) * 100);
  const valid = upi.trim().length >= 6 && number.trim().length >= 9 && reason.trim().length >= MIN_REASON
    && (!offStatement || (amountPaise > 0 && Number.isFinite(amountPaise) && creditedOn !== ""));
  const match = useMutation({
    mutationFn: () => api.post<BillingPayment>("/admin/billing/credits/match", {
      upi_reference: upi.trim(),
      request_number: number.trim(),
      reason: reason.trim(),
      ...(offStatement ? { amount_paise: amountPaise, credited_on: creditedOn } : {}),
    }),
    onSuccess: (p) => {
      toast.success(`Matched ${p.upi_reference} to ${p.request_number}.`);
      qc.invalidateQueries({ queryKey: ["admin", "billing"] });
      onDone();
    },
  });
  return (
    <form
      aria-label="Match a credit by hand"
      className="grid gap-3 rounded-2xl border p-4"
      data-testid="match-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) match.mutate();
      }}
    >
      <h3 className="text-sm font-semibold">Match a credit by hand</h3>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-upi`}>UPI reference</Label>
          <Input id={`${id}-upi`} value={upi} onChange={(e) => setUpi(e.target.value.slice(0, 64))} autoComplete="off" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-number`}>Payment request number</Label>
          <Input id={`${id}-number`} value={number} onChange={(e) => setNumber(e.target.value.slice(0, 16))} placeholder="NSP000123" autoComplete="off" />
        </div>
      </div>
      <Reason id={`${id}-reason`} value={reason} onChange={setReason} />
      <details className="text-sm">
        <summary className="cursor-pointer text-muted-foreground">A credit not on any uploaded statement yet</summary>
        <div className="mt-2 grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor={`${id}-amount`}>Amount received (rupees)</Label>
            <Input id={`${id}-amount`} inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^0-9.]/g, ""))} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`${id}-date`}>Credited on</Label>
            <Input id={`${id}-date`} type="date" value={creditedOn} onChange={(e) => setCreditedOn(e.target.value)} />
          </div>
        </div>
      </details>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={!valid || match.isPending}>{match.isPending && <Loader2 className="animate-spin" />} Match</Button>
        <Button type="button" variant="ghost" onClick={onDone} disabled={match.isPending}>Clear</Button>
      </div>
    </form>
  );
}

/** Asks for a bank or gateway reference and a reason: returning a credit, closing a mismatch, refunding a payment. */
function ReferenceDialog({
  open,
  onOpenChange,
  title,
  description,
  referenceLabel,
  confirmLabel,
  pending,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  referenceLabel: string;
  confirmLabel: string;
  pending: boolean;
  onConfirm: (body: { bank_reference: string; reason: string }) => void;
}) {
  const id = useId();
  const [reference, setReference] = useState("");
  const [reason, setReason] = useState("");
  const valid = reference.trim().length >= MIN_BANK_REFERENCE && reason.trim().length >= MIN_REASON;
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (pending) return;
        if (!o) {
          setReference("");
          setReason("");
        }
        onOpenChange(o);
      }}
    >
      <DialogContent>
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (valid) onConfirm({ bank_reference: reference.trim(), reason: reason.trim() });
          }}
        >
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>{description}</DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor={`${id}-ref`}>{referenceLabel}</Label>
            <Input id={`${id}-ref`} value={reference} onChange={(e) => setReference(e.target.value.slice(0, 64))} autoComplete="off" />
          </div>
          <Reason id={`${id}-reason`} value={reason} onChange={setReason} />
          <DialogFooter>
            <DialogClose asChild><Button type="button" variant="outline" disabled={pending}>Cancel</Button></DialogClose>
            <Button type="submit" variant="destructive" disabled={!valid || pending}>{pending && <Loader2 className="animate-spin" />} {confirmLabel}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function CreditItem({ c, onMatch }: { c: BankCredit; onMatch: (d: MatchDraft) => void }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const mismatch = c.source === "reconciliation";
  const act = useMutation({
    mutationFn: (body: { bank_reference: string; reason: string }) =>
      api.post<BankCredit>(`/admin/billing/credits/${c.id}/${mismatch ? "close" : "return"}`, body),
    onSuccess: () => {
      toast.success(mismatch ? `Mismatch ${c.upi_reference} closed.` : `Credit ${c.upi_reference} returned.`);
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["admin", "billing"] });
    },
  });
  return (
    <li className="space-y-2 py-3 text-sm" aria-label={`Credit ${c.upi_reference}`} data-testid="unmatched-credit">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono font-semibold break-all">{c.upi_reference}</span>
        <span className="tabular-nums">{rupees(c.amount_paise)}</span>
        <span className="text-muted-foreground">{c.credited_on}</span>
        <Badge variant="secondary">{c.source}</Badge>
        {c.alerted_at && <Badge variant="destructive">over 3 working days</Badge>}
      </div>
      {c.narration && <p className="break-words text-xs text-muted-foreground">{c.narration}</p>}
      {c.note && <p className="text-xs">Why unmatched: {c.note}</p>}
      <div className="flex flex-wrap gap-2">
        {!mismatch && (
          <Button size="sm" onClick={() => onMatch({ upi_reference: c.upi_reference, request_number: c.request_number ?? "" })}>Match by hand</Button>
        )}
        <Button size="sm" variant="outline" onClick={() => setOpen(true)}><Undo2 /> {mismatch ? "Close" : "Return to payer"}</Button>
      </div>
      <ReferenceDialog
        open={open}
        onOpenChange={setOpen}
        title={mismatch ? `Close mismatch ${c.upi_reference}` : `Return ${rupees(c.amount_paise)} to the payer`}
        description={mismatch ? "The gateway mismatch is checked; give the gateway's reference (settlement id, ticket or refund id)." : "Record the bank transfer that sent this credit back."}
        referenceLabel={mismatch ? "Gateway reference" : "Bank transfer reference"}
        confirmLabel={mismatch ? "Close" : "Record return"}
        pending={act.isPending}
        onConfirm={(body) => act.mutate(body)}
      />
    </li>
  );
}

function CreditsSection() {
  const credits = useQuery({
    queryKey: ["admin", "billing", "credits", "unmatched"],
    queryFn: () => api.get<BankCredit[]>("/admin/billing/credits", { status: "unmatched" }),
  });
  const [draft, setDraft] = useState<MatchDraft & { key: number }>({ upi_reference: "", request_number: "", key: 0 });
  const rows = credits.data;
  return (
    <Section id="credits-title" title="Bank credits" description="Upload the bank's statement: each credit is matched by its request number and amount. The rest wait here, oldest first, until they are matched by hand or returned.">
      <StatementUpload />
      <h3 className="mt-5 text-sm font-semibold">Unmatched queue</h3>
      {credits.isError ? <ErrorState error={credits.error} onRetry={() => credits.refetch()} /> : !rows ? <ListSkeleton rows={2} /> : !rows.length ? (
        <p className="mt-1 text-sm text-muted-foreground" data-testid="unmatched-empty">No unmatched credits.</p>
      ) : (
        <ul className="divide-y" data-testid="unmatched-queue">
          {rows.map((c) => <CreditItem key={c.id} c={c} onMatch={(d) => setDraft((x) => ({ ...d, key: x.key + 1 }))} />)}
        </ul>
      )}
      <div className="mt-4">
        <MatchByHand key={draft.key} draft={draft} onDone={() => setDraft((x) => ({ upi_reference: "", request_number: "", key: x.key + 1 }))} />
      </div>
    </Section>
  );
}

// ------------------------------------------------------------------ payment requests and payments

type RequestState = "open" | "paid" | "cancelled" | "refunded";

function RequestsSection() {
  const qc = useQueryClient();
  const id = useId();
  const [state, setState] = useState<RequestState>("open");
  const requests = useQuery({
    queryKey: ["admin", "billing", "requests", state],
    queryFn: () => api.get<PaymentRequest[]>("/admin/billing/requests", { status: state }),
  });
  const [shopId, setShopId] = useState("");
  const [reason, setReason] = useState("");
  const create = useMutation({
    mutationFn: () => api.post<PaymentRequest>("/admin/billing/requests", { shop_id: Number(shopId), plan_code: "growth", period: "year", reason: reason.trim() }),
    onSuccess: (r) => {
      toast.success(`Request ${r.number} made for shop ${r.shop_id}: ${rupees(r.total_paise)}.`);
      setShopId("");
      setReason("");
      qc.invalidateQueries({ queryKey: ["admin", "billing", "requests"] });
    },
  });
  const rows = requests.data;
  return (
    <Section id="requests-title" title="Payment requests" description="A request carries the amount and its number, which the owner puts in the UPI payment note.">
      <Tabs value={state} onValueChange={(v) => setState(v as RequestState)} className="mb-3">
        <TabsList>
          <TabsTrigger value="open">Open</TabsTrigger>
          <TabsTrigger value="paid">Paid</TabsTrigger>
          <TabsTrigger value="cancelled">Cancelled</TabsTrigger>
          <TabsTrigger value="refunded">Refunded</TabsTrigger>
        </TabsList>
      </Tabs>
      {requests.isError ? <ErrorState error={requests.error} onRetry={() => requests.refetch()} /> : !rows ? <ListSkeleton rows={2} /> : !rows.length ? (
        <p className="text-sm text-muted-foreground">No {state} requests.</p>
      ) : (
        <ul className="divide-y text-sm">
          {rows.map((r) => (
            <li key={r.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
              <span className="min-w-0">
                <span className="font-mono font-semibold">{r.number}</span> · shop {r.shop_id} · {r.plan_code} {r.period} · {r.kind}{r.founding ? " · founding" : ""}
                <span className="block text-xs text-muted-foreground">Made {formatDate(r.created_at)} by {r.created_by}{r.reason ? `: ${r.reason}` : ""}</span>
              </span>
              <span className="tabular-nums font-medium">{rupees(r.total_paise)}</span>
            </li>
          ))}
        </ul>
      )}
      <form
        className="mt-4 grid gap-3 rounded-2xl border p-4"
        aria-label="Make a payment request for a shop"
        onSubmit={(e) => {
          e.preventDefault();
          if (Number(shopId) > 0 && reason.trim().length >= MIN_REASON) create.mutate();
        }}
      >
        <h3 className="text-sm font-semibold">Make a Growth year request for a shop</h3>
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-shop`}>Shop id</Label>
          <Input id={`${id}-shop`} inputMode="numeric" value={shopId} onChange={(e) => setShopId(e.target.value.replace(/\D/g, "").slice(0, 9))} className="max-w-40" />
        </div>
        <Reason id={`${id}-reason`} value={reason} onChange={setReason} hint="At least 10 characters. Kept on the request. The shop's other open requests are cancelled." />
        <Button type="submit" className="w-fit" disabled={!(Number(shopId) > 0) || reason.trim().length < MIN_REASON || create.isPending}>
          {create.isPending && <Loader2 className="animate-spin" />} Make request
        </Button>
      </form>
    </Section>
  );
}

function PaymentItem({ p }: { p: BillingPayment }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const refund = useMutation({
    mutationFn: (body: { bank_reference: string; reason: string }) => api.post<BillingPayment>(`/admin/billing/payments/${p.id}/refund`, body),
    onSuccess: () => {
      toast.success(`Payment ${p.id} refunded.`);
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["admin", "billing"] });
    },
  });
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
      <span className="min-w-0">
        <span className="font-medium tabular-nums">{rupees(p.amount_paise)}</span> · {p.request_number} · shop {p.shop_id} · {p.match}
        <span className="block break-all text-xs text-muted-foreground">
          UPI {p.upi_reference} · paid {formatDate(p.paid_at)} · subscription {p.subscription_id}{p.document_id ? ` · document ${p.document_id}` : ""}
          {p.refund ? ` · refunded ${formatDate(p.refund.at)} (${p.refund.bank_reference}): ${p.refund.reason}` : ""}
        </span>
      </span>
      {p.status === "matched" ? (
        <Button size="sm" variant="outline" onClick={() => setOpen(true)}><Undo2 /> Refund</Button>
      ) : (
        <Badge variant="secondary">refunded</Badge>
      )}
      <ReferenceDialog
        open={open}
        onOpenChange={setOpen}
        title={`Refund ${rupees(p.amount_paise)} (${p.request_number})`}
        description="Send the money back by bank transfer first, then record it here. The subscription is refunded and a refund receipt or credit note is issued."
        referenceLabel="Bank transfer reference"
        confirmLabel="Refund"
        pending={refund.isPending}
        onConfirm={(body) => refund.mutate(body)}
      />
    </li>
  );
}

function PaymentsSection() {
  const payments = useQuery({ queryKey: ["admin", "billing", "payments"], queryFn: () => api.get<BillingPayment[]>("/admin/billing/payments") });
  const rows = payments.data;
  return (
    <Section id="payments-title" title="Payments and refunds" description="Matched payments, newest first. A refund needs the bank transfer's reference and a reason.">
      {payments.isError ? <ErrorState error={payments.error} onRetry={() => payments.refetch()} /> : !rows ? <ListSkeleton rows={2} /> : !rows.length ? (
        <p className="text-sm text-muted-foreground">No payments yet.</p>
      ) : (
        <ul className="divide-y">{rows.map((p) => <PaymentItem key={p.id} p={p} />)}</ul>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------ subscriptions by status

function SubscriptionsSection() {
  const [status, setStatus] = useState<SubscriptionStatus>("active");
  const subs = useQuery({
    queryKey: ["admin", "billing", "subscriptions", "list", status],
    queryFn: () => api.get<AdminSubscription[]>("/admin/subscriptions", { status }),
  });
  const rows = subs.data;
  return (
    <Section id="subscriptions-title" title="Subscriptions" description="By status, the ones ending soonest first.">
      <Tabs value={status} onValueChange={(v) => setStatus(v as SubscriptionStatus)} className="mb-3">
        <TabsList className="flex-wrap">
          {STATUSES.map((s) => <TabsTrigger key={s} value={s} className="capitalize">{s}</TabsTrigger>)}
        </TabsList>
      </Tabs>
      {subs.isError ? <ErrorState error={subs.error} onRetry={() => subs.refetch()} /> : !rows ? <ListSkeleton rows={2} /> : !rows.length ? (
        <p className="text-sm text-muted-foreground">No {status} subscriptions.</p>
      ) : (
        <ul className="divide-y text-sm">
          {rows.map((s) => (
            <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
              <span className="min-w-0">
                <span className="font-medium">{s.shops.map((x) => x.name ?? `Shop ${x.id}`).join(", ")}</span>
                <span className="block text-xs text-muted-foreground">
                  {s.id} · {s.plan_code} v{s.plan_version} by the {s.period} · {s.source}{s.founding ? ` · founding slot ${s.founding_slot ?? "-"}` : ""} · {rupees(s.price_paise)}
                </span>
              </span>
              <span className="text-xs text-muted-foreground">{formatDate(s.starts_at)} to {formatDate(s.ends_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

export default function AdminBillingPage() {
  return (
    <>
      <PageTitle title="Billing" description="Payments to NearShop's own account, the documents they produce, and the plans they pay for." />
      <div className="grid gap-6 xl:grid-cols-2">
        <div className="space-y-6">
          <CreditsSection />
          <PaymentsSection />
        </div>
        <div className="space-y-6">
          <FoundingSection />
          <AdvancesSection />
          <RequestsSection />
          <SubscriptionsSection />
        </div>
      </div>
    </>
  );
}
