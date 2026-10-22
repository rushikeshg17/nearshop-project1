"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, ArrowLeft, Check, Download, FileSpreadsheet, Loader2, Upload } from "lucide-react";
import Link from "next/link";
import { useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { useI18n } from "@/components/providers";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useMeta } from "@/hooks/use-session";
import { api } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { type LanguageCode, languageInfo, type MessageKey, plural, richText, type Translate } from "@/lib/i18n";
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

// The columns the importer recognises (backend/app/services/importer.py, COLUMN_ALIASES), named in the interface
// language; an unknown one is shown as the API names it.
const FIELDS = ["name", "quantity", "price", "mrp", "brand", "category", "sku"] as const;

// The importer's row notes (backend/app/services/importer.py) arrive as English text; the known ones are shown in
// the interface language, and any other as the API wrote it.
const ISSUES: Record<string, MessageKey> = {
  "Check the catalogue match": "owner.import.issues.checkMatch",
  "No price in file; using the typical local price": "owner.import.issues.typicalPrice",
  "No selling price in file; using MRP": "owner.import.issues.usingMrp",
  "No quantity; stock will not be changed": "owner.import.issues.noQuantityUpdate",
  "No quantity; will be added as 0": "owner.import.issues.noQuantityAdd",
  "Price missing: enter one before importing": "owner.import.issues.priceMissing",
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

const STATUS_LABEL: Record<SalesStatus, MessageKey> = {
  matched: "owner.import.sales.status.matched",
  check: "owner.import.sales.status.check",
  unmatched: "owner.import.sales.status.unmatched",
  invalid: "owner.import.sales.status.invalid",
};
const STATUS_CLASS: Record<SalesStatus, string> = {
  matched: "bg-success-soft text-success-ink",
  check: "bg-warning-soft text-warning-ink",
  unmatched: "",
  invalid: "",
};

// The sales columns the importer recognises (backend/app/services/importer.py, SALES_COLUMN_ALIASES).
const SALES_FIELDS = ["date", "item", "quantity", "bill", "rate", "amount"] as const;

// The importer's notes on a sales line (backend/app/services/importer.py, sales_preview) arrive as English text;
// the known ones are shown in the interface language, and any other as the API wrote it.
const SALES_ISSUES: Record<string, MessageKey> = {
  "The date is in the future": "owner.import.sales.issues.futureDate",
  "No quantity": "owner.import.sales.issues.noQuantity",
  "A return or credit note: not imported": "owner.import.sales.issues.return",
  "Quantity is less than 1": "owner.import.sales.issues.lessThanOne",
  "No rate or amount in file; using your listed price": "owner.import.sales.issues.listedPrice",
  "Not in your listings: add it (stock import) and upload again": "owner.import.sales.issues.notListed",
  "Check the match": "owner.import.sales.issues.checkMatch",
};

function salesIssue(t: Translate, message: string): string {
  if (Object.hasOwn(SALES_ISSUES, message)) return t(SALES_ISSUES[message]);
  let m = /^Quantity (\S+) rounded to (\d+)$/.exec(message);
  if (m) return t("owner.import.sales.issues.rounded", { quantity: m[1], whole: m[2] });
  m = /^Couldn't read the date '(.*)'$/.exec(message);
  if (m) return t("owner.import.sales.issues.badDate", { value: m[1] });
  return message;
}

function formatDay(iso: string, language: LanguageCode) {
  return new Date(`${iso}T00:00:00`).toLocaleDateString(languageInfo(language).htmlLang, { day: "numeric", month: "short", year: "numeric" });
}

export default function ImportPage() {
  const { t } = useI18n();
  const [kind, setKind] = useState<"stock" | "sales">("stock");
  return (
    <Tabs value={kind} onValueChange={(v) => setKind(v as "stock" | "sales")}>
      <TabsList className="mb-4" aria-label={t("owner.import.tabs.label")}>
        <TabsTrigger value="stock">{t("owner.import.tabs.stock")}</TabsTrigger>
        <TabsTrigger value="sales">{t("owner.import.tabs.sales")}</TabsTrigger>
      </TabsList>
      {/* Both panels stay mounted, so switching tabs keeps a preview the owner is part-way through. */}
      <TabsContent value="stock" forceMount className="data-[state=inactive]:hidden"><StockImport /></TabsContent>
      <TabsContent value="sales" forceMount className="data-[state=inactive]:hidden"><SalesImport /></TabsContent>
    </Tabs>
  );
}

function SalesImport() {
  const { t, language } = useI18n();
  const fileRef = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [preview, setPreview] = useState<SalesPreview | null>(null);
  const [rows, setRows] = useState<SalesRow[]>([]);
  const [done, setDone] = useState<SalesDone | null>(null);
  const count = (key: "lines" | "matched" | "check" | "unmatched" | "invalid" | "importLines", n: number) =>
    t(`owner.import.sales.counts.${key}.${plural(language, n)}`, { count: formatNumber(n) });
  const day = (iso: string) => formatDay(iso, language);
  const fieldName = (k: string) =>
    (SALES_FIELDS as readonly string[]).includes(k) ? t(`owner.import.sales.fields.${k as (typeof SALES_FIELDS)[number]}`) : k;

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
      toast.success(t("owner.import.sales.doneToast", { lines: count("lines", r.lines) }));
    },
  });

  function pick(files: FileList | null) {
    const f = files?.[0];
    if (f) upload.mutate(f);
  }

  if (done) {
    const values = { lines: count("lines", done.lines), start: day(done.start), end: day(done.end) };
    return (
      <div className="mx-auto max-w-lg py-16 text-center">
        <div className="mx-auto grid size-14 place-items-center rounded-2xl bg-success-soft"><Check className="size-7 text-success" /></div>
        <h1 className="mt-5 text-2xl font-semibold">{t("owner.import.sales.doneTitle")}</h1>
        <p className="mt-2 text-muted-foreground">
          {done.replaced
            ? t("owner.import.sales.doneBodyReplaced", { ...values, replaced: formatNumber(done.replaced) })
            : t("owner.import.sales.doneBody", values)}
        </p>
        <Button className="mt-6" variant="outline" onClick={() => { setPreview(null); setDone(null); }}>{t("owner.import.sales.importAnother")}</Button>
      </div>
    );
  }

  if (!preview) {
    return (
      <>
        <PageTitle title={t("owner.import.sales.title")} description={t("owner.import.sales.description")} />
        <div
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files); }}
          className={cn("flex flex-col items-center rounded-3xl border-2 border-dashed bg-card px-6 py-14 text-center transition-colors", drag && "border-brand bg-brand-soft/40")}
        >
          <div className="grid size-14 place-items-center rounded-2xl bg-brand-soft"><FileSpreadsheet className="size-7 text-brand" /></div>
          <p className="mt-4 font-semibold">{t("owner.import.sales.dropTitle")}</p>
          <p className="mt-1 text-sm text-muted-foreground">{t("owner.import.sales.dropHint")}</p>
          <Button className="mt-5" onClick={() => fileRef.current?.click()} disabled={upload.isPending}>
            {upload.isPending ? <Loader2 className="animate-spin" /> : <Upload />} {t("owner.import.chooseFile")}
          </Button>
          <input ref={fileRef} type="file" accept=".xlsx,.csv" className="hidden" onChange={(e) => { pick(e.target.files); e.target.value = ""; }} />
        </div>
        <div className="mt-6 grid gap-4 md:grid-cols-3">
          <div className="rounded-2xl border bg-card p-5">
            <p className="font-semibold">{t("owner.import.sales.steps.export.title")}</p>
            <p className="mt-1 text-sm text-muted-foreground">{t("owner.import.sales.steps.export.body")}</p>
          </div>
          <div className="rounded-2xl border bg-card p-5">
            <p className="font-semibold">{t("owner.import.sales.steps.check.title")}</p>
            <p className="mt-1 text-sm text-muted-foreground">{t("owner.import.sales.steps.check.body")}</p>
          </div>
          <div className="rounded-2xl border bg-card p-5">
            <p className="font-semibold">{t("owner.import.sales.steps.import.title")}</p>
            <p className="mt-1 text-sm text-muted-foreground">{t("owner.import.sales.steps.import.body")}</p>
          </div>
        </div>
      </>
    );
  }

  return (
    <div className="pb-24">
      <button onClick={() => setPreview(null)} className="mb-4 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> {t("owner.import.chooseDifferent")}
      </button>
      <PageTitle
        title={t("owner.import.checkTitle")}
        description={t("owner.import.sales.checkDescription", {
          lines: count("lines", preview.total),
          start: day(preview.start),
          end: day(preview.end),
          columns: Object.entries(preview.columns)
            .map(([k, v]) => t("owner.import.columnPair", { field: fieldName(k), header: v }))
            .join(", "),
        })}
      />
      <div className="mb-4 flex flex-wrap gap-2 text-sm">
        <Badge className={STATUS_CLASS.matched}>{count("matched", preview.summary.matched)}</Badge>
        {preview.summary.check > 0 && <Badge className={STATUS_CLASS.check}>{count("check", preview.summary.check)}</Badge>}
        {preview.summary.unmatched > 0 && <Badge variant="outline">{count("unmatched", preview.summary.unmatched)}</Badge>}
        {preview.summary.invalid > 0 && <Badge variant="outline">{count("invalid", preview.summary.invalid)}</Badge>}
      </div>
      {preview.replaces > 0 && (
        <p className="mb-4 flex items-center gap-1.5 text-sm text-warning-ink">
          <AlertCircle className="size-4" /> {t("owner.import.sales.replaces", { count: formatNumber(preview.replaces) })}
        </p>
      )}
      <div className="overflow-x-auto rounded-2xl border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12"><span className="sr-only">{t("owner.import.sales.table.import")}</span></TableHead>
              <TableHead className="w-28">{t("owner.import.sales.table.date")}</TableHead>
              <TableHead>{t("owner.import.sales.table.item")}</TableHead>
              <TableHead className="w-28">{t("owner.import.sales.table.match")}</TableHead>
              <TableHead className="w-24">{t("owner.import.sales.table.bill")}</TableHead>
              <TableHead className="w-16 text-right">{t("owner.import.table.qty")}</TableHead>
              <TableHead className="w-24 text-right">{t("owner.import.sales.table.rate")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r, i) => {
              const importable = r.status === "matched" || r.status === "check";
              return (
                <TableRow key={`${r.row}-${i}`} className={cn(!r.include && "opacity-60")}>
                  <TableCell>
                    <Checkbox checked={r.include} disabled={!importable} aria-label={t("owner.import.sales.includeRow", { name: r.name })}
                      onCheckedChange={(v) => setRows((rs) => rs.map((x, j) => (j === i ? { ...x, include: v === true } : x)))} />
                  </TableCell>
                  <TableCell className="text-sm tabular">{r.date ? day(r.date) : "-"}</TableCell>
                  <TableCell className="max-w-64">
                    <p className="truncate font-medium">{r.name}</p>
                    {r.match_name && (
                      <p className="truncate text-xs text-muted-foreground">
                        {richText(t("owner.import.sales.yourListing", { name: r.match_name }), {
                          score: <span className={cn("tabular", r.score < 86 && "text-warning-ink")}>({formatNumber(r.score)}%)</span>,
                        })}
                      </p>
                    )}
                    {r.issues.map((m) => (
                      <p key={m} className="flex items-center gap-1 text-xs text-warning-ink"><AlertCircle className="size-3" /> {salesIssue(t, m)}</p>
                    ))}
                  </TableCell>
                  <TableCell>
                    <Badge variant={importable ? "default" : "outline"} className={STATUS_CLASS[r.status]}>{t(STATUS_LABEL[r.status])}</Badge>
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
          <p className="text-sm text-muted-foreground">{t("owner.import.sales.nothingSaved")}</p>
          <Button className="ml-auto" disabled={included.length === 0 || commit.isPending} onClick={() => commit.mutate()}>
            {commit.isPending && <Loader2 className="animate-spin" />} {count("importLines", included.length)}
          </Button>
        </div>
      </div>
    </div>
  );
}

function StockImport() {
  const { t, language } = useI18n();
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
      if (!p.rows.length) toast.error(t("owner.import.noItems"));
    },
  });
  const commit = useMutation({
    mutationFn: () => api.post<{ updated: number; added: number; skipped: number }>("/owner/import/commit", { rows }),
    onSuccess: (r) => {
      setDone(r);
      qc.invalidateQueries({ queryKey: ["owner"] });
      toast.success(
        t("owner.import.doneToast", {
          added: t(`owner.import.doneToastAdded.${plural(language, r.added)}`, { count: formatNumber(r.added) }),
          updated: t(`owner.import.doneUpdated.${plural(language, r.updated)}`, { count: formatNumber(r.updated) }),
        }),
      );
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

  const fieldName = (k: string) => ((FIELDS as readonly string[]).includes(k) ? t(`owner.import.fields.${k as (typeof FIELDS)[number]}`) : k);
  const toImport = counts.update + counts.add;

  function pick(files: FileList | null) {
    const f = files?.[0];
    if (f) upload.mutate(f);
  }

  if (done) {
    return (
      <div className="mx-auto max-w-lg py-16 text-center">
        <div className="mx-auto grid size-14 place-items-center rounded-2xl bg-success-soft"><Check className="size-7 text-success" /></div>
        <h1 className="mt-5 text-2xl font-semibold">{t("owner.import.doneTitle")}</h1>
        <p className="mt-2 text-muted-foreground">
          {t(done.skipped ? "owner.import.doneBodySkipped" : "owner.import.doneBody", {
            added: t(`owner.import.doneAdded.${plural(language, done.added)}`, { count: formatNumber(done.added) }),
            updated: t(`owner.import.doneUpdated.${plural(language, done.updated)}`, { count: formatNumber(done.updated) }),
            skipped: t(`owner.import.doneSkipped.${plural(language, done.skipped)}`, { count: formatNumber(done.skipped) }),
          })}
        </p>
        <div className="mt-6 flex justify-center gap-2">
          <Button asChild><Link href="/shop/inventory?sort=updated">{t("owner.import.openInventory")}</Link></Button>
          <Button variant="outline" onClick={() => { setPreview(null); setDone(null); }}>{t("owner.import.importAnother")}</Button>
        </div>
      </div>
    );
  }

  if (!preview) {
    return (
      <>
        <PageTitle title={t("owner.import.title")} description={t("owner.import.description")} />
        <div
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files); }}
          className={cn("flex flex-col items-center rounded-3xl border-2 border-dashed bg-card px-6 py-14 text-center transition-colors", drag && "border-brand bg-brand-soft/40")}
        >
          <div className="grid size-14 place-items-center rounded-2xl bg-brand-soft"><FileSpreadsheet className="size-7 text-brand" /></div>
          <p className="mt-4 font-semibold">{t("owner.import.dropTitle")}</p>
          <p className="mt-1 text-sm text-muted-foreground">{t("owner.import.dropHint")}</p>
          <Button className="mt-5" onClick={() => fileRef.current?.click()} disabled={upload.isPending}>
            {upload.isPending ? <Loader2 className="animate-spin" /> : <Upload />} {t("owner.import.chooseFile")}
          </Button>
          <input ref={fileRef} type="file" accept=".xlsx,.csv" className="hidden" onChange={(e) => { pick(e.target.files); e.target.value = ""; }} />
        </div>
        <div className="mt-6 grid gap-4 md:grid-cols-3">
          {(["export", "check", "import"] as const).map((step) => (
            <div key={step} className="rounded-2xl border bg-card p-5">
              <p className="font-semibold">{t(`owner.import.steps.${step}.title`)}</p>
              <p className="mt-1 text-sm text-muted-foreground">{t(`owner.import.steps.${step}.body`)}</p>
            </div>
          ))}
        </div>
        <p className="mt-6 text-sm text-muted-foreground">
          {richText(t("owner.import.template"), {
            link: (
              <a href="/api/owner/import/template" className="inline-flex items-center gap-1 font-medium text-foreground underline underline-offset-4">
                <Download className="size-3.5" /> {t("owner.import.templateLink")}
              </a>
            ),
          })}
        </p>
      </>
    );
  }

  return (
    <div className="pb-24">
      <button onClick={() => setPreview(null)} className="mb-4 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> {t("owner.import.chooseDifferent")}
      </button>
      <PageTitle
        title={t("owner.import.checkTitle")}
        description={t(`owner.import.checkDescription.${plural(language, preview.total)}`, {
          count: formatNumber(preview.total),
          columns: Object.entries(preview.columns)
            .map(([k, v]) => t("owner.import.columnPair", { field: fieldName(k), header: v }))
            .join(", "),
        })}
      />
      <div className="mb-4 flex flex-wrap gap-2 text-sm">
        <Badge variant="secondary">{t(`owner.import.badges.update.${plural(language, counts.update)}`, { count: formatNumber(counts.update) })}</Badge>
        <Badge className="bg-success-soft text-success-ink">{t(`owner.import.badges.add.${plural(language, counts.add)}`, { count: formatNumber(counts.add) })}</Badge>
        {counts.skip > 0 && <Badge variant="outline">{t(`owner.import.badges.skipped.${plural(language, counts.skip)}`, { count: formatNumber(counts.skip) })}</Badge>}
        {missingPrice > 0 && <Badge className="bg-warning-soft text-warning-ink">{t(`owner.import.badges.needPrice.${plural(language, missingPrice)}`, { count: formatNumber(missingPrice) })}</Badge>}
      </div>
      <div className="overflow-x-auto rounded-2xl border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12">{t("owner.import.table.row")}</TableHead>
              <TableHead>{t("owner.import.table.item")}</TableHead>
              <TableHead>{t("owner.import.table.whatHappens")}</TableHead>
              <TableHead className="w-24 text-right">{t("owner.import.table.qty")}</TableHead>
              <TableHead className="w-28 text-right">{t("owner.import.table.price")}</TableHead>
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
                      {t("owner.import.matched", { name: r.match_name })} <span className={cn("tabular", r.score < 86 && "text-warning-ink")}>({r.score}%)</span>
                    </p>
                  )}
                  {r.issues.map((m) => (
                    <p key={m} className="flex items-center gap-1 text-xs text-warning-ink"><AlertCircle className="size-3" /> {Object.hasOwn(ISSUES, m) ? t(ISSUES[m]) : m}</p>
                  ))}
                </TableCell>
                <TableCell className="min-w-56">
                  <div className="flex flex-col gap-1.5">
                    <Select value={r.action} onValueChange={(v) => edit(i, { action: v as Action })}>
                      <SelectTrigger className="h-8 w-full" aria-label={t("owner.import.whatToDo", { name: r.name })}><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {(["update", "add_catalog", "add_new", "skip"] as Action[])
                          .filter((a) => (a === "update" ? !!r.product_id : a === "add_catalog" ? !!r.catalog_item_id && !r.product_id : true))
                          .map((a) => <SelectItem key={a} value={a}>{t(`owner.import.actions.${a}`)}</SelectItem>)}
                      </SelectContent>
                    </Select>
                    {r.action === "add_new" && (
                      <Select value={r.category_slug ?? ""} onValueChange={(v) => edit(i, { category_slug: v })}>
                        <SelectTrigger className="h-8 w-full" aria-label={t("owner.import.categoryFor", { name: r.name })}><SelectValue placeholder={t("owner.import.category")} /></SelectTrigger>
                        <SelectContent>{meta?.categories.map((c) => <SelectItem key={c.slug} value={c.slug}>{c.name}</SelectItem>)}</SelectContent>
                      </Select>
                    )}
                  </div>
                </TableCell>
                <TableCell className="text-right">
                  <Input value={r.quantity ?? ""} inputMode="numeric" placeholder="-" className="ml-auto h-8 w-20 text-right tabular" aria-label={t("owner.import.quantityFor", { name: r.name })}
                    onChange={(e) => edit(i, { quantity: e.target.value === "" ? null : Number(e.target.value.replace(/\D/g, "")) })} />
                </TableCell>
                <TableCell className="text-right">
                  <Input value={r.price ?? ""} inputMode="decimal" placeholder={t("owner.import.pricePlaceholder")} aria-label={t("owner.import.priceFor", { name: r.name })}
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
            {missingPrice
              ? t(`owner.import.needPrice.${plural(language, missingPrice)}`, { count: formatNumber(missingPrice) })
              : t("owner.import.nothingChanges")}
          </p>
          <Button className="ml-auto" disabled={missingPrice > 0 || commit.isPending || toImport === 0} onClick={() => commit.mutate()}>
            {commit.isPending && <Loader2 className="animate-spin" />} {t(`owner.import.importCount.${plural(language, toImport)}`, { count: formatNumber(toImport) })}
          </Button>
        </div>
      </div>
    </div>
  );
}
