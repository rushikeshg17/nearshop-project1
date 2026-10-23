"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Navigation, PackageX, Phone, Search, Star, WifiOff, X } from "lucide-react";
import { motion, MotionConfig } from "motion/react";
import Link from "next/link";
import { useEffect, useState, useSyncExternalStore } from "react";
import { toast } from "sonner";

import { ProductThumb } from "@/components/common/product-thumb";
import { ErrorState, ListSkeleton } from "@/components/common/states";
import { Countdown, StatusBadge } from "@/components/common/status";
import { ConfirmAction } from "@/components/fulfillment/confirm-action";
import { PayShopByUpi } from "@/components/fulfillment/pay-shop-by-upi";
import { EventTimeline, FulfillmentProgress } from "@/components/fulfillment/progress";
import { ReviewDialog } from "@/components/fulfillment/review-dialog";
import { ShopMap } from "@/components/map/shop-map";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import {
  forgetOfflineReservation,
  type OfflineReservation,
  readOfflineReservation,
  storeOfflineReservation,
  useMe,
} from "@/hooks/use-session";
import { api, ApiError } from "@/lib/api";
import { formatNumber, formatPrice } from "@/lib/format";
import type { Translate } from "@/lib/i18n";
import type { Reservation } from "@/lib/types";
import { cn } from "@/lib/utils";

// The close_reason of a "Not available when I arrived" report (P9.1): a code, shown in the interface language.
const NOT_AVAILABLE_ON_ARRIVAL = "not_available_on_arrival";

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
    body: (r, t) =>
      r.payment_method === "upi_direct"
        ? t("transactions.reservation.status.COMPLETED.bodyUpi")
        : r.payment_method === "cod"
          ? t("transactions.reservation.status.COMPLETED.bodyCash")
          : t("transactions.reservation.status.COMPLETED.body"),
  },
  REJECTED: {
    title: (t) => t("transactions.reservation.status.REJECTED.title"),
    body: (r, t) => r.close_reason ?? t("transactions.reservation.status.REJECTED.body"),
  },
  CANCELLED: {
    title: (t) => t("transactions.reservation.status.CANCELLED.title"),
    body: (r, t) =>
      r.close_reason === NOT_AVAILABLE_ON_ARRIVAL
        ? t("transactions.reservation.notAvailable.closedBody")
        : (r.close_reason ?? t("transactions.reservation.status.CANCELLED.body")),
  },
  EXPIRED: {
    title: (t) => t("transactions.reservation.status.EXPIRED.title"),
    body: (r, t) =>
      r.confirmed_at ? t("transactions.reservation.status.EXPIRED.bodyHeld") : t("transactions.reservation.status.EXPIRED.body"),
  },
};

function subscribeToConnectivity(onChange: () => void) {
  window.addEventListener("online", onChange);
  window.addEventListener("offline", onChange);
  return () => {
    window.removeEventListener("online", onChange);
    window.removeEventListener("offline", onChange);
  };
}

export function ReservationView({ id }: { id: number }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [reviewOpen, setReviewOpen] = useState(false);
  const { data: me } = useMe();
  const q = useQuery({
    queryKey: ["reservation", id],
    queryFn: () => api.get<Reservation>(`/reservations/${id}`),
    refetchInterval: (query) => (["REQUESTED", "CONFIRMED", "READY_FOR_PICKUP"].includes(query.state.data?.status ?? "") ? 8_000 : false),
  });
  const offline = useSyncExternalStore(subscribeToConnectivity, () => !navigator.onLine, () => false);

  // Offline codes (P7.12): the copy kept on this phone, read once when the page opens. While signed in, each load of
  // an open reservation refreshes the copy, and a closed one, or one this account can no longer see, removes it.
  const [stored, setStored] = useState<OfflineReservation | null>(null);
  useEffect(() => {
    let cancelled = false;
    readOfflineReservation(id).then((entry) => {
      if (!cancelled) setStored(entry);
    });
    return () => {
      cancelled = true;
    };
  }, [id]);
  const signedIn = !!me;
  useEffect(() => {
    if (signedIn && q.data) storeOfflineReservation(q.data);
  }, [signedIn, q.data, q.dataUpdatedAt]);
  // Signed out (401: this session was revoked, perhaps from another device), or a reservation this account can no
  // longer see (403, 404): the stored copy is forgotten, not shown.
  const gone = q.error instanceof ApiError && [401, 403, 404].includes(q.error.status);
  // The request never reached the server (ApiError status 0), as opposed to an HTTP error the server answered with.
  const unreachable = q.error instanceof ApiError && q.error.status === 0;
  useEffect(() => {
    if (gone) forgetOfflineReservation(id);
  }, [gone, id]);
  const cancel = useMutation({
    mutationFn: (reason: string | null) => api.post<Reservation>(`/reservations/${id}/cancel`, { reason }),
    onSuccess: (r) => {
      qc.setQueryData(["reservation", id], r);
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      toast.success(t("transactions.reservation.cancelled"));
    },
  });
  // "Not available when I arrived" (P9.1): once, on a held reservation. The shop is told and the listing stops
  // claiming the stock until the shop counts it again.
  const notAvailable = useMutation({
    mutationFn: () => api.post<Reservation>(`/reservations/${id}/not-available`),
    onSuccess: (r) => {
      qc.setQueryData(["reservation", id], r);
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      toast.success(t("transactions.reservation.notAvailable.done"));
    },
  });

  // Without a network, the saved copy, at once: not after the request's retries have run out. Only then: an error
  // the server answered with (a 5xx, say) shows the error and its retry, not the saved copy.
  if (!q.data && stored && !gone && (offline || unreachable)) return <OfflineReservationView r={stored} />;
  if (q.isError) return <div className="mx-auto max-w-3xl px-4 py-12"><ErrorState error={q.error} onRetry={() => q.refetch()} /></div>;
  const r = q.data;
  if (!r) return <div className="mx-auto max-w-3xl px-4 py-12"><ListSkeleton rows={3} /></div>;

  const live = ["REQUESTED", "CONFIRMED", "READY_FOR_PICKUP"].includes(r.status);
  const held = r.status === "CONFIRMED" || r.status === "READY_FOR_PICKUP";
  const head = HEADLINE[r.status];
  // The shop's UPI ID is in the customer's own reservation (P6.1); paying is offered once the item is held.
  const payByUpi = !!r.shop.upi_id && held && r.payment_status !== "paid";

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
                    {t(r.shop.upi_id ? "transactions.reservation.pickupCodeHintUpi" : "transactions.reservation.pickupCodeHint", {
                      price: formatPrice(r.total),
                    })}
                  </p>
                </section>
              )}
              <FulfillmentProgress item={r} className="mt-8" />
            </motion.section>

            {payByUpi && (
              <PayShopByUpi upiId={r.shop.upi_id!} payeeName={r.shop.upi_payee_name || r.shop.name} amount={r.total} note={r.code}
                cash={t("transactions.upi.cashAtCounter")} />
            )}

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
              {r.actions.includes("not_available") && (
                <ConfirmAction
                  title={t("transactions.reservation.notAvailable.title")}
                  description={t("transactions.reservation.notAvailable.body")}
                  confirmLabel={t("transactions.reservation.notAvailable.confirm")}
                  pending={notAvailable.isPending}
                  onConfirm={() => notAvailable.mutate()}
                  trigger={(open) => (
                    <Button variant="outline" onClick={open}><PackageX /> {t("transactions.reservation.notAvailable.action")}</Button>
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

/** The reservation as saved on this phone, shown without a network (P7.12): the code, the item, the shop and when
 * it was saved, with a note that it may be out of date. */
function OfflineReservationView({ r }: { r: OfflineReservation }) {
  const { t, info } = useI18n();
  const savedAt = new Intl.DateTimeFormat(info.htmlLang, { dateStyle: "medium", timeStyle: "short" }).format(new Date(r.saved_at));
  return (
    <div className="mx-auto max-w-3xl px-4 pt-6 md:px-6 md:pt-10">
      <Link href="/account" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> {t("transactions.common.yourActivity")}
      </Link>
      <section aria-labelledby="offline-code-title" className="mt-4 rounded-3xl border border-brand/30 bg-brand-soft/50 p-6">
        <StatusBadge status={r.status} kind="reservation" />
        <h1 id="offline-code-title" className="mt-3 text-2xl font-semibold md:text-3xl">{t("shell.offline.savedCodeTitle")}</h1>
        <section aria-labelledby="offline-code-label" className="mt-6 rounded-2xl border-2 border-dashed border-brand/50 bg-card p-5 text-center">
          <p id="offline-code-label" className="text-sm font-semibold">{t("transactions.reservation.showAtCounter")}</p>
          <p className="mt-2 font-mono text-5xl font-bold tracking-[0.25em] tabular sm:text-6xl" data-testid="offline-code">
            {r.code.replace(/^R-/, "")}
          </p>
          <p className="mt-2 text-sm">{r.product.name} × {formatNumber(r.quantity)}</p>
        </section>
        <div className="mt-6">
          <p className="font-semibold">{r.shop.name}</p>
          <p className="mt-0.5 text-sm text-muted-foreground">{r.shop.address_line}</p>
          <p className="mt-1 text-xs text-muted-foreground">{r.shop.hours_label}</p>
        </div>
        <p className="mt-6 flex items-center gap-2 text-sm font-medium" data-testid="offline-saved-at">
          <WifiOff className="size-4 shrink-0" aria-hidden />
          {t("shell.offline.savedAt", { time: savedAt })}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">{t("shell.offline.mayBeOutdated")}</p>
      </section>
    </div>
  );
}
