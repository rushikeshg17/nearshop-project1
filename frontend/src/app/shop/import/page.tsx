"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, ArrowLeft, Check, Download, FileSpreadsheet, Loader2, Upload } from "lucide-react";
import Link from "next/link";
import { useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useMeta } from "@/hooks/use-session";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

type Action = "update" | "add_catalog" | "add_new" | "skip";
interface Row {
  row: number;
  name: string;
  brand: string | null;
  quantity: number | null;
  price: number | null;
  mrp: number | null;
  action: Action;
  product_id: number | null;
  catalog_item_id: number | null;
  match_name: string | null;
  score: number;
  category_slug: string | null;
  issues: string[];
}
interface Preview {
  columns: Record<string, string>;
  rows: Row[];
  summary: Record<string, number>;
  total: number;
}

const ACTION_LABEL: Record<Action, string> = {
  update: "Update my listing",
  add_catalog: "Add (catalogue match)",
  add_new: "Add as new product",
  skip: "Skip",
};

// Sales-report import (P7.2): each line of the billing app's sales register, matched to one of the shop's listings.
type SalesStatus = "matched" | "check" | "unmatched" | "invalid";
interface SalesRow {
  row: number;
  date: string | null;
  name: string;
  bill: string | null;
  quantity: number | null;
  unit_price: number | null;
  status: SalesStatus;
  include: boolean;
  product_id: number | null;
  match_name: string | null;
  score: number;
  issues: string[];
}
interface SalesPreview {
  kind: "sales";
  columns: Record<string, string>;
  rows: SalesRow[];
  summary: Record<SalesStatus, number>;
  total: number;
  start: string;
  end: string;
  replaces: number;
}
interface SalesDone {
  imported: number;
  lines: number;
  replaced: number;
  start: string;
  end: string;
}

const STATUS_LABEL: Record<SalesStatus, string> = {
  matched: "Matched",
  check: "Check match",
  unmatched: "Not listed",
  invalid: "Can't import",
};
const STATUS_CLASS: Record<SalesStatus, string> = {
  matched: "bg-success-soft text-success-ink",
  check: "bg-warning-soft text-warning-ink",
  unmatched: "",
  invalid: "",
};

function formatDay(iso: string) {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

export default function ImportPage() {
  const [kind, setKind] = useState<"stock" | "sales">("stock");
  return (
    <Tabs value={kind} onValueChange={(v) => setKind(v as "stock" | "sales")}>
      <TabsList className="mb-4" aria-label="What to import">
        <TabsTrigger value="stock">Stock list</TabsTrigger>
        <TabsTrigger value="sales">Sales report</TabsTrigger>
      </TabsList>
      <TabsContent value="stock"><StockImport /></TabsContent>
      <TabsContent value="sales"><SalesImport /></TabsContent>
    </Tabs>
  );
}

function SalesImport() {
  const fileRef = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [preview, setPreview] = useState<SalesPreview | null>(null);
  const [rows, setRows] = useState<SalesRow[]>([]);
  const [done, setDone] = useState<SalesDone | null>(null);

  const upload = useMutation({
    mutationFn: (file: File) => api.upload<SalesPreview>("/owner/import/preview?kind=sales", file),
    onSuccess: (p) => {
      setPreview(p);
      setRows(p.rows);
      setDone(null);
    },
  });
  const included = rows.filter((r) => r.include);
  const commit = useMutation({
    mutationFn: () =>
      api.post<SalesDone>("/owner/import/commit", {
        kind: "sales",
        start: preview?.start,
        end: preview?.end,
        rows: included.map((r) => ({ row: r.row, product_id: r.product_id, date: r.date, quantity: r.quantity, unit_price: r.unit_price, bill: r.bill })),
      }),
    onSuccess: (r) => {
      setDone(r);
      toast.success(`${r.lines} sales lines imported`);
    },
  });

  function pick(files: FileList | null) {
    const f = files?.[0];
    if (f) upload.mutate(f);
  }

  if (done) {
    return (
      <div className="mx-auto max-w-lg py-16 text-center">
        <div className="mx-auto grid size-14 place-items-center rounded-2xl bg-success-soft"><Check className="size-7 text-success" /></div>
        <h1 className="mt-5 text-2xl font-semibold">Sales imported</h1>
        <p className="mt-2 text-muted-foreground">
          {done.lines} sales lines from {formatDay(done.start)} to {formatDay(done.end)} saved
          {done.replaced ? `, replacing ${done.replaced} rows from an earlier upload` : ""}. Demand forecasts and bundle suggestions learn from them.
        </p>
        <Button className="mt-6" variant="outline" onClick={() => { setPreview(null); setDone(null); }}>Import another report</Button>
      </div>
    );
  }

  if (!preview) {
    return (
      <>
        <PageTitle title="Import sales from your billing app" description="Upload the sales report your billing app exports, so demand forecasts and bundle suggestions learn from everything you sell, not only NearShop orders." />
        <div
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files); }}
          className={cn("flex flex-col items-center rounded-3xl border-2 border-dashed bg-card px-6 py-14 text-center transition-colors", drag && "border-brand bg-brand-soft/40")}
        >
          <div className="grid size-14 place-items-center rounded-2xl bg-brand-soft"><FileSpreadsheet className="size-7 text-brand" /></div>
          <p className="mt-4 font-semibold">Drop your sales report (.xlsx or .csv) here</p>
          <p className="mt-1 text-sm text-muted-foreground">Needs a date, an item and a quantity column. Bill number and rate or amount are picked up too. Up to 90 days per upload.</p>
          <Button className="mt-5" onClick={() => fileRef.current?.click()} disabled={upload.isPending}>
            {upload.isPending ? <Loader2 className="animate-spin" /> : <Upload />} Choose file
          </Button>
          <input ref={fileRef} type="file" accept=".xlsx,.csv" className="hidden" onChange={(e) => pick(e.target.files)} />
        </div>
        <div className="mt-6 grid gap-4 md:grid-cols-3">
          <div className="rounded-2xl border bg-card p-5">
            <p className="font-semibold">1. Export</p>
            <p className="mt-1 text-sm text-muted-foreground">In Tally, Vyapar or your billing app, export the item-wise sales report (sales register) for up to 90 days.</p>
          </div>
          <div className="rounded-2xl border bg-card p-5">
            <p className="font-semibold">2. Check the matches</p>
            <p className="mt-1 text-sm text-muted-foreground">Every line is matched to your listings. Items you don&apos;t list yet are left out: add them with the stock import first.</p>
          </div>
          <div className="rounded-2xl border bg-card p-5">
            <p className="font-semibold">3. Import</p>
            <p className="mt-1 text-sm text-muted-foreground">Uploading the same dates again replaces them, so nothing is counted twice.</p>
          </div>
        </div>
      </>
    );
  }

  return (
    <div className="pb-24">
      <button onClick={() => setPreview(null)} className="mb-4 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> Choose a different file
      </button>
      <PageTitle
        title="Check before importing"
        description={`${preview.total} sales lines from ${formatDay(preview.start)} to ${formatDay(preview.end)}. Columns detected: ${Object.entries(preview.columns).map(([k, v]) => `${k} = "${v}"`).join(", ")}.`}
      />
      <div className="mb-4 flex flex-wrap gap-2 text-sm">
        <Badge className={STATUS_CLASS.matched}>{preview.summary.matched} matched</Badge>
        {preview.summary.check > 0 && <Badge className={STATUS_CLASS.check}>{preview.summary.check} to check</Badge>}
        {preview.summary.unmatched > 0 && <Badge variant="outline">{preview.summary.unmatched} not in your listings</Badge>}
        {preview.summary.invalid > 0 && <Badge variant="outline">{preview.summary.invalid} can&apos;t be imported</Badge>}
      </div>
      {preview.replaces > 0 && (
        <p className="mb-4 flex items-center gap-1.5 text-sm text-warning-ink">
          <AlertCircle className="size-4" /> You already imported {preview.replaces} sales rows for these dates. Importing replaces them.
        </p>
      )}
      <div className="overflow-x-auto rounded-2xl border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12"><span className="sr-only">Import</span></TableHead>
              <TableHead className="w-28">Date</TableHead>
              <TableHead>Item in your report</TableHead>
              <TableHead className="w-28">Match</TableHead>
              <TableHead className="w-24">Bill</TableHead>
              <TableHead className="w-16 text-right">Qty</TableHead>
              <TableHead className="w-24 text-right">Rate (Rs)</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r, i) => {
              const importable = r.status === "matched" || r.status === "check";
              return (
                <TableRow key={`${r.row}-${i}`} className={cn(!r.include && "opacity-60")}>
                  <TableCell>
                    <Checkbox checked={r.include} disabled={!importable} aria-label={`Import ${r.name}`}
                      onCheckedChange={(v) => setRows((rs) => rs.map((x, j) => (j === i ? { ...x, include: v === true } : x)))} />
                  </TableCell>
                  <TableCell className="text-sm tabular">{r.date ? formatDay(r.date) : "-"}</TableCell>
                  <TableCell className="max-w-64">
                    <p className="truncate font-medium">{r.name}</p>
                    {r.match_name && (
                      <p className="truncate text-xs text-muted-foreground">
                        Your listing: {r.match_name} <span className={cn("tabular", r.score < 86 && "text-warning-ink")}>({r.score}%)</span>
                      </p>
                    )}
                    {r.issues.map((m) => (
                      <p key={m} className="flex items-center gap-1 text-xs text-warning-ink"><AlertCircle className="size-3" /> {m}</p>
                    ))}
                  </TableCell>
                  <TableCell>
                    <Badge variant={importable ? "default" : "outline"} className={STATUS_CLASS[r.status]}>{STATUS_LABEL[r.status]}</Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">{r.bill ?? "-"}</TableCell>
                  <TableCell className="text-right tabular">{r.quantity ?? "-"}</TableCell>
                  <TableCell className="text-right tabular">{r.unit_price ?? "-"}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
      <div className="fixed inset-x-0 bottom-0 z-40 border-t bg-background/95 p-3 backdrop-blur lg:left-[248px]">
        <div className="mx-auto flex max-w-6xl items-center gap-3 px-1 md:px-3">
          <p className="text-sm text-muted-foreground">Nothing is saved until you import.</p>
          <Button className="ml-auto" disabled={included.length === 0 || commit.isPending} onClick={() => commit.mutate()}>
            {commit.isPending && <Loader2 className="animate-spin" />} Import {included.length} sales lines
          </Button>
        </div>
      </div>
    </div>
  );
}

function StockImport() {
  const qc = useQueryClient();
  const { data: meta } = useMeta();
  const fileRef = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [done, setDone] = useState<{ updated: number; added: number; skipped: number } | null>(null);

  const upload = useMutation({
    mutationFn: (file: File) => api.upload<Preview>("/owner/import/preview", file),
    onSuccess: (p) => {
      setPreview(p);
      setRows(p.rows);
      setDone(null);
      if (!p.rows.length) toast.error("No items found in that file");
    },
  });
  const commit = useMutation({
    mutationFn: () => api.post<{ updated: number; added: number; skipped: number }>("/owner/import/commit", { rows }),
    onSuccess: (r) => {
      setDone(r);
      qc.invalidateQueries({ queryKey: ["owner"] });
      toast.success(`${r.added} added, ${r.updated} updated`);
    },
  });

  const edit = (i: number, patch: Partial<Row>) => setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const missingPrice = rows.filter((r) => r.action !== "skip" && r.action !== "update" && !r.price).length;
  const counts = useMemo(
    () => ({
      update: rows.filter((r) => r.action === "update").length,
      add: rows.filter((r) => r.action === "add_catalog" || r.action === "add_new").length,
      skip: rows.filter((r) => r.action === "skip").length,
    }),
    [rows],
  );

  function pick(files: FileList | null) {
    const f = files?.[0];
    if (f) upload.mutate(f);
  }

  if (done) {
    return (
      <div className="mx-auto max-w-lg py-16 text-center">
        <div className="mx-auto grid size-14 place-items-center rounded-2xl bg-success-soft"><Check className="size-7 text-success" /></div>
        <h1 className="mt-5 text-2xl font-semibold">Stock imported</h1>
        <p className="mt-2 text-muted-foreground">
          {done.added} new items listed, {done.updated} updated{done.skipped ? `, ${done.skipped} skipped` : ""}. Customers nearby can find them now.
        </p>
        <div className="mt-6 flex justify-center gap-2">
          <Button asChild><Link href="/shop/inventory?sort=updated">Open inventory</Link></Button>
          <Button variant="outline" onClick={() => { setPreview(null); setDone(null); }}>Import another file</Button>
        </div>
      </div>
    );
  }

  if (!preview) {
    return (
      <>
        <PageTitle title="Import stock from Excel" description="The fastest way to put your whole shelf online. Export your item list from your billing app and upload it here." />
        <div
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files); }}
          className={cn("flex flex-col items-center rounded-3xl border-2 border-dashed bg-card px-6 py-14 text-center transition-colors", drag && "border-brand bg-brand-soft/40")}
        >
          <div className="grid size-14 place-items-center rounded-2xl bg-brand-soft"><FileSpreadsheet className="size-7 text-brand" /></div>
          <p className="mt-4 font-semibold">Drop your .xlsx or .csv file here</p>
          <p className="mt-1 text-sm text-muted-foreground">Needs at least an item name column. Quantity and price are picked up automatically.</p>
          <Button className="mt-5" onClick={() => fileRef.current?.click()} disabled={upload.isPending}>
            {upload.isPending ? <Loader2 className="animate-spin" /> : <Upload />} Choose file
          </Button>
          <input ref={fileRef} type="file" accept=".xlsx,.csv" className="hidden" onChange={(e) => pick(e.target.files)} />
        </div>
        <div className="mt-6 grid gap-4 md:grid-cols-3">
          <div className="rounded-2xl border bg-card p-5">
            <p className="font-semibold">1. Export</p>
            <p className="mt-1 text-sm text-muted-foreground">Most billing apps (Vyapar, Tally, Marg, Busy and others) can export an item or stock report to Excel. Any sheet with an item name column works.</p>
          </div>
          <div className="rounded-2xl border bg-card p-5">
            <p className="font-semibold">2. Check the matches</p>
            <p className="mt-1 text-sm text-muted-foreground">We match every row to your listings and the NearShop catalogue. You review before anything changes.</p>
          </div>
          <div className="rounded-2xl border bg-card p-5">
            <p className="font-semibold">3. Import</p>
            <p className="mt-1 text-sm text-muted-foreground">Upload again every evening to keep counts fresh. Fresh shops rank higher in search.</p>
          </div>
        </div>
        <p className="mt-6 text-sm text-muted-foreground">
          No billing app?{" "}
          <a href="/api/owner/import/template" className="inline-flex items-center gap-1 font-medium text-foreground underline underline-offset-4">
            <Download className="size-3.5" /> Download the template
          </a>{" "}
          and fill it in Excel or Google Sheets.
        </p>
      </>
    );
  }

  return (
    <div className="pb-24">
      <button onClick={() => setPreview(null)} className="mb-4 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> Choose a different file
      </button>
      <PageTitle
        title="Check before importing"
        description={`Found ${preview.total} items. Columns detected: ${Object.entries(preview.columns).map(([k, v]) => `${k} = "${v}"`).join(", ")}.`}
      />
      <div className="mb-4 flex flex-wrap gap-2 text-sm">
        <Badge variant="secondary">{counts.update} update existing</Badge>
        <Badge className="bg-success-soft text-success-ink">{counts.add} new listings</Badge>
        {counts.skip > 0 && <Badge variant="outline">{counts.skip} skipped</Badge>}
        {missingPrice > 0 && <Badge className="bg-warning-soft text-warning-ink">{missingPrice} need a price</Badge>}
      </div>
      <div className="overflow-x-auto rounded-2xl border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12">Row</TableHead>
              <TableHead>Item in your file</TableHead>
              <TableHead>What happens</TableHead>
              <TableHead className="w-24 text-right">Qty</TableHead>
              <TableHead className="w-28 text-right">Price (Rs)</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r, i) => (
              <TableRow key={`${r.row}-${i}`} className={cn(r.action === "skip" && "opacity-50")}>
                <TableCell className="text-xs text-muted-foreground tabular">{r.row}</TableCell>
                <TableCell className="max-w-64">
                  <p className="truncate font-medium">{r.name}</p>
                  {r.match_name && r.action !== "add_new" && (
                    <p className="truncate text-xs text-muted-foreground">
                      Matched: {r.match_name} <span className={cn("tabular", r.score < 86 && "text-warning-ink")}>({r.score}%)</span>
                    </p>
                  )}
                  {r.issues.map((m) => (
                    <p key={m} className="flex items-center gap-1 text-xs text-warning-ink"><AlertCircle className="size-3" /> {m}</p>
                  ))}
                </TableCell>
                <TableCell className="min-w-56">
                  <div className="flex flex-col gap-1.5">
                    <Select value={r.action} onValueChange={(v) => edit(i, { action: v as Action })}>
                      <SelectTrigger className="h-8 w-full" aria-label={`What to do with ${r.name}`}><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {(["update", "add_catalog", "add_new", "skip"] as Action[])
                          .filter((a) => (a === "update" ? !!r.product_id : a === "add_catalog" ? !!r.catalog_item_id && !r.product_id : true))
                          .map((a) => <SelectItem key={a} value={a}>{ACTION_LABEL[a]}</SelectItem>)}
                      </SelectContent>
                    </Select>
                    {r.action === "add_new" && (
                      <Select value={r.category_slug ?? ""} onValueChange={(v) => edit(i, { category_slug: v })}>
                        <SelectTrigger className="h-8 w-full" aria-label={`Category for ${r.name}`}><SelectValue placeholder="Category" /></SelectTrigger>
                        <SelectContent>{meta?.categories.map((c) => <SelectItem key={c.slug} value={c.slug}>{c.name}</SelectItem>)}</SelectContent>
                      </Select>
                    )}
                  </div>
                </TableCell>
                <TableCell className="text-right">
                  <Input value={r.quantity ?? ""} inputMode="numeric" placeholder="-" className="ml-auto h-8 w-20 text-right tabular" aria-label={`Quantity for ${r.name}`}
                    onChange={(e) => edit(i, { quantity: e.target.value === "" ? null : Number(e.target.value.replace(/\D/g, "")) })} />
                </TableCell>
                <TableCell className="text-right">
                  <Input value={r.price ?? ""} inputMode="decimal" placeholder="Price" aria-label={`Price for ${r.name}`}
                    className={cn("ml-auto h-8 w-24 text-right tabular", !r.price && r.action !== "skip" && r.action !== "update" && "border-warning")}
                    onChange={(e) => edit(i, { price: e.target.value === "" ? null : Number(e.target.value.replace(/[^\d.]/g, "")) })} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <div className="fixed inset-x-0 bottom-0 z-40 border-t bg-background/95 p-3 backdrop-blur lg:left-[248px]">
        <div className="mx-auto flex max-w-6xl items-center gap-3 px-1 md:px-3">
          <p className="text-sm text-muted-foreground">
            {missingPrice ? `${missingPrice} rows need a price, or set them to Skip` : "Nothing changes until you import."}
          </p>
          <Button className="ml-auto" disabled={missingPrice > 0 || commit.isPending || counts.update + counts.add === 0} onClick={() => commit.mutate()}>
            {commit.isPending && <Loader2 className="animate-spin" />} Import {counts.update + counts.add} items
          </Button>
        </div>
      </div>
    </div>
  );
}
