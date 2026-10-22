"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { ResponsiveDialog } from "@/components/common/responsive-dialog";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useMeta } from "@/hooks/use-session";
import { api } from "@/lib/api";
import type { OwnerListing } from "@/lib/types";

/** Create a custom product, or edit any listing's details (stock is edited inline in the table). */
export function ProductFormDialog({ product, open, onOpenChange }: { product?: OwnerListing; open: boolean; onOpenChange: (o: boolean) => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const { data: meta } = useMeta();
  const editing = !!product;
  const [f, setF] = useState(() => ({
    name: product?.name ?? "",
    brand: product?.brand ?? "",
    category_slug: product?.category ?? "",
    unit: product?.unit ?? "",
    sku: product?.sku ?? "",
    price: product ? String(product.price) : "",
    mrp: product?.mrp ? String(product.mrp) : "",
    quantity: "1",
    low_stock_threshold: String(product?.low_stock_threshold ?? 5),
    description: product?.description ?? "",
    tags: "",
  }));
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setF((s) => ({ ...s, [k]: e.target.value }));
  const num = (v: string) => (v.trim() === "" ? null : Number(v));

  const save = useMutation({
    mutationFn: () => {
      const tags = f.tags.split(",").map((tag) => tag.trim()).filter(Boolean);
      const common = {
        name: f.name.trim(),
        brand: f.brand.trim() || null,
        category_slug: f.category_slug,
        unit: f.unit.trim() || null,
        sku: f.sku.trim() || null,
        price: Number(f.price),
        mrp: num(f.mrp),
        low_stock_threshold: Number(f.low_stock_threshold) || 0,
        description: f.description.trim() || null,
      };
      return editing
        ? api.patch(`/owner/products/${product.id}`, { ...common, ...(tags.length ? { tags } : {}) })
        : api.post("/owner/products", { ...common, tags, specs: {}, quantity: Number(f.quantity) || 0 });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["owner"] });
      toast.success(editing ? t("owner.productForm.toast.updated") : t("owner.productForm.toast.added"));
      onOpenChange(false);
    },
  });

  const valid = f.name.trim().length >= 2 && f.category_slug && Number(f.price) > 0;

  return (
    <ResponsiveDialog
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? t("owner.productForm.editTitle") : t("owner.productForm.addTitle")}
      description={editing ? undefined : t("owner.productForm.addDescription")}
      footer={
        <Button className="w-full sm:w-auto" disabled={!valid || save.isPending} onClick={() => save.mutate()}>
          {save.isPending && <Loader2 className="animate-spin" />} {editing ? t("owner.productForm.saveChanges") : t("owner.productForm.addProduct")}
        </Button>
      }
    >
      <div className="grid max-h-[60vh] gap-3 overflow-y-auto pr-1 pb-2 sm:grid-cols-2">
        <div className="space-y-1.5 sm:col-span-2">
          <Label htmlFor="pf-name">{t("owner.productForm.name")}</Label>
          <Input id="pf-name" value={f.name} onChange={set("name")} placeholder={t("owner.productForm.namePlaceholder")} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="pf-brand">{t("owner.productForm.brand")}</Label>
          <Input id="pf-brand" value={f.brand} onChange={set("brand")} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="pf-category">{t("owner.productForm.category")}</Label>
          <Select value={f.category_slug} onValueChange={(v) => setF((s) => ({ ...s, category_slug: v }))}>
            <SelectTrigger id="pf-category" className="w-full"><SelectValue placeholder={t("owner.productForm.choose")} /></SelectTrigger>
            <SelectContent>{meta?.categories.map((c) => <SelectItem key={c.slug} value={c.slug}>{c.name}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="pf-price">{t("owner.productForm.price")}</Label>
          <Input id="pf-price" inputMode="decimal" value={f.price} onChange={set("price")} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="pf-mrp">{t("owner.productForm.mrp")}</Label>
          <Input id="pf-mrp" inputMode="decimal" value={f.mrp} onChange={set("mrp")} />
        </div>
        {!editing && (
          <div className="space-y-1.5">
            <Label htmlFor="pf-qty">{t("owner.productForm.quantity")}</Label>
            <Input id="pf-qty" inputMode="numeric" value={f.quantity} onChange={set("quantity")} />
          </div>
        )}
        <div className="space-y-1.5">
          <Label htmlFor="pf-low">{t("owner.productForm.lowStock")}</Label>
          <Input id="pf-low" inputMode="numeric" value={f.low_stock_threshold} onChange={set("low_stock_threshold")} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="pf-unit">{t("owner.productForm.unit")}</Label>
          <Input id="pf-unit" value={f.unit} onChange={set("unit")} placeholder={t("owner.productForm.unitPlaceholder")} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="pf-sku">{t("owner.productForm.sku")}</Label>
          <Input id="pf-sku" value={f.sku} onChange={set("sku")} />
        </div>
        <div className="space-y-1.5 sm:col-span-2">
          <Label htmlFor="pf-tags">{t("owner.productForm.tags")} {editing && <span className="font-normal text-muted-foreground">{t("owner.productForm.tagsReplaces")}</span>}</Label>
          <Input id="pf-tags" value={f.tags} onChange={set("tags")} placeholder={t("owner.productForm.tagsPlaceholder")} />
        </div>
        <div className="space-y-1.5 sm:col-span-2">
          <Label htmlFor="pf-desc">{t("owner.productForm.description")}</Label>
          <Textarea id="pf-desc" rows={2} value={f.description} onChange={set("description")} />
        </div>
      </div>
    </ResponsiveDialog>
  );
}
