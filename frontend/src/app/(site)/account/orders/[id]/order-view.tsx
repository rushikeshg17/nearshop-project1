"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Banknote, MapPin, Phone, Star, X } from "lucide-react";
import { motion, MotionConfig } from "motion/react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { ProductThumb } from "@/components/common/product-thumb";
import { ErrorState, ListSkeleton } from "@/components/common/states";
import { StatusBadge } from "@/components/common/status";
import { ConfirmAction } from "@/components/fulfillment/confirm-action";
import { PayShopByUpi } from "@/components/fulfillment/pay-shop-by-upi";
import { EventTimeline, FulfillmentProgress } from "@/components/fulfillment/progress";
import { ReviewDialog } from "@/components/fulfillment/review-dialog";
import { ShopMap } from "@/components/map/shop-map";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatDistance, formatNumber, formatPrice } from "@/lib/format";
import type { MessageKey } from "@/lib/i18n";
import type { Order } from "@/lib/types";
import { cn } from "@/lib/utils";

// The headline for each state, in the interface language.
const HEADLINE: Record<string, MessageKey> = {
  PENDING: "transactions.order.status.PENDING",
  SHOP_CONFIRMED: "transactions.order.status.SHOP_CONFIRMED",
  PREPARING: "transactions.order.status.PREPARING",
  OUT_FOR_DELIVERY: "transactions.order.status.OUT_FOR_DELIVERY",
  DELIVERED: "transactions.order.status.DELIVERED",
  CANCELLED: "transactions.order.status.CANCELLED",
  DELIVERY_FAILED: "transactions.order.status.DELIVERY_FAILED",
  RETURNED_TO_SHOP: "transactions.order.status.RETURNED_TO_SHOP",
};

// Confirmed orders not yet delivered: the customer's delivery code is shown while the order is in these states.
const AWAITING_DELIVERY: string[] = ["SHOP_CONFIRMED", "PREPARING", "OUT_FOR_DELIVERY"];

// How a paid order was paid (P6.1), in the interface language.
const PAID_BY: Record<string, MessageKey> = {
  cod: "transactions.order.paidCash",
  upi_direct: "transactions.order.paidUpi",
};

export function OrderView({ id }: { id: number }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [reviewOpen, setReviewOpen] = useState(false);
  const q = useQuery({
    queryKey: ["order", id],
    queryFn: () => api.get<Order>(`/orders/${id}`),
    refetchInterval: (query) => (["PENDING", "SHOP_CONFIRMED", "PREPARING", "OUT_FOR_DELIVERY"].includes(query.state.data?.status ?? "") ? 10_000 : false),
  });
  const cancel = useMutation({
    mutationFn: (reason: string | null) => api.post<Order>(`/orders/${id}/cancel`, { reason }),
    onSuccess: (o) => {
      qc.setQueryData(["order", id], o);
      toast.success(t("transactions.order.cancelled"));
    },
  });

  if (q.isError) return <div className="mx-auto max-w-3xl px-4 py-12"><ErrorState error={q.error} onRetry={() => q.refetch()} /></div>;
  const o = q.data;
  if (!o) return <div className="mx-auto max-w-3xl px-4 py-12"><ListSkeleton rows={3} /></div>;
  const moving = o.status === "OUT_FOR_DELIVERY";
  // The shop's UPI ID is in the customer's own order (P6.1); paying is offered once the shop has confirmed.
  const payByUpi = !!o.shop.upi_id && AWAITING_DELIVERY.includes(o.status) && o.payment_status !== "paid";

  return (
    <MotionConfig reducedMotion="user">
      <div className="mx-auto max-w-5xl px-4 pt-6 md:px-6 md:pt-10">
        <Link href="/account" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeft className="size-4" /> {t("transactions.common.yourActivity")}
        </Link>
        <div className="mt-4 grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
          <div className="space-y-6">
            <motion.section key={o.status} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
              className={cn("rounded-3xl border p-6", moving ? "border-brand/30 bg-brand-soft/50" : "bg-card")}>
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={o.status} kind="order" />
                <span className="font-mono text-xs text-muted-foreground">{o.code}</span>
              </div>
              <h1 className="mt-3 text-2xl font-semibold md:text-3xl">{HEADLINE[o.status] ? t(HEADLINE[o.status]) : o.status}</h1>
              {o.close_reason && <p className="mt-1 text-muted-foreground">{t("transactions.order.reason", { reason: o.close_reason })}</p>}
              {moving && (
                <p className="mt-1 text-muted-foreground">
                  {t(o.shop.upi_id ? "transactions.order.keepReadyUpi" : "transactions.order.keepReady", {
                    price: formatPrice(o.total),
                    shop: o.shop.name,
                  })}
                </p>
              )}
              {o.delivery_code && AWAITING_DELIVERY.includes(o.status) && (
                // Only the customer sees this code (P3.5): the shop needs it to mark the order delivered.
                <section aria-labelledby="delivery-code-label" className="mt-6 rounded-2xl border-2 border-dashed border-brand/50 bg-card p-5 text-center">
                  <p id="delivery-code-label" className="text-sm font-semibold">{t("transactions.order.deliveryCode")}</p>
                  <p className="mt-2 font-mono text-5xl font-bold tracking-[0.3em] tabular sm:text-6xl">{o.delivery_code}</p>
                  <p className="mt-2 text-xs text-muted-foreground">
                    {t("transactions.order.deliveryCodeHint", { shop: o.shop.name })}
                  </p>
                </section>
              )}
              <FulfillmentProgress item={o} className="mt-8" />
            </motion.section>

            {payByUpi && (
              <PayShopByUpi upiId={o.shop.upi_id!} payeeName={o.shop.upi_payee_name || o.shop.name} amount={o.total} note={o.code}
                cash={t("transactions.upi.cashOnDelivery")} />
            )}

            <section className="rounded-3xl border bg-card p-5">
              <ul className="divide-y">
                {o.items.map((i) => (
                  <li key={i.product_id} className="flex items-center gap-3 py-3 first:pt-0">
                    <ProductThumb icon={i.icon} imageUrl={i.image_url} thumbnailUrl={i.thumbnail_url} alt="" size="sm" />
                    <Link href={`/product/${i.product_id}`} className="min-w-0 flex-1 truncate text-sm font-medium hover:underline">{i.name}</Link>
                    <span className="text-sm text-muted-foreground tabular">
                      {t("transactions.common.quantityPrice", { count: formatNumber(i.quantity), price: formatPrice(i.unit_price) })}
                    </span>
                  </li>
                ))}
              </ul>
              <dl className="mt-3 space-y-1.5 border-t pt-3 text-sm">
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("transactions.order.items")}</dt><dd className="tabular">{formatPrice(o.subtotal)}</dd></div>
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("transactions.order.fee", { distance: formatDistance(o.distance_km) })}</dt><dd className="tabular">{o.delivery_fee ? formatPrice(o.delivery_fee) : t("transactions.order.free")}</dd></div>
                <div className="flex justify-between font-semibold"><dt className="inline-flex items-center gap-1.5"><Banknote className="size-4" /> {o.payment_status === "paid" ? t(PAID_BY[o.payment_method] ?? "transactions.order.paidOnDelivery") : o.shop.upi_id ? t("transactions.order.payOnDeliveryUpi") : t("transactions.order.payOnDelivery")}</dt><dd className="tabular">{formatPrice(o.total)}</dd></div>
              </dl>
            </section>

            <div className="flex flex-wrap gap-2">
              {o.actions.includes("cancel") && (
                <ConfirmAction title={t("transactions.order.cancelTitle")} description={t("transactions.order.cancelBody")}
                  confirmLabel={t("transactions.order.cancel")} reasonLabel={t("transactions.order.cancelReason")}
                  pending={cancel.isPending} onConfirm={(r) => cancel.mutate(r)}
                  trigger={(open) => <Button variant="outline" onClick={open}><X /> {t("transactions.order.cancel")}</Button>} />
              )}
              {o.status === "DELIVERED" && !o.reviewed && <Button onClick={() => setReviewOpen(true)}><Star /> {t("transactions.order.rate")}</Button>}
            </div>

            {o.timeline && (
              <section className="rounded-3xl border bg-card p-5">
                <h2 className="mb-4 font-semibold">{t("transactions.common.timeline")}</h2>
                <EventTimeline events={o.timeline} />
              </section>
            )}
          </div>

          <aside className="space-y-4 lg:sticky lg:top-20 lg:self-start">
            <div className="overflow-hidden rounded-3xl border bg-card">
              <div className="h-48">
                <ShopMap className="h-full" center={o.shop} interactive={false}
                  points={[
                    { id: o.shop.id, lat: o.shop.lat, lng: o.shop.lng, label: o.shop.name, title: o.shop.name },
                    { id: -1, lat: o.delivery_lat, lng: o.delivery_lng, label: t("transactions.order.mapYou"), title: t("transactions.order.mapAddress") },
                  ]} selected={-1} />
              </div>
              <div className="space-y-3 p-5 text-sm">
                <p className="flex gap-2"><MapPin className="mt-0.5 size-4 shrink-0 text-muted-foreground" /> {o.delivery_address}</p>
                <div className="flex items-center justify-between gap-2 border-t pt-3">
                  <Link href={`/shops/${o.shop.slug}`} className="font-medium hover:underline">{o.shop.name}</Link>
                  {o.shop.phone && <Button asChild size="sm" variant="outline"><a href={`tel:${o.shop.phone}`}><Phone /> {t("transactions.order.callShop")}</a></Button>}
                </div>
              </div>
            </div>
          </aside>
        </div>
        {reviewOpen && <ReviewDialog item={o} open onOpenChange={setReviewOpen} />}
      </div>
    </MotionConfig>
  );
}
