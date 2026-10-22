"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { ResponsiveDialog } from "@/components/common/responsive-dialog";
import { QuantityStepper } from "@/components/product/quantity-stepper";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { formatNumber, formatPrice } from "@/lib/format";
import type { ProductDetail, Reservation } from "@/lib/types";

export function ReserveDialog({ product, open, onOpenChange }: { product: ProductDetail; open: boolean; onOpenChange: (o: boolean) => void }) {
  const { t } = useI18n();
  const router = useRouter();
  const qc = useQueryClient();
  const [qty, setQty] = useState(1);
  const [note, setNote] = useState("");
  const max = Math.max(1, Math.min(product.quantity, 20));

  const reserve = useMutation({
    mutationFn: () => api.post<Reservation>("/reservations", { product_id: product.id, quantity: qty, note: note || null }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["product", product.id] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      toast.success(t("transactions.reserve.sent", { code: r.code, shop: product.shop.name }), {
        description: t("transactions.reserve.sentBody"),
      });
      onOpenChange(false);
      router.push(`/account/reservations/${r.id}`);
    },
  });

  return (
    <ResponsiveDialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("transactions.reserve.title")}
      description={t("transactions.reserve.description", { shop: product.shop.name })}
      footer={
        <Button size="lg" className="h-11 w-full bg-brand text-brand-foreground hover:bg-brand/90" onClick={() => reserve.mutate()} disabled={reserve.isPending}>
          {reserve.isPending ? <Loader2 className="animate-spin" /> : <Check />}
          {qty > 1
            ? t("transactions.reserve.submitMany", { count: formatNumber(qty), price: formatPrice(product.price * qty) })
            : t("transactions.reserve.submitOne", { price: formatPrice(product.price * qty) })}
        </Button>
      }
    >
      <div className="space-y-5 pb-2">
        <div className="flex items-center justify-between gap-4 rounded-xl bg-muted/60 p-3">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">{product.name}</p>
            <p className="text-xs text-muted-foreground">
              {t("transactions.reserve.eachInStock", { price: formatPrice(product.price), count: formatNumber(product.quantity) })}
            </p>
          </div>
          <QuantityStepper value={qty} onChange={setQty} max={max} />
        </div>

        <ol className="space-y-3 text-sm">
          {[
            { key: "check", t: t("transactions.reserve.steps.check.title"), d: t("transactions.reserve.steps.check.body") },
            {
              key: "held",
              t: t("transactions.reserve.steps.held.title", { minutes: formatNumber(product.shop.hold_minutes) }),
              d: t("transactions.reserve.steps.held.body"),
            },
            { key: "pay", t: t("transactions.reserve.steps.pay.title"), d: t("transactions.reserve.steps.pay.body") },
          ].map((s, i) => (
            <li key={s.key} className="flex gap-3">
              <span className="grid size-7 shrink-0 place-items-center rounded-full bg-brand-soft text-xs font-semibold text-brand-ink tabular">{i + 1}</span>
              <span>
                <span className="block font-medium">{s.t}</span>
                <span className="text-muted-foreground">{s.d}</span>
              </span>
            </li>
          ))}
        </ol>

        <div className="space-y-1.5">
          <Label htmlFor="res-note">{t("transactions.reserve.note")}</Label>
          <Textarea
            id="res-note"
            value={note}
            onChange={(e) => setNote(e.target.value.slice(0, 300))}
            placeholder={t("transactions.reserve.notePlaceholder")}
            rows={2}
          />
        </div>
      </div>
    </ResponsiveDialog>
  );
}
