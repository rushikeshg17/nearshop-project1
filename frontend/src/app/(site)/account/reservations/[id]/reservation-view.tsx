"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Navigation, Phone, Search, Star, X } from "lucide-react";
import { motion, MotionConfig } from "motion/react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { ProductThumb } from "@/components/common/product-thumb";
import { ErrorState, ListSkeleton } from "@/components/common/states";
import { Countdown, StatusBadge } from "@/components/common/status";
import { ConfirmAction } from "@/components/fulfillment/confirm-action";
import { EventTimeline, FulfillmentProgress } from "@/components/fulfillment/progress";
import { ReviewDialog } from "@/components/fulfillment/review-dialog";
import { ShopMap } from "@/components/map/shop-map";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatNumber, formatPrice } from "@/lib/format";
import type { Translate } from "@/lib/i18n";
import type { Reservation } from "@/lib/types";
import { cn } from "@/lib/utils";

// The headline and line for each state, in the interface language (transactions.reservation.status). The shop's or
// customer's own reason, when there is one, is shown as written.
const HEADLINE: Record<string, { title: (t: Translate) => string; body: (r: Reservation, t: Translate) => string }> = {
  REQUESTED: {
    title: (t) => t("transactions.reservation.status.REQUESTED.title"),
    body: (r, t) => t("transactions.reservation.status.REQUESTED.body", { shop: r.shop.name }),
  },
  CONFIRMED: {
    title: (t) => t("transactions.reservation.status.CONFIRMED.title"),
    body: (r, t) => t("transactions.reservation.status.CONFIRMED.body", { code: r.code }),
  },
  READY_FOR_PICKUP: {
    title: (t) => t("transactions.reservation.status.READY_FOR_PICKUP.title"),
    body: (r, t) => t("transactions.reservation.status.READY_FOR_PICKUP.body", { code: r.code }),
  },
  COMPLETED: {
    title: (t) => t("transactions.reservation.status.COMPLETED.title"),
    body: (_r, t) => t("transactions.reservation.status.COMPLETED.body"),
  },
  REJECTED: {
    title: (t) => t("transactions.reservation.status.REJECTED.title"),
    body: (r, t) => r.close_reason ?? t("transactions.reservation.status.REJECTED.body"),
  },
  CANCELLED: {
    title: (t) => t("transactions.reservation.status.CANCELLED.title"),
    body: (r, t) => r.close_reason ?? t("transactions.reservation.status.CANCELLED.body"),
  },
  EXPIRED: {
    title: (t) => t("transactions.reservation.status.EXPIRED.title"),
    body: (r, t) =>
      r.confirmed_at ? t("transactions.reservation.status.EXPIRED.bodyHeld") : t("transactions.reservation.status.EXPIRED.body"),
  },
};

export function ReservationView({ id }: { id: number }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [reviewOpen, setReviewOpen] = useState(false);
  const q = useQuery({
    queryKey: ["reservation", id],
    queryFn: () => api.get<Reservation>(`/reservations/${id}`),
    refetchInterval: (query) => (["REQUESTED", "CONFIRMED", "READY_FOR_PICKUP"].includes(query.state.data?.status ?? "") ? 8_000 : false),
  });
  const cancel = useMutation({
    mutationFn: (reason: string | null) => api.post<Reservation>(`/reservations/${id}/cancel`, { reason }),
    onSuccess: (r) => {
      qc.setQueryData(["reservation", id], r);
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      toast.success(t("transactions.reservation.cancelled"));
    },
  });

  if (q.isError) return <div className="mx-auto max-w-3xl px-4 py-12"><ErrorState error={q.error} onRetry={() => q.refetch()} /></div>;
  const r = q.data;
  if (!r) return <div className="mx-auto max-w-3xl px-4 py-12"><ListSkeleton rows={3} /></div>;

  const live = ["REQUESTED", "CONFIRMED", "READY_FOR_PICKUP"].includes(r.status);
  const held = r.status === "CONFIRMED" || r.status === "READY_FOR_PICKUP";
  const head = HEADLINE[r.status];

  return (
    <MotionConfig reducedMotion="user">
      <div className="mx-auto max-w-5xl px-4 pt-6 md:px-6 md:pt-10">
        <Link href="/account" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeft className="size-4" /> {t("transactions.common.yourActivity")}
        </Link>

        <div className="mt-4 grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
          <div className="space-y-6">
            <motion.section
              key={r.status}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              className={cn("rounded-3xl border p-6", held ? "border-brand/30 bg-brand-soft/50" : "bg-card")}
            >
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <StatusBadge status={r.status} kind="reservation" />
                  <h1 className="mt-3 text-2xl font-semibold md:text-3xl">{head.title(t)}</h1>
                  <p className="mt-1 max-w-md text-muted-foreground">{head.body(r, t)}</p>
                </div>
                {live && (
                  <div className="rounded-2xl bg-card px-5 py-3 text-center shadow-soft">
                    <p className="text-xs text-muted-foreground">{r.status === "REQUESTED" ? t("transactions.reservation.respondsWithin") : t("transactions.reservation.heldFor")}</p>
                    <p className="font-heading text-3xl font-semibold"><Countdown to={r.expires_at} expiredLabel="0:00" /></p>
                  </div>
                )}
              </div>
              {held && (
                // The shop types this code to complete the pickup (P3.5), so it is large enough to read across a counter.
                <section aria-labelledby="pickup-code-label" className="mt-6 rounded-2xl border-2 border-dashed border-brand/50 bg-card p-5 text-center">
                  <p id="pickup-code-label" className="text-sm font-semibold">{t("transactions.reservation.showAtCounter")}</p>
                  <p className="mt-2 font-mono text-5xl font-bold tracking-[0.25em] tabular sm:text-6xl">
                    {r.code.replace(/^R-/, "")}
                  </p>
                  <p className="mt-2 text-xs text-muted-foreground">
                    {t("transactions.reservation.pickupCodeHint", { price: formatPrice(r.total) })}
                  </p>
                </section>
              )}
              <FulfillmentProgress item={r} className="mt-8" />
            </motion.section>

            <section className="flex items-center gap-4 rounded-3xl border bg-card p-5">
              <ProductThumb icon={r.product.icon} imageUrl={r.product.image_url} thumbnailUrl={r.product.thumbnail_url} alt="" size="lg" />
              <div className="min-w-0 flex-1">
                <Link href={`/product/${r.product.id}`} className="font-semibold hover:underline">{r.product.name}</Link>
                <p className="text-sm text-muted-foreground">
                  {t("transactions.common.quantityPrice", { count: formatNumber(r.quantity), price: formatPrice(r.unit_price) })}
                  {r.product.unit && ` · ${r.product.unit}`}
                </p>
                {r.note && <p className="mt-1 text-sm">{t("transactions.reservation.yourNote", { note: r.note })}</p>}
              </div>
              <p className="text-lg font-semibold tabular">{formatPrice(r.total)}</p>
            </section>

            <div className="flex flex-wrap gap-2">
              {r.actions.includes("cancel") && (
                <ConfirmAction
                  title={t("transactions.reservation.cancelTitle")}
                  description={t("transactions.reservation.cancelBody")}
                  confirmLabel={t("transactions.reservation.cancel")}
                  reasonLabel={t("transactions.reservation.cancelReason")}
                  reasonPlaceholder={t("transactions.reservation.cancelReasonPlaceholder")}
                  pending={cancel.isPending}
                  onConfirm={(reason) => cancel.mutate(reason)}
                  trigger={(open) => (
                    <Button variant="outline" onClick={open}><X /> {t("transactions.reservation.cancel")}</Button>
                  )}
                />
              )}
              {r.status === "COMPLETED" && !r.reviewed && (
                <Button onClick={() => setReviewOpen(true)}><Star /> {t("transactions.reservation.rate")}</Button>
              )}
              {["REJECTED", "EXPIRED", "CANCELLED"].includes(r.status) && (
                <Button asChild><Link href={`/search?q=${encodeURIComponent(r.product.name)}`}><Search /> {t("transactions.reservation.findElsewhere")}</Link></Button>
              )}
            </div>

            {r.timeline && (
              <section className="rounded-3xl border bg-card p-5">
                <h2 className="mb-4 font-semibold">{t("transactions.common.timeline")}</h2>
                <EventTimeline events={r.timeline} />
              </section>
            )}
          </div>

          <aside className="space-y-4 lg:sticky lg:top-20 lg:self-start">
            <div className="overflow-hidden rounded-3xl border bg-card">
              <div className="h-44">
                <ShopMap className="h-full" center={r.shop} zoom={15} fitToPoints={false} interactive={false}
                  points={[{ id: r.shop.id, lat: r.shop.lat, lng: r.shop.lng, label: r.shop.name, title: r.shop.name }]} selected={r.shop.id} />
              </div>
              <div className="p-5">
                <Link href={`/shops/${r.shop.slug}`} className="font-semibold hover:underline">{r.shop.name}</Link>
                <p className="mt-0.5 text-sm text-muted-foreground">{r.shop.address_line}</p>
                <p className="mt-1 text-xs text-muted-foreground">{r.shop.hours_label}</p>
                <div className="mt-4 grid grid-cols-2 gap-2">
                  <Button asChild variant="outline" size="sm">
                    <a href={`https://www.openstreetmap.org/directions?to=${r.shop.lat}%2C${r.shop.lng}`} target="_blank" rel="noreferrer"><Navigation /> {t("transactions.common.directions")}</a>
                  </Button>
                  {r.shop.phone && (
                    <Button asChild variant="outline" size="sm"><a href={`tel:${r.shop.phone}`}><Phone /> {t("transactions.common.call")}</a></Button>
                  )}
                </div>
              </div>
            </div>
          </aside>
        </div>
        {reviewOpen && <ReviewDialog item={r} open onOpenChange={setReviewOpen} />}
      </div>
    </MotionConfig>
  );
}
