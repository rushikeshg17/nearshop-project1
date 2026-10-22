"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bike, Loader2, MapPin, ReceiptText } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { ResponsiveDialog } from "@/components/common/responsive-dialog";
import { LocationPicker } from "@/components/site/location-picker";
import { QuantityStepper } from "@/components/product/quantity-stepper";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useMe } from "@/hooks/use-session";
import { useDebounce } from "@/hooks/use-debounce";
import { api } from "@/lib/api";
import { formatDistance, formatNumber, formatPrice } from "@/lib/format";
import { useLocation } from "@/lib/location";
import type { DeliveryQuote, Order, ProductDetail } from "@/lib/types";
import { REFUND_POLICY_HREF, UPI_REFUND_DAYS } from "@/lib/upi";

export function DeliveryDialog({ product, open, onOpenChange }: { product: ProductDetail; open: boolean; onOpenChange: (o: boolean) => void }) {
  const { t } = useI18n();
  const router = useRouter();
  const qc = useQueryClient();
  const { data: me } = useMe();
  const { location } = useLocation();
  const [qty, setQty] = useState(1);
  const [address, setAddress] = useState(location?.source === "locality" ? `, ${location.label}` : "");
  const [phone, setPhone] = useState(me?.phone ?? "");
  const [note, setNote] = useState("");
  const dq = useDebounce(qty, 250);

  const quote = useQuery({
    queryKey: ["quote", product.id, dq, location?.lat, location?.lng],
    queryFn: () =>
      api.post<DeliveryQuote>("/orders/quote", { items: [{ product_id: product.id, quantity: dq }], lat: location!.lat, lng: location!.lng }),
    enabled: open && !!location,
  });

  const place = useMutation({
    mutationFn: () =>
      api.post<Order>("/orders", {
        items: [{ product_id: product.id, quantity: qty }],
        lat: location!.lat,
        lng: location!.lng,
        address,
        phone,
        note: note || null,
      }),
    onSuccess: (o) => {
      qc.invalidateQueries({ queryKey: ["product", product.id] });
      toast.success(t("transactions.delivery.sent", { code: o.code, shop: product.shop.name }), {
        description: t("transactions.delivery.sentBody"),
      });
      onOpenChange(false);
      router.push(`/account/orders/${o.id}`);
    },
  });

  const q = quote.data;
  const phoneOk = /^[6-9]\d{9}$/.test(phone);
  const canSubmit = q?.eligible && address.trim().length >= 8 && phoneOk && !place.isPending;

  return (
    <ResponsiveDialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("transactions.delivery.title")}
      description={t("transactions.delivery.description", { shop: product.shop.name })}
      footer={
        <Button size="lg" className="h-11 w-full" disabled={!canSubmit} onClick={() => place.mutate()}>
          {place.isPending ? <Loader2 className="animate-spin" /> : <Bike />}
          {q?.eligible && q.subtotal !== undefined
            ? t("transactions.delivery.placeOrderTotal", { price: formatPrice((q.subtotal ?? 0) + (q.delivery_fee ?? 0)) })
            : t("transactions.delivery.placeOrder")}
        </Button>
      }
    >
      <div className="space-y-4 pb-2">
        <div className="flex items-center justify-between gap-4 rounded-xl bg-muted/60 p-3">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">{product.name}</p>
            <p className="text-xs text-muted-foreground">{t("transactions.delivery.each", { price: formatPrice(product.price) })}</p>
          </div>
          <QuantityStepper value={qty} onChange={setQty} max={Math.max(1, Math.min(product.quantity, 20))} />
        </div>

        <div className="space-y-1.5">
          <Label className="flex items-center gap-1.5"><MapPin className="size-3.5" /> {t("transactions.delivery.deliverNear")}</Label>
          <LocationPicker className="w-full justify-start" />
        </div>

        {quote.isLoading ? (
          <div className="h-16 animate-pulse rounded-xl bg-muted" />
        ) : q && !q.eligible ? (
          <p className="rounded-xl bg-warning-soft p-3 text-sm text-warning-ink">{t("transactions.delivery.ineligible", { reason: q.reason ?? "" })}</p>
        ) : q ? (
          <dl className="space-y-1.5 rounded-xl border p-3 text-sm">
            <div className="flex justify-between"><dt className="text-muted-foreground">{t("transactions.delivery.items")}</dt><dd className="tabular">{formatPrice(q.subtotal ?? 0)}</dd></div>
            <div className="flex flex-wrap justify-between">
              <dt className="text-muted-foreground">{t("transactions.delivery.fee", { distance: formatDistance(q.distance_km) })}</dt>
              <dd className="tabular">{q.delivery_fee ? formatPrice(q.delivery_fee) : t("transactions.delivery.free")}</dd>
              {/* A dd, not a p: a list of terms may hold only dt and dd (axe: definition-list; P2.10). */}
              {q.free_delivery_above && (q.delivery_fee ?? 0) > 0 && (
                <dd className="mt-1.5 w-full text-xs text-muted-foreground">{t("transactions.delivery.freeAbove", { price: formatPrice(q.free_delivery_above) })}</dd>
              )}
            </div>
            <div className="flex justify-between border-t pt-1.5 font-semibold">
              <dt>{t("transactions.delivery.payOnDelivery")}</dt>
              <dd className="tabular">{formatPrice((q.subtotal ?? 0) + (q.delivery_fee ?? 0))}</dd>
            </div>
          </dl>
        ) : null}

        <div className="space-y-1.5">
          <Label htmlFor="addr">{t("transactions.delivery.address")}</Label>
          <Textarea
            id="addr"
            rows={2}
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder={t("transactions.delivery.addressPlaceholder")}
          />
        </div>
        <div className="grid grid-cols-[1fr_1.4fr] gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="phone">{t("transactions.delivery.phone")}</Label>
            <Input
              id="phone"
              inputMode="numeric"
              value={phone}
              onChange={(e) => setPhone(e.target.value.replace(/\D/g, "").slice(0, 10))}
              aria-invalid={phone.length > 0 && !phoneOk}
              placeholder={t("transactions.delivery.phonePlaceholder")}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="dnote">{t("transactions.delivery.note")}</Label>
            <Input
              id="dnote"
              value={note}
              onChange={(e) => setNote(e.target.value.slice(0, 300))}
              placeholder={t("transactions.delivery.notePlaceholder")}
            />
          </div>
        </div>

        {/* Shown before the order is placed (plan check 9.5). */}
        <section aria-labelledby="delivery-policy-title" className="flex gap-2.5 rounded-xl border p-3 text-xs text-muted-foreground">
          <ReceiptText className="mt-0.5 size-4 shrink-0" />
          <div>
            <h3 id="delivery-policy-title" className="text-sm font-medium text-foreground">{t("transactions.policy.title")}</h3>
            <p className="mt-0.5">
              {t("transactions.policy.deliveryBody", { days: formatNumber(UPI_REFUND_DAYS) })}{" "}
              <Link href={REFUND_POLICY_HREF} target="_blank" className="font-medium text-foreground underline underline-offset-4">
                {t("transactions.policy.link")}
              </Link>
            </p>
          </div>
        </section>
      </div>
    </ResponsiveDialog>
  );
}
