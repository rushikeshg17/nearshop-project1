"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Banknote, MapPin, Phone, Smartphone, Star, X } from "lucide-react";
import { motion, MotionConfig } from "motion/react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { ProductThumb } from "@/components/common/product-thumb";
import { ErrorState, ListSkeleton } from "@/components/common/states";
import { StatusBadge } from "@/components/common/status";
import { ConfirmAction } from "@/components/fulfillment/confirm-action";
import { EventTimeline, FulfillmentProgress } from "@/components/fulfillment/progress";
import { ReviewDialog } from "@/components/fulfillment/review-dialog";
import { ShopMap } from "@/components/map/shop-map";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatDistance, formatPrice } from "@/lib/format";
import type { Order } from "@/lib/types";
import { REFUND_POLICY_HREF, UPI_REFUND_DAYS, upiPaymentLink, useUpiQr } from "@/lib/upi";
import { cn } from "@/lib/utils";

const HEADLINE: Record<string, string> = {
  PENDING: "Waiting for the shop to confirm",
  SHOP_CONFIRMED: "Confirmed, items set aside",
  PREPARING: "Packing your order",
  OUT_FOR_DELIVERY: "On the way to you",
  DELIVERED: "Delivered",
  CANCELLED: "Order cancelled",
  DELIVERY_FAILED: "Delivery attempt failed",
  RETURNED_TO_SHOP: "Returned to the shop",
};

// Confirmed orders not yet delivered: the customer's delivery code is shown while the order is in these states.
const AWAITING_DELIVERY: string[] = ["SHOP_CONFIRMED", "PREPARING", "OUT_FOR_DELIVERY"];

/**
 * "Pay the shop by UPI" (P6.2, plan section 8.8): a upi://pay link to the shop's own UPI ID, with the amount and the
 * order or reservation code as the note, and the same link as a QR code made in the browser, for paying from another
 * phone. The money goes to the shop directly; NearShop never holds it. The reservation page shows it too.
 */
export function PayShopByUpi({ upiId, payeeName, amount, note, cash }: {
  upiId: string;
  payeeName: string;
  amount: number;
  note: string;
  /** How cash works instead, for example "Or pay cash when it arrives." */
  cash: string;
}) {
  const link = upiPaymentLink({ upiId, payeeName, amount, note });
  const qr = useUpiQr(link);
  return (
    <section aria-labelledby="pay-upi-title" className="rounded-3xl border bg-card p-5">
      <div className="flex items-start gap-3">
        <Smartphone className="mt-0.5 size-5 shrink-0 text-primary" />
        <div>
          <h2 id="pay-upi-title" className="font-semibold">Pay the shop by UPI</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Pay {payeeName} {formatPrice(amount)} directly with any UPI app. The money goes straight to the shop: NearShop
            never holds it. {cash}
          </p>
        </div>
      </div>
      <div className="mt-4 flex flex-col items-center gap-4 sm:flex-row sm:items-start">
        <div className="shrink-0 rounded-xl border bg-white p-2">
          {qr ? (
            <svg
              role="img"
              aria-label={`QR code to pay ${payeeName} ${formatPrice(amount)} by UPI`}
              viewBox={`-4 -4 ${qr.size + 8} ${qr.size + 8}`}
              shapeRendering="crispEdges"
              className="size-40"
            >
              <path d={qr.path} fill="#000" />
            </svg>
          ) : (
            <div aria-hidden className="size-40 animate-pulse rounded-lg bg-neutral-200" />
          )}
        </div>
        <div className="w-full min-w-0 space-y-3 text-sm">
          <Button asChild className="h-11 w-full sm:w-auto">
            <a href={link}><Smartphone /> Pay {formatPrice(amount)} by UPI</a>
          </Button>
          <p className="text-muted-foreground">On a computer or another phone? Scan the QR code with any UPI app.</p>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
            <dt className="text-muted-foreground">UPI ID</dt>
            <dd className="break-all font-mono">{upiId}</dd>
            <dt className="text-muted-foreground">Note</dt>
            <dd className="font-mono">{note}</dd>
          </dl>
          <p className="text-xs text-muted-foreground">
            Show the shop the payment confirmation. If it is cancelled after you pay, the shop refunds you within{" "}
            {UPI_REFUND_DAYS} days. <Link href={REFUND_POLICY_HREF} className="font-medium text-foreground underline underline-offset-4">Cancellation and refunds</Link>
          </p>
        </div>
      </div>
    </section>
  );
}

const PAID_BY: Record<string, string> = { cod: "Paid in cash", upi_direct: "Paid to the shop by UPI" };

export function OrderView({ id }: { id: number }) {
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
      toast.success("Order cancelled");
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
          <ArrowLeft className="size-4" /> Your activity
        </Link>
        <div className="mt-4 grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
          <div className="space-y-6">
            <motion.section key={o.status} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
              className={cn("rounded-3xl border p-6", moving ? "border-brand/30 bg-brand-soft/50" : "bg-card")}>
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={o.status} kind="order" />
                <span className="font-mono text-xs text-muted-foreground">{o.code}</span>
              </div>
              <h1 className="mt-3 text-2xl font-semibold md:text-3xl">{HEADLINE[o.status]}</h1>
              {o.close_reason && <p className="mt-1 text-muted-foreground">Reason: {o.close_reason}</p>}
              {moving && <p className="mt-1 text-muted-foreground">Keep {formatPrice(o.total)} ready{o.shop.upi_id ? " in cash, or pay by UPI" : ""}. {o.shop.name} is delivering it themselves.</p>}
              {o.delivery_code && AWAITING_DELIVERY.includes(o.status) && (
                // Only the customer sees this code (P3.5): the shop needs it to mark the order delivered.
                <section aria-labelledby="delivery-code-label" className="mt-6 rounded-2xl border-2 border-dashed border-brand/50 bg-card p-5 text-center">
                  <p id="delivery-code-label" className="text-sm font-semibold">Your delivery code</p>
                  <p className="mt-2 font-mono text-5xl font-bold tracking-[0.3em] tabular sm:text-6xl">{o.delivery_code}</p>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Give it to the person from {o.shop.name} only when the order is in your hands. Never share it by phone or message.
                  </p>
                </section>
              )}
              <FulfillmentProgress item={o} className="mt-8" />
            </motion.section>

            {payByUpi && (
              <PayShopByUpi upiId={o.shop.upi_id!} payeeName={o.shop.upi_payee_name || o.shop.name} amount={o.total} note={o.code}
                cash="Or pay cash when it arrives." />
            )}

            <section className="rounded-3xl border bg-card p-5">
              <ul className="divide-y">
                {o.items.map((i) => (
                  <li key={i.product_id} className="flex items-center gap-3 py-3 first:pt-0">
                    <ProductThumb icon={i.icon} imageUrl={i.image_url} thumbnailUrl={i.thumbnail_url} alt="" size="sm" />
                    <Link href={`/product/${i.product_id}`} className="min-w-0 flex-1 truncate text-sm font-medium hover:underline">{i.name}</Link>
                    <span className="text-sm text-muted-foreground tabular">{i.quantity} x {formatPrice(i.unit_price)}</span>
                  </li>
                ))}
              </ul>
              <dl className="mt-3 space-y-1.5 border-t pt-3 text-sm">
                <div className="flex justify-between"><dt className="text-muted-foreground">Items</dt><dd className="tabular">{formatPrice(o.subtotal)}</dd></div>
                <div className="flex justify-between"><dt className="text-muted-foreground">Delivery ({formatDistance(o.distance_km)})</dt><dd className="tabular">{o.delivery_fee ? formatPrice(o.delivery_fee) : "Free"}</dd></div>
                <div className="flex justify-between font-semibold"><dt className="inline-flex items-center gap-1.5"><Banknote className="size-4" /> {o.payment_status === "paid" ? (PAID_BY[o.payment_method] ?? "Paid on delivery") : o.shop.upi_id ? "Pay on delivery: cash or UPI" : "Pay on delivery"}</dt><dd className="tabular">{formatPrice(o.total)}</dd></div>
              </dl>
            </section>

            <div className="flex flex-wrap gap-2">
              {o.actions.includes("cancel") && (
                <ConfirmAction title="Cancel this order?" description="The shop will be notified straight away." confirmLabel="Cancel order"
                  reasonLabel="Reason (optional)" pending={cancel.isPending} onConfirm={(r) => cancel.mutate(r)}
                  trigger={(open) => <Button variant="outline" onClick={open}><X /> Cancel order</Button>} />
              )}
              {o.status === "DELIVERED" && !o.reviewed && <Button onClick={() => setReviewOpen(true)}><Star /> Rate this delivery</Button>}
            </div>

            {o.timeline && (
              <section className="rounded-3xl border bg-card p-5">
                <h2 className="mb-4 font-semibold">Timeline</h2>
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
                    { id: -1, lat: o.delivery_lat, lng: o.delivery_lng, label: "You", title: "Delivery address" },
                  ]} selected={-1} />
              </div>
              <div className="space-y-3 p-5 text-sm">
                <p className="flex gap-2"><MapPin className="mt-0.5 size-4 shrink-0 text-muted-foreground" /> {o.delivery_address}</p>
                <div className="flex items-center justify-between gap-2 border-t pt-3">
                  <Link href={`/shops/${o.shop.slug}`} className="font-medium hover:underline">{o.shop.name}</Link>
                  {o.shop.phone && <Button asChild size="sm" variant="outline"><a href={`tel:${o.shop.phone}`}><Phone /> Call shop</a></Button>}
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
