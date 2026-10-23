"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Camera, FileSpreadsheet, Library, Minus, MoreHorizontal, Package, Pencil, Plus, Search, Trash2, Undo2 } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { Freshness, StockPill } from "@/components/common/indicators";
import { ProductThumb } from "@/components/common/product-thumb";
import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { ConfirmAction } from "@/components/fulfillment/confirm-action";
import { ProductFormDialog } from "@/components/owner/product-form-dialog";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useDebounce } from "@/hooks/use-debounce";
import { useMeta } from "@/hooks/use-session";
import { api } from "@/lib/api";
import { formatNumber, formatPrice } from "@/lib/format";
import { plural } from "@/lib/i18n";
import type { Listing, OwnerListing } from "@/lib/types";
import { cn } from "@/lib/utils";

type Page = { items: OwnerListing[]; total: number; page: number; page_size: number };
// The stock filter's values, each with its label (owner.inventory.tabs in src/messages).
const STOCK_TABS = ["all", "low_stock", "out_of_stock", "inactive"] as const;
const SORTS = ["name", "stock_asc", "stock_desc", "price_asc", "updated"] as const;

export function InventoryView() {
  const { t, language } = useI18n();
  const router = useRouter();
  const params = useSearchParams();
  const { data: meta } = useMeta();
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<OwnerListing | null>(null);
  const stock = params.get("stock") ?? "all";
  const category = params.get("category") ?? "";
  const sort = params.get("sort") ?? "name";
  const focus = Number(params.get("focus")) || null;
  const q = useDebounce(search, 250);

  const setParam = (k: string, v: string | null) => {
    const next = new URLSearchParams(params.toString());
    if (!v || v === "all") next.delete(k);
    else next.set(k, v);
    next.delete("focus");
    router.replace(`/shop/inventory?${next.toString()}`, { scroll: false });
  };

  const list = useQuery({
    queryKey: ["owner", "products", q, stock, category, sort],
    queryFn: () => api.get<Page>("/owner/products", { q, stock, category, sort, page_size: 200 }),
    placeholderData: keepPreviousData,
  });

  return (
    <>
      <PageTitle
        title={t("owner.inventory.title")}
        description={t("owner.inventory.description")}
        action={
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="outline"><Link href="/shop/import"><FileSpreadsheet /> {t("owner.inventory.importExcel")}</Link></Button>
            <Button asChild variant="outline"><Link href="/shop/catalog"><Library /> {t("owner.inventory.addFromCatalogue")}</Link></Button>
            <Button onClick={() => setCreating(true)}><Plus /> {t("owner.inventory.newProduct")}</Button>
          </div>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="relative min-w-56 flex-1">
          <Search className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder={t("owner.inventory.searchPlaceholder")} className="h-9 pl-9" />
        </div>
        <Tabs value={stock} onValueChange={(v) => setParam("stock", v)}>
          <TabsList>{STOCK_TABS.map((tab) => <TabsTrigger key={tab} value={tab}>{t(`owner.inventory.tabs.${tab}`)}</TabsTrigger>)}</TabsList>
        </Tabs>
        <Select value={category || "all"} onValueChange={(v) => setParam("category", v)}>
          <SelectTrigger className="h-9 w-40" aria-label={t("owner.inventory.category")}><SelectValue placeholder={t("owner.inventory.category")} /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t("owner.inventory.allCategories")}</SelectItem>
            {meta?.categories.map((c) => <SelectItem key={c.slug} value={c.slug}>{c.name}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={sort} onValueChange={(v) => setParam("sort", v === "name" ? null : v)}>
          <SelectTrigger className="h-9 w-40" aria-label={t("owner.inventory.sortLabel")}><SelectValue /></SelectTrigger>
          <SelectContent>
            {SORTS.map((value) => <SelectItem key={value} value={value}>{t(`owner.inventory.sort.${value}`)}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>

      {list.isError ? (
        <ErrorState error={list.error} onRetry={() => list.refetch()} />
      ) : !list.data ? (
        <ListSkeleton rows={6} />
      ) : list.data.items.length === 0 ? (
        <EmptyState icon={Package} title={q || stock !== "all" ? t("owner.inventory.noMatchTitle") : t("owner.inventory.noProductsTitle")}
          description={q || stock !== "all" ? t("owner.inventory.noMatchBody") : t("owner.inventory.noProductsBody")}
          action={<Button asChild><Link href="/shop/catalog">{t("owner.inventory.openCatalogue")}</Link></Button>} />
      ) : (
        <>
          <p className="mb-2 text-xs text-muted-foreground">
            {t(`owner.inventory.listings.${plural(language, list.data.total)}`, { count: formatNumber(list.data.total) })}
          </p>
          <ul className={cn("divide-y rounded-2xl border bg-card transition-opacity", list.isFetching && "opacity-70")}>
            {list.data.items.map((p) => (
              <InventoryRow key={p.id} p={p} focused={p.id === focus} onEdit={() => setEditing(p)} />
            ))}
          </ul>
        </>
      )}
      {creating && <ProductFormDialog open onOpenChange={setCreating} />}
      {editing && <ProductFormDialog key={editing.id} product={editing} open onOpenChange={(o) => !o && setEditing(null)} />}
    </>
  );
}

function InventoryRow({ p, focused, onEdit }: { p: OwnerListing; focused: boolean; onEdit: () => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const ref = useRef<HTMLLIElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [qty, setQty] = useState(p.quantity);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [price, setPrice] = useState(String(p.price));
  const dirtyQty = qty !== p.quantity;
  const dirtyPrice = Number(price) !== p.price && Number(price) > 0;

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- keep local edits in sync after a refetch
    setQty(p.quantity);
    setPrice(String(p.price));
  }, [p.quantity, p.price]);
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [focused]);

  const invalidate = () => qc.invalidateQueries({ queryKey: ["owner"] });
  const saveStock = useMutation({
    mutationFn: () => api.put<Listing>(`/owner/products/${p.id}/stock`, { quantity: qty }),
    onSuccess: () => {
      toast.success(t("owner.inventory.toast.stockSet", { name: p.name, count: formatNumber(qty) }));
      invalidate();
    },
  });
  const savePrice = useMutation({
    mutationFn: () => api.patch(`/owner/products/${p.id}`, { price: Number(price) }),
    onSuccess: () => {
      toast.success(t("owner.inventory.toast.priceUpdated", { price: formatPrice(Number(price)) }));
      invalidate();
    },
  });
  const remove = useMutation({
    mutationFn: () => api.delete(`/owner/products/${p.id}`),
    onSuccess: () => {
      toast.success(t("owner.inventory.toast.removed"));
      invalidate();
    },
  });
  const restore = useMutation({
    mutationFn: () => api.patch(`/owner/products/${p.id}`, { is_active: true }),
    onSuccess: () => {
      toast.success(t("owner.inventory.toast.listedAgain"));
      invalidate();
    },
  });
  const upload = useMutation({
    mutationFn: (file: File) => api.upload(`/owner/products/${p.id}/image`, file),
    onSuccess: () => {
      toast.success(t("owner.inventory.toast.photoUpdated"));
      invalidate();
    },
  });

  return (
    <li ref={ref} className={cn("flex flex-wrap items-center gap-x-4 gap-y-3 p-3 sm:flex-nowrap", focused && "bg-brand-soft/50", !p.is_active && "opacity-60")}>
      <button type="button" onClick={() => fileRef.current?.click()} className="group relative shrink-0 rounded-lg" aria-label={t("owner.inventory.uploadPhotoFor", { name: p.name })}>
        <ProductThumb icon={p.icon} imageUrl={p.image_url} thumbnailUrl={p.thumbnail_url} category={p.category} alt="" size="sm" />
        <span className="absolute inset-0 grid place-items-center rounded-lg bg-black/40 text-white opacity-0 transition-opacity group-hover:opacity-100"><Camera className="size-4" /></span>
      </button>
      <input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" className="hidden" onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])} />

      <div className="min-w-0 flex-1 basis-48">
        <p className="truncate text-sm font-medium">{p.name}</p>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1">
          <StockPill status={p.stock_status} quantity={p.quantity} exact compact />
          <Freshness at={p.stock_updated_at} />
          {p.sku && <span className="font-mono text-[11px] text-muted-foreground">{p.sku}</span>}
        </div>
      </div>

      {p.is_active && (
        <>
          <form
            className="flex items-center gap-1"
            onSubmit={(e) => {
              e.preventDefault();
              if (dirtyPrice) savePrice.mutate();
            }}
          >
            <span className="text-sm text-muted-foreground">{t("owner.inventory.rupee")}</span>
            <Input value={price} onChange={(e) => setPrice(e.target.value.replace(/[^\d.]/g, ""))} onBlur={() => dirtyPrice && savePrice.mutate()}
              inputMode="decimal" className="h-8 w-20 text-right tabular" aria-label={t("owner.inventory.priceOf", { name: p.name })} />
          </form>

          <form
            className="flex items-center gap-1"
            onSubmit={(e) => {
              e.preventDefault();
              saveStock.mutate();
            }}
          >
            <div className="flex items-center rounded-lg border">
              <Button type="button" variant="ghost" size="icon-sm" onClick={() => setQty(Math.max(0, qty - 1))} aria-label={t("owner.inventory.decrease")}><Minus /></Button>
              <Input value={qty} onChange={(e) => setQty(Math.max(0, Number(e.target.value.replace(/\D/g, "")) || 0))} inputMode="numeric"
                className="h-7 w-12 border-0 px-0 text-center tabular shadow-none focus-visible:ring-0" aria-label={t("owner.inventory.stockOf", { name: p.name })} />
              <Button type="button" variant="ghost" size="icon-sm" onClick={() => setQty(qty + 1)} aria-label={t("owner.inventory.increase")}><Plus /></Button>
            </div>
            <Button type="submit" size="sm" variant={dirtyQty ? "default" : "outline"} disabled={saveStock.isPending} className="w-20">
              {dirtyQty ? t("owner.inventory.save") : t("owner.inventory.recount")}
            </Button>
          </form>
        </>
      )}

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon-sm" aria-label={t("owner.inventory.moreActions", { name: p.name })}><MoreHorizontal /></Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={onEdit}><Pencil /> {t("owner.inventory.editDetails")}</DropdownMenuItem>
          <DropdownMenuItem onClick={() => fileRef.current?.click()}><Camera /> {t("owner.inventory.uploadPhoto")}</DropdownMenuItem>
          <DropdownMenuItem asChild><Link href={`/product/${p.id}`}>{t("owner.inventory.viewAsCustomer")}</Link></DropdownMenuItem>
          <DropdownMenuSeparator />
          {p.is_active ? (
            <DropdownMenuItem variant="destructive" onClick={() => setConfirmRemove(true)}><Trash2 /> {t("owner.inventory.removeFromShop")}</DropdownMenuItem>
          ) : (
            <DropdownMenuItem onClick={() => restore.mutate()}><Undo2 /> {t("owner.inventory.listAgain")}</DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      <ConfirmAction open={confirmRemove} onOpenChange={setConfirmRemove} title={t("owner.inventory.removeTitle", { name: p.name })}
        description={t("owner.inventory.removeBody")} confirmLabel={t("owner.inventory.remove")}
        onConfirm={() => remove.mutate()} pending={remove.isPending} />
    </li>
  );
}
