"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Gauge, Loader2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

// GET /api/admin/kpis (backend/app/services/kpis.py report, P9.12; plan section 8.23).
type Unit = "count" | "pct" | "hours" | "weeks" | "rupees" | "minutes";
interface Threshold {
  gate: string;
  gate_name: string;
  gate_date: string;
  op: ">=" | "<=";
  value: number;
}
interface Figure {
  key: string;
  label: string;
  unit: Unit;
  group: string;
  value: number | null;
  threshold: Threshold | null;
  pass: boolean | null;
  detail: Record<string, unknown>;
}
interface EnteredInput {
  id: number;
  key: string;
  month: string;
  value: number;
  source_note: string;
  entered_by: number | null;
  entered_at: string;
}
interface Snapshot {
  month: string;
  taken_at: string;
  city: string | null;
  figures: Figure[];
}
interface KpiReport {
  live: { as_of: string; city: string; month: string; figures: Figure[] };
  snapshots: Snapshot[];
  inputs: Record<string, EnteredInput | null>;
  entered: { key: string; label: string; unit: Unit; source: string }[];
  gates: { key: string; name: string; date: string }[];
  note: string;
}

const NOTE_MIN = 10; // kpis.NOTE_MIN

function formatDate(iso: string | null | undefined) {
  return iso ? new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "-";
}

function formatMonth(key: string) {
  const [y, m] = key.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("en-IN", { month: "short", year: "numeric", timeZone: "UTC" });
}

function formatValue(value: number | null, unit: Unit) {
  if (value === null || value === undefined) return "No data";
  const n = Number.isInteger(value) ? value.toLocaleString("en-IN") : value.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  switch (unit) {
    case "pct":
      return `${n}%`;
    case "hours":
      return `${n} h`;
    case "weeks":
      return `${n} wk`;
    case "rupees":
      return `₹${n}`;
    case "minutes":
      return `${n} min`;
    default:
      return n;
  }
}

function thresholdText(t: Threshold | null, unit: Unit) {
  if (!t) return "No threshold";
  return `${t.op === ">=" ? "at least" : "at most"} ${formatValue(t.value, unit)}`;
}

function Verdict({ pass }: { pass: boolean | null }) {
  if (pass === null) return <Badge variant="outline">No data</Badge>;
  return pass ? (
    <Badge className="bg-success-soft text-success-ink">Pass</Badge>
  ) : (
    <Badge variant="destructive">Fail</Badge>
  );
}

/** One line under a figure: the counts it is computed from. */
function detailText(f: Figure): string | null {
  const d = f.detail as Record<string, number | string | null | undefined>;
  switch (f.key) {
    case "arrival_failure_pct":
      return `${d.failures} arrival failures of ${d.denominator} (${d.completions} completions + failures) since ${formatDate(String(d.since))}`;
    case "completions_90d":
      return `Since ${formatDate(String(d.since))}`;
    case "completions_month":
      return `${d.month}: ${d.pickups} pickups, ${d.deliveries} deliveries. This month so far: ${d.this_month_so_far}`;
    case "month2_return_pct":
      return `${d.returned} of ${d.cohort} first-time customers of ${d.cohort_month} came back in ${d.return_month}`;
    case "zero_result_pct":
      return `${d.zero} of ${d.searches} searches`;
    case "shop_paid_pct":
      return `${d.paid} paying of ${d.active} active shops`;
    case "founding_renewal_pct":
      return `${d.renewed} of ${d.cohort} renewed within 30 days; ${d.pending} still inside their 30 days`;
    case "annual_inactivity_pct":
      return d.then_active === null || d.then_active === undefined
        ? `Needs the snapshot of ${d.from_month}`
        : `${d.inactive} of ${d.then_active} shops active in ${d.from_month} are not active now`;
    case "accurate_pct":
      return d.month ? `${d.accurate} of ${d.statements} statements for ${d.month}` : "No statements yet";
    case "fresh_weeks":
      return `Last complete week from ${formatDate(String(d.last_complete_week))}`;
    case "median_stock_age_hours":
      return `${d.listings} listings`;
    case "partner_plans":
      return String(d.note ?? "");
    case "activation_cost_rupees":
    case "support_minutes":
      return d.entered_at ? `For ${d.month}, entered ${formatDateTime(String(d.entered_at))}: ${d.source_note}` : "Not entered yet";
    default:
      return null;
  }
}

function LiveFigures({ figures }: { figures: Figure[] }) {
  const groups = Array.from(new Set(figures.map((f) => f.group)));
  return (
    <div className="space-y-6">
      {groups.map((group) => (
        <section key={group} aria-labelledby={`kpi-${group}`}>
          <h2 id={`kpi-${group}`} className="mb-2 text-sm font-semibold text-muted-foreground">{group}</h2>
          <ul className="grid gap-3 md:grid-cols-2">
            {figures.filter((f) => f.group === group).map((f) => (
              <li key={f.key} className={cn("rounded-2xl border bg-card p-4", f.pass === false && "border-destructive/50")}>
                <div className="flex items-start justify-between gap-3">
                  <p className="text-sm font-medium">{f.label}</p>
                  <Verdict pass={f.pass} />
                </div>
                <p className="mt-1 text-2xl font-semibold tabular">{formatValue(f.value, f.unit)}</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {f.threshold
                    ? `${f.threshold.gate_name} (${formatDate(f.threshold.gate_date)}): ${thresholdText(f.threshold, f.unit)}`
                    : "Shown separately; not judged"}
                </p>
                {detailText(f) && <p className="mt-1 text-xs text-muted-foreground">{detailText(f)}</p>}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function EnterInput({ report }: { report: KpiReport }) {
  const qc = useQueryClient();
  const [key, setKey] = useState(report.entered[0]?.key ?? "activation_cost_rupees");
  const [month, setMonth] = useState(report.live.month);
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const save = useMutation({
    mutationFn: () => api.post("/admin/kpis/inputs", { key, month, value: Number(value), source_note: note.trim() }),
    onSuccess: () => {
      toast.success("Figure entered.");
      setValue("");
      setNote("");
      qc.invalidateQueries({ queryKey: ["admin", "kpis"] });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const valid = value.trim() !== "" && Number(value) >= 0 && /^\d{4}-(0[1-9]|1[0-2])$/.test(month) && note.trim().length >= NOTE_MIN;
  return (
    <section aria-labelledby="kpi-entered" className="rounded-2xl border bg-card p-4">
      <h2 id="kpi-entered" className="text-base font-semibold">Entered each month</h2>
      <p className="mt-1 text-sm text-muted-foreground">These two figures cannot be computed by NearShop. Enter them each month with a note saying where they come from.</p>
      <ul className="mt-3 space-y-1 text-sm">
        {report.entered.map((e) => {
          const last = report.inputs[e.key];
          return (
            <li key={e.key}>
              <span className="font-medium">{e.label}</span> <span className="text-muted-foreground">(from {e.source}):</span>{" "}
              {last ? `${formatValue(last.value, e.unit)} for ${last.month}, last entered ${formatDateTime(last.entered_at)}` : "never entered"}
            </li>
          );
        })}
      </ul>
      <form
        className="mt-4 grid gap-3 sm:grid-cols-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) save.mutate();
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor="kpi-key">Figure</Label>
          <select
            id="kpi-key"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            className="h-9 w-full rounded-md border bg-transparent px-3 text-sm"
          >
            {report.entered.map((e) => <option key={e.key} value={e.key}>{e.label}</option>)}
          </select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="kpi-month">Month (YYYY-MM)</Label>
          <Input id="kpi-month" value={month} onChange={(e) => setMonth(e.target.value.slice(0, 7))} inputMode="numeric" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="kpi-value">Value</Label>
          <Input id="kpi-value" type="number" min={0} step="any" value={value} onChange={(e) => setValue(e.target.value)} />
        </div>
        <div className="space-y-1.5 sm:col-span-3">
          <Label htmlFor="kpi-note">Source note</Label>
          <Textarea id="kpi-note" rows={2} value={note} onChange={(e) => setNote(e.target.value.slice(0, 500))} aria-describedby="kpi-note-hint" />
          <p id="kpi-note-hint" className="text-xs text-muted-foreground">Required, at least {NOTE_MIN} characters: the log and the period it covers.</p>
        </div>
        <div className="sm:col-span-3">
          <Button type="submit" disabled={!valid || save.isPending}>
            {save.isPending && <Loader2 className="animate-spin" />} Enter figure
          </Button>
        </div>
      </form>
    </section>
  );
}

function SnapshotTable({ report }: { report: KpiReport }) {
  const snaps = report.snapshots;
  if (!snaps.length) {
    return <p className="text-sm text-muted-foreground">No snapshot yet. The first is stored on the 2nd of next month.</p>;
  }
  const rows = report.live.figures;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="min-w-48">Figure</TableHead>
          {snaps.map((s) => (
            <TableHead key={s.month} className="text-right whitespace-nowrap" title={`Taken ${formatDateTime(s.taken_at)}`}>
              {formatMonth(s.month)}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((r) => (
          <TableRow key={r.key}>
            <TableCell className="text-sm">{r.label}</TableCell>
            {snaps.map((s) => {
              const f = s.figures.find((x) => x.key === r.key);
              return (
                <TableCell
                  key={s.month}
                  className={cn("text-right tabular whitespace-nowrap", f?.pass === false && "text-destructive", f?.pass === true && "text-success-ink")}
                >
                  {f ? formatValue(f.value, f.unit) : "-"}
                </TableCell>
              );
            })}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export default function AdminKpisPage() {
  const report = useQuery({ queryKey: ["admin", "kpis"], queryFn: () => api.get<KpiReport>("/admin/kpis") });
  const d = report.data;
  return (
    <>
      <PageTitle
        title="Gate KPIs"
        description="The figures the business model's gates use, computed the same way every month, each against the threshold of its next gate."
        action={
          <Button asChild variant="outline" size="sm">
            <a href="/api/admin/kpis/snapshots.csv" download>
              <Download /> Snapshots (CSV)
            </a>
          </Button>
        }
      />
      {report.isError ? (
        <ErrorState error={report.error} onRetry={() => report.refetch()} />
      ) : !d ? (
        <ListSkeleton />
      ) : !d.live.figures.length ? (
        <EmptyState icon={Gauge} title="No figures" description="The gate figures appear here." />
      ) : (
        <div className="space-y-8">
          <p role="note" className="rounded-xl border-l-2 border-brand/50 bg-muted/50 px-3 py-2 text-sm">{d.note}</p>
          <p className="text-sm text-muted-foreground">
            Live for {d.live.city}, as of {formatDateTime(d.live.as_of)}. Monthly figures are for {formatMonth(d.live.month)}. Gates:{" "}
            {d.gates.map((g) => `${g.name} ${formatDate(g.date)}`).join("; ")}.
          </p>
          <LiveFigures figures={d.live.figures} />
          <EnterInput report={d} />
          <section aria-labelledby="kpi-snapshots">
            <h2 id="kpi-snapshots" className="mb-1 text-base font-semibold">Monthly snapshots</h2>
            <p className="mb-3 text-sm text-muted-foreground">Stored on the 2nd of each month and never changed afterwards. The last 12 are shown; the CSV has them all.</p>
            <SnapshotTable report={d} />
          </section>
        </div>
      )}
    </>
  );
}
