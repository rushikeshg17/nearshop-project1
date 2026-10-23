"use client";

import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  Bike,
  ChevronRight,
  Footprints,
  Loader2,
  MapPin,
  Navigation,
  PackageX,
  Phone,
  Sparkles,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { lazy, Suspense, useEffect, useState } from "react";

import { Distance, Freshness, OpenBadge, Price, Rating, StockPill, VerifiedMark } from "@/components/common/indicators";
import { ProductThumb } from "@/components/common/product-thumb";
import { EmptyState, ErrorState } from "@/components/common/states";
import { StatusBadge } from "@/components/common/status";
import { ShopMap } from "@/components/map/shop-map";
import { useI18n } from "@/components/providers";
import { ReviewList } from "@/components/shop/review-list";
import { ShopTrust } from "@/components/shop/shop-trust";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useMe } from "@/hooks/use-session";
import { api, ApiError, recordLead } from "@/lib/api";
import { formatDistance, formatNumber, formatPrice, travelTime, weekdayName } from "@/lib/format";
import { plural } from "@/lib/i18n";
import { useLocation } from "@/lib/location";
import type { ProductDetail, Recommendation } from "@/lib/types";
import { cn } from "@/lib/utils";

// The reserve and delivery dialogs (Radix dialog, vaul drawer) are not first-load JavaScript (P2.9). Their code
// starts loading when the pointer or keyboard focus reaches a button that opens one (or the finger touches it),
// and, for a signed-in customer, once the page has loaded and the browser is idle. A tap before it has arrived
// shows a spinner over the page at once (DialogLoading) until the dialog opens.
const loadReserveDialog = () => import("@/components/product/reserve-dialog");
const loadDeliveryDialog = () => import("@/components/product/delivery-dialog");
const ReserveDialog = lazy(() => loadReserveDialog().then((m) => ({ default: m.ReserveDialog })));
const DeliveryDialog = lazy(() => loadDeliveryDialog().then((m) => ({ default: m.DeliveryDialog })));
const preloadReserve = { onPointerEnter: () => void loadReserveDialog(), onPointerDown: () => void loadReserveDialog(), onFocus: () => void loadReserveDialog() };
const preloadDelivery = { onPointerEnter: () => void loadDeliveryDialog(), onPointerDown: () => void loadDeliveryDialog(), onFocus: () => void loadDeliveryDialog() };

/** A call-only listing (P9.8, plan section 8.17): nobody has confirmed its stock for over 14 days (from 1 Jun 2027), so
 * it shows no stock band and no Reserve or Delivery button, only "Call the shop to check" with the shop's Call link. */
const isCallOnly = (o: object) => (o as { call_only?: boolean }).call_only === true;

/** Feedback for the first tap on slow 4G while a dialog's code is still on its way; it also stops repeated taps. */
function DialogLoading() {
  const { t } = useI18n();
  return (
    <div role="status" className="fixed inset-0 z-50 grid place-items-center bg-black/30">
      <span className="inline-flex items-center gap-2 rounded-full bg-card px-4 py-2 text-sm font-medium shadow-soft">
        <Loader2 className="size-4 animate-spin text-brand" aria-hidden /> {t("discovery.product.opening")}
      </span>
    </div>
  );
}

/**
 * The product page's content. `initial` is the listing as the server rendered it (page.tsx): the same for every
 * visitor, without location or session. Once the location is known, the listing is fetched once more in the
 * browser for live stock, the distance, the delivery quote and the visitor's own reservation, and nearby
 * alternatives, recommendations and the map appear. `initial` is null only when the server could not reach the
 * API; the page then loads entirely in the browser, as it did before server rendering.
 */
export function ProductView({ id, initial }: { id: number; initial: ProductDetail | null }) {
  const { t, language } = useI18n();
  const router = useRouter();
  const pathname = usePathname();
  const { location, ready } = useLocation();
  const { data: me } = useMe();
  const [reserveOpen, setReserveOpen] = useState(false);
  const [deliveryOpen, setDeliveryOpen] = useState(false);
  // A dialog mounts on first use and then stays mounted, so it can animate closed.
  const [dialogsUsed, setDialogsUsed] = useState({ reserve: false, delivery: false });
  const openReserve = () => {
    setDialogsUsed((u) => ({ ...u, reserve: true }));
    setReserveOpen(true);
  };
  const openDelivery = () => {
    setDialogsUsed((u) => ({ ...u, delivery: true }));
    setDeliveryOpen(true);
  };
  // A signed-in customer is likely to reserve: fetch the reserve dialog once the page has loaded and is idle.
  const signedInCustomer = me?.role === "customer";
  useEffect(() => {
    if (!signedInCustomer) return;
    let idleId: number | undefined;
    const idle = () => {
      if ("requestIdleCallback" in window) idleId = window.requestIdleCallback(() => void loadReserveDialog(), { timeout: 3000 });
      else void loadReserveDialog();
    };
    if (document.readyState === "complete") idle();
    else window.addEventListener("load", idle, { once: true });
    return () => {
      window.removeEventListener("load", idle);
      if (idleId !== undefined) window.cancelIdleCallback(idleId);
    };
  }, [signedInCustomer]);

  const q = useQuery({
    queryKey: ["product", id, location?.lat, location?.lng],
    queryFn: () => api.get<ProductDetail>(`/products/${id}`, { lat: location?.lat, lng: location?.lng }),
    enabled: ready,
  });
  const recs = useQuery({
    queryKey: ["product-recs", id, location?.lat, location?.lng],
    queryFn: () => api.get<{ source: "apriori" | "popular"; items: Recommendation[] }>(`/products/${id}/recommendations`, { lat: location?.lat, lng: location?.lng }),
    enabled: ready,
  });

  // Server data until the browser's own fetch arrives. The title comes from the page's metadata (page.tsx).
  const live = q.data;
  const p = live ?? initial;

  function requireCustomer(open: () => void) {
    if (!me) {
      router.push(`/login?next=${encodeURIComponent(pathname)}`);
      return;
    }
    open();
  }

  const gone = q.error instanceof ApiError && q.error.status === 404;
  if (q.isError && (!p || gone)) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16">
        {gone ? (
          <EmptyState
            icon={PackageX}
            title={t("discovery.product.goneTitle")}
            description={t("discovery.product.goneBody")}
            action={
              <Button asChild>
                <Link href="/search">{t("discovery.product.searchNearby")}</Link>
              </Button>
            }
          />
        ) : (
          <ErrorState error={q.error} onRetry={() => q.refetch()} />
        )}
      </div>
    );
  }
  if (!p) return <ProductSkeleton />;

  const out = p.quantity <= 0;
  const callOnly = isCallOnly(p);
  const isCustomer = !me || me.role === "customer";
  const insight = p.price_insight;
  const shop = p.shop;
  const delivery = p.delivery_quote;
  const travel = travelTime(shop.distance_km);

  return (
    <div className="mx-auto max-w-7xl px-4 pt-4 md:px-6 md:pt-8">
      <nav aria-label={t("discovery.product.breadcrumb")} className="mb-5 flex items-center gap-1 text-sm text-muted-foreground">
        <Link href={`/search?category=${p.category_detail.slug}`} className="hover:text-foreground">{p.category_detail.name}</Link>
        {p.subcategory && (
          <>
            <ChevronRight className="size-3.5" />
            <Link href={`/search?q=${encodeURIComponent(p.subcategory.replace(/-/g, " "))}`} className="capitalize hover:text-foreground">
              {p.subcategory.replace(/-/g, " ")}
            </Link>
          </>
        )}
      </nav>

      <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_360px] lg:gap-10 xl:grid-cols-[minmax(0,1fr)_400px] xl:gap-14">
        {/* ---------------------------------------------------------- left: item */}
        <div className="min-w-0 space-y-10">
          <div className="grid gap-6 sm:grid-cols-[200px_minmax(0,1fr)] sm:items-start xl:grid-cols-[260px_minmax(0,1fr)]">
            {/* No entrance fade: the server HTML would carry opacity 0, hiding the photo until scripts run. */}
            <div>
              <ProductThumb icon={p.icon} imageUrl={p.image_url} hue={p.category_detail.color_hue} alt={p.name} size="xl" className="max-sm:mx-auto max-sm:max-w-56" />
            </div>
            <div>
              {p.brand && <p className="text-sm font-medium tracking-wide text-muted-foreground uppercase">{p.brand}</p>}
              <h1 className="mt-1 text-2xl leading-tight font-semibold md:text-3xl">{p.name}</h1>
              {p.unit && <p className="mt-1 text-sm text-muted-foreground">{p.unit}</p>}
              {p.description && <p className="mt-4 text-[15px] leading-relaxed text-muted-foreground">{p.description}</p>}
              {Object.keys(p.specs).length > 0 && (
                <dl className="mt-5 grid grid-cols-1 gap-x-6 gap-y-2 text-sm 2xl:grid-cols-2">
                  {Object.entries(p.specs).map(([k, v]) => (
                    <div key={k} className="flex justify-between gap-4 border-b border-dashed pb-2">
                      <dt className="text-muted-foreground">{k}</dt>
                      <dd className="text-right font-medium">{v}</dd>
                    </div>
                  ))}
                </dl>
              )}
            </div>
          </div>

          {/* compare */}
          <section id="compare">
            <h2 className="text-xl font-semibold">{t("discovery.product.compareTitle")}</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {live &&
                (live.other_offers.length
                  ? t(`discovery.product.shopsList.${plural(language, live.other_offers.length + 1)}`, {
                      count: formatNumber(live.other_offers.length + 1),
                    })
                  : t("discovery.product.onlyThisShop"))}
              {insight?.median && ` ${t("discovery.product.typicalPrice", { price: formatPrice(insight.median) })}`}
            </p>
            {/* Nearby alternatives are sorted by distance from the visitor, so only the browser's fetch has them. */}
            {!live && !q.isError && <Skeleton className="mt-4 h-32 rounded-2xl" />}
            {live && live.other_offers.length > 0 && (
              <div className="mt-4 overflow-hidden rounded-2xl border">
                <Table>
                  <TableHeader>
                    <TableRow className="bg-muted/40 hover:bg-muted/40">
                      <TableHead>{t("discovery.product.columns.shop")}</TableHead>
                      <TableHead className="text-right">{t("discovery.product.columns.price")}</TableHead>
                      <TableHead className="hidden sm:table-cell">{t("discovery.product.columns.stock")}</TableHead>
                      <TableHead className="text-right">{t("discovery.product.columns.distance")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {[{ ...live, shop: live.shop as ProductDetail["shop"] }, ...live.other_offers].map((o, i) => (
                      <TableRow key={o.id} className={cn(i === 0 && "bg-brand-soft/40 hover:bg-brand-soft/50")}>
                        <TableCell className="max-w-48">
                          <Link href={`/product/${o.id}`} className="block truncate font-medium hover:underline">{o.shop.name}</Link>
                          <span className="flex items-center gap-2 text-xs text-muted-foreground">
                            {i === 0 ? t("discovery.product.viewing") : o.shop.locality}
                            <Rating value={o.shop.rating_avg} count={o.shop.rating_count} showCount={false} />
                          </span>
                        </TableCell>
                        <TableCell className="text-right font-semibold tabular">{formatPrice(o.price)}</TableCell>
                        <TableCell className="hidden sm:table-cell">
                          {isCallOnly(o) ? (
                            <span className="inline-flex items-center gap-1 text-xs font-medium text-warning-ink">
                              <Phone className="size-3" aria-hidden /> {t("discovery.product.callToCheck")}
                            </span>
                          ) : (
                            <div className="flex flex-col items-start gap-1">
                              <StockPill status={o.stock_status} quantity={o.quantity} compact />
                              <Freshness at={o.stock_updated_at} />
                            </div>
                          )}
                        </TableCell>
                        <TableCell className="text-right text-sm tabular">{formatDistance(o.shop.distance_km) || "-"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </section>

          {/* recommendations */}
          {recs.data && recs.data.items.length > 0 && (
            <section>
              <div className="flex items-center gap-2">
                <h2 className="text-xl font-semibold">{recs.data.source === "apriori"
                    ? t("discovery.product.boughtTogether")
                    : t("discovery.product.popularIn", { category: p.category_detail.name })}</h2>
                {recs.data.source === "apriori" && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-brand-soft px-2 py-0.5 text-xs font-medium text-brand-ink">
                    <Sparkles className="size-3" /> {t("discovery.product.basketAi")}
                  </span>
                )}
              </div>
              <p className="mt-1 text-sm text-muted-foreground">
                {recs.data.source === "apriori" ? t("discovery.product.boughtTogetherBody") : t("discovery.product.popularBody")}
              </p>
              {/* grid-cols-1 (minmax(0, 1fr)): a long name truncates instead of widening the page, which made a
                  phone zoom out and shift the layout (P2.9). */}
              <ul className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
                {recs.data.items.map((r) => (
                  <li key={r.offer.id}>
                    <Link href={`/product/${r.offer.id}`} className="flex items-center gap-3 rounded-2xl border bg-card p-3 transition-shadow hover:shadow-lift">
                      <ProductThumb icon={r.offer.icon} imageUrl={r.offer.image_url} thumbnailUrl={r.offer.thumbnail_url} category={r.offer.category} alt="" size="sm" />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium">{r.offer.name}</p>
                        <p className="truncate text-xs text-muted-foreground">
                          {r.same_shop ? t("discovery.product.alsoAtShop") : `${r.offer.shop.name} · ${formatDistance(r.offer.shop.distance_km)}`}
                          {r.confidence !== null && ` · ${t("discovery.product.buyersShare", { percent: Math.round(r.confidence * 100) })}`}
                        </p>
                      </div>
                      <span className="text-sm font-semibold tabular">{formatPrice(r.offer.price)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* reviews */}
          <section>
            <h2 className="text-xl font-semibold">{t("discovery.product.reviewsTitle", { shop: shop.name })}</h2>
            <div className="mt-3">
              <ReviewList rating={{ avg: shop.rating_avg }} reviews={p.reviews} shopId={shop.id} empty={t("discovery.product.reviewsEmpty")} />
            </div>
            {shop.rating_count > p.reviews.length && (
              <Link href={`/shops/${shop.slug}#reviews`} className="mt-2 inline-flex items-center gap-1 text-sm font-medium hover:underline">
                {t(`discovery.product.allReviews.${plural(language, shop.rating_count)}`, { count: formatNumber(shop.rating_count) })} <ArrowRight className="size-3.5" />
              </Link>
            )}
          </section>
        </div>

        {/* ---------------------------------------------------------- right: buy box */}
        <aside className="space-y-4 lg:sticky lg:top-20 lg:self-start">
          <div className="rounded-3xl border bg-card p-5 shadow-soft">
            <Price value={p.price} mrp={p.mrp} size="xl" />
            {insight?.statement && (
              <p className={cn("mt-2 inline-flex items-center gap-1.5 text-sm", (insight.deviation_pct ?? 0) < -3 ? "text-success-ink" : "text-muted-foreground")}>
                {(insight.deviation_pct ?? 0) < -3 ? <TrendingDown className="size-4" /> : (insight.deviation_pct ?? 0) > 3 ? <TrendingUp className="size-4" /> : null}
                {insight.statement}
              </p>
            )}
            {!callOnly && (
              <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2">
                <StockPill status={p.stock_status} quantity={p.quantity} />
                <Freshness at={p.stock_updated_at} />
              </div>
            )}
            {p.sold_last_30d > 0 && (
              <p className="mt-2 text-xs text-muted-foreground">{t(`discovery.product.sold30d.${plural(language, p.sold_last_30d)}`, { count: formatNumber(p.sold_last_30d) })}</p>
            )}

            {p.my_reservation ? (
              <Link href={`/account/reservations/${p.my_reservation.id}`} className="mt-5 flex items-center justify-between rounded-2xl bg-brand-soft p-4 text-brand-ink">
                <span>
                  <span className="block text-sm font-semibold">{t("discovery.product.youReserved", { code: p.my_reservation.code })}</span>
                  <StatusBadge status={p.my_reservation.status} kind="reservation" className="mt-1 bg-card" />
                </span>
                <ArrowRight className="size-4" />
              </Link>
            ) : callOnly ? (
              <div className="mt-5 space-y-2.5">
                <div className="rounded-2xl bg-warning-soft p-4 text-warning-ink">
                  <p className="flex items-center gap-1.5 text-sm font-semibold">
                    <Phone className="size-4" aria-hidden /> {t("discovery.product.callOnlyTitle")}
                  </p>
                  <p className="mt-1 text-sm">{t("discovery.product.callOnlyBody")}</p>
                </div>
                {shop.phone ? (
                  <Button asChild size="lg" className="h-12 w-full rounded-xl bg-brand text-base text-brand-foreground hover:bg-brand/90">
                    <a href={`tel:${shop.phone}`} onClick={() => recordLead(shop.id, "call_tap", p.id)}>
                      <Phone /> {t("discovery.product.callShop")}
                    </a>
                  </Button>
                ) : (
                  <p className="text-center text-xs text-muted-foreground">{t("discovery.product.callOnlyNoPhone")}</p>
                )}
              </div>
            ) : (
              <div className="mt-5 space-y-2.5">
                <Button
                  size="lg"
                  className="h-12 w-full rounded-xl bg-brand text-base text-brand-foreground hover:bg-brand/90"
                  disabled={out || !shop.offers_pickup || !isCustomer}
                  onClick={() => requireCustomer(openReserve)}
                  {...preloadReserve}
                >
                  <Footprints /> {t("discovery.product.reserve")}
                </Button>
                <Button
                  size="lg"
                  variant="outline"
                  className="h-12 w-full rounded-xl text-base"
                  disabled={out || !shop.offers_delivery || !isCustomer || (delivery !== null && !delivery.eligible)}
                  onClick={() => requireCustomer(openDelivery)}
                  {...preloadDelivery}
                >
                  <Bike /> {t("discovery.product.requestDelivery")}
                </Button>
                <p className="text-center text-xs text-muted-foreground">
                  {!isCustomer
                    ? t("discovery.product.hintCustomer")
                    : out
                      ? t("discovery.product.hintOut")
                      : !shop.offers_delivery
                        ? t("discovery.product.hintPickupOnly", { minutes: shop.hold_minutes })
                        : delivery && !delivery.eligible
                          ? delivery.reason
                          : delivery
                            ? t("discovery.product.hintDelivery", {
                                fee: delivery.delivery_fee ? formatPrice(delivery.delivery_fee) : t("discovery.product.free"),
                                distance: formatDistance(delivery.distance_km),
                              })
                            : t("discovery.product.hintPayAtShop")}
                </p>
              </div>
            )}
          </div>

          {/* shop box */}
          <div className="rounded-3xl border bg-card p-5">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <Link href={`/shops/${shop.slug}`} className="flex items-center gap-1 font-semibold hover:underline">
                  <span className="truncate">{shop.name}</span>
                  {shop.is_verified && <VerifiedMark />}
                </Link>
                <p className="mt-0.5 text-sm text-muted-foreground">{shop.address_line}</p>
                {shop.opening_hours && (
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {t("discovery.product.hours", { hours: shop.opening_hours })}
                    {shop.closed_on && ` · ${t("discovery.product.closedOn", { day: weekdayName(shop.closed_on, language) })}`}
                  </p>
                )}
              </div>
              <Rating value={shop.rating_avg} count={shop.rating_count} />
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-sm">
              <OpenBadge isOpen={shop.is_open} label={shop.hours_label} />
              {shop.distance_km !== null && (
                <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                  <MapPin className="size-3.5" /> <Distance km={shop.distance_km} />
                  {travel && ` · ${t(`common.travel.${travel.mode}`, { minutes: travel.minutes })}`}
                </span>
              )}
            </div>
            <div className="mt-4 h-40 overflow-hidden rounded-2xl border">
              <ShopMap
                className="h-full"
                center={shop}
                zoom={15}
                fitToPoints={false}
                interactive={false}
                points={[{ id: shop.id, lat: shop.lat, lng: shop.lng, label: shop.name, title: shop.name }]}
                user={location?.source === "gps" ? location : null}
              />
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2">
              <Button asChild variant="outline" size="sm">
                <a
                  href={`https://www.openstreetmap.org/directions?to=${shop.lat}%2C${shop.lng}`}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => recordLead(shop.id, "directions_tap", p.id)}
                >
                  <Navigation /> {t("discovery.product.directions")}
                </a>
              </Button>
              {shop.phone && (
                <Button asChild variant="outline" size="sm">
                  <a href={`tel:${shop.phone}`} onClick={() => recordLead(shop.id, "call_tap", p.id)}>
                    <Phone /> {t("discovery.product.callShop")}
                  </a>
                </Button>
              )}
            </div>
          </div>

          <div className="rounded-3xl border bg-card p-5">
            <p className="mb-3 text-sm font-semibold">{t("discovery.product.trustTitle")}</p>
            <ShopTrust r={shop.reliability} />
          </div>
        </aside>
      </div>

      {/* mobile sticky buy bar */}
      {!p.my_reservation && isCustomer && !out && !callOnly && (
        <div className="fixed inset-x-0 bottom-[calc(3.9rem+env(safe-area-inset-bottom))] z-30 border-t bg-background/95 p-3 backdrop-blur lg:hidden">
          <div className="mx-auto flex max-w-md items-center gap-3">
            <div className="min-w-0">
              <p className="text-lg font-semibold tabular">{formatPrice(p.price)}</p>
              <p className="truncate text-xs text-muted-foreground">
                {t("discovery.product.stickyStock", { count: formatNumber(p.quantity), shop: shop.name })}
              </p>
            </div>
            <Button className="ml-auto h-11 rounded-xl bg-brand px-5 text-brand-foreground hover:bg-brand/90" onClick={() => requireCustomer(openReserve)} {...preloadReserve}>
              <Footprints /> {t("discovery.product.reserveShort")}
            </Button>
          </div>
        </div>
      )}

      {dialogsUsed.reserve && (
        <Suspense fallback={reserveOpen ? <DialogLoading /> : null}>
          <ReserveDialog product={p} open={reserveOpen} onOpenChange={setReserveOpen} />
        </Suspense>
      )}
      {shop.offers_delivery && dialogsUsed.delivery && (
        <Suspense fallback={deliveryOpen ? <DialogLoading /> : null}>
          <DeliveryDialog product={p} open={deliveryOpen} onOpenChange={setDeliveryOpen} />
        </Suspense>
      )}
    </div>
  );
}

function ProductSkeleton() {
  return (
    <div className="mx-auto grid max-w-7xl gap-10 px-4 pt-10 md:px-6 lg:grid-cols-[1fr_420px]">
      <div className="grid gap-6 sm:grid-cols-[280px_1fr]">
        <Skeleton className="aspect-square rounded-3xl" />
        <div className="space-y-3">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-8 w-3/4" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-5/6" />
        </div>
      </div>
      <Skeleton className="h-80 rounded-3xl" />
    </div>
  );
}
