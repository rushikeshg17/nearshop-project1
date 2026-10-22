"use client";

import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  BellRing,
  Bike,
  BrainCircuit,
  Check,
  ClipboardCheck,
  Footprints,
  PackageSearch,
  Search,
  ShieldCheck,
  Store,
  TrendingUp,
} from "lucide-react";
import Link from "next/link";

import { AppIcon } from "@/components/common/app-icon";
import { Freshness, StockPill } from "@/components/common/indicators";
import { ProductThumb } from "@/components/common/product-thumb";
import { useI18n } from "@/components/providers";
import { ShopCard } from "@/components/shop/shop-card";
import { LocationPicker } from "@/components/site/location-picker";
import { SearchBox } from "@/components/site/search-box";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useMeta } from "@/hooks/use-session";
import { api } from "@/lib/api";
import { formatDistance, formatNumber, formatPrice } from "@/lib/format";
import { plural } from "@/lib/i18n";
import { useLocation } from "@/lib/location";
import type { SearchResponse, ShopBrief } from "@/lib/types";
import { cn, revealFocused } from "@/lib/utils";

// Example searches, in the interface language (discovery.home.quick in the message files).
const QUICK = ["charger", "elbow", "shoes", "bulb", "umbrella", "oil"] as const;

// Entrance animations are CSS (tw-animate-css), not an animation library, so none of it is first-load
// JavaScript (P2.9). The hero's heading and text are not animated: they are the page's largest paint.
const ENTER = "animate-in fade-in fill-mode-both duration-500 ease-out";

export function HomeView() {
  const { t, language } = useI18n();
  const { data: meta } = useMeta();
  const { location, radiusKm, ready } = useLocation();

  const live = useQuery({
    queryKey: ["home-live", location?.lat, location?.lng],
    queryFn: () =>
      api.get<SearchResponse>("/search", { q: "fast charger", lat: location!.lat, lng: location!.lng, radius_km: 6, page_size: 3 }),
    enabled: ready,
    staleTime: 5 * 60_000,
  });
  const shops = useQuery({
    queryKey: ["shops", "near", location?.lat, location?.lng, radiusKm],
    queryFn: () => api.get<{ shops: ShopBrief[] }>("/shops", { lat: location!.lat, lng: location!.lng, radius_km: Math.max(radiusKm, 4) }),
    enabled: ready,
  });

  return (
    <>
      {/* ---------------------------------------------------------------- hero */}
      <section className="relative overflow-hidden border-b">
        <div className="bg-dots absolute inset-0 [mask-image:radial-gradient(ellipse_at_30%_20%,black,transparent_70%)]" aria-hidden />
        <div className="relative mx-auto grid max-w-7xl gap-12 px-4 pt-12 pb-16 md:px-6 md:pt-20 md:pb-24 lg:grid-cols-[1.15fr_1fr] lg:items-center">
          <div>
            <div className="mb-5">
              <LocationPicker />
            </div>
            <h1 className="text-[2.6rem] leading-[1.02] font-semibold sm:text-6xl lg:text-[3.6rem] xl:text-7xl"
            >
              {t("discovery.home.heroTitle")}
              <span className="block text-muted-foreground">{t("discovery.home.heroSubtitle")}</span>
            </h1>
            <p className="mt-5 max-w-xl text-base text-muted-foreground md:text-lg"
            >
              {t("discovery.home.heroBody")}
            </p>
            <div className="mt-8 max-w-xl">
              <SearchBox size="lg" placeholder={t("discovery.home.searchPlaceholder")} />
              <div className="mt-4 flex flex-wrap gap-2">
                {QUICK.map((key) => t(`discovery.home.quick.${key}`)).map((q) => (
                  <Link
                    key={q}
                    href={`/search?q=${encodeURIComponent(q)}`}
                    className="rounded-full border bg-card px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:border-foreground/20 hover:text-foreground"
                  >
                    {q}
                  </Link>
                ))}
              </div>
            </div>
            {/* Always laid out, invisible until the city's numbers arrive, so nothing below it moves (P2.9). */}
            <dl className={cn("mt-10 flex flex-wrap gap-x-8 gap-y-3 text-sm", !meta && "invisible")} aria-hidden={!meta}>
              {[
                [
                  meta ? formatNumber(meta.stats.shops) : "00",
                  meta ? t("discovery.home.statShopsIn", { city: meta.city.name }) : t("discovery.home.statShopsHere"),
                ],
                [meta ? formatNumber(meta.stats.in_stock) : "0,000", t("discovery.home.statItems")],
                ["0", t("discovery.home.statRiders")],
              ].map(([n, l], i) => (
                <div key={i} className="flex items-baseline gap-2">
                  <dt className="font-heading text-2xl font-semibold tabular">{n}</dt>
                  <dd className="text-muted-foreground">{l}</dd>
                </div>
              ))}
            </dl>
          </div>

          {/* Live proof: real results from the API, not a mockup */}
          <div className={`${ENTER} relative hidden zoom-in-97 slide-in-from-bottom-5 delay-200 duration-700 lg:block`}
          >
            <div className="absolute -inset-6 rounded-[2.5rem] bg-gradient-to-b from-brand/10 to-transparent blur-2xl" aria-hidden />
            <div className="relative rounded-3xl border bg-card/90 p-4 shadow-lift backdrop-blur">
              <div className="flex items-center gap-2 rounded-xl border bg-background px-3 py-2.5 text-sm">
                <Search className="size-4 text-muted-foreground" />
                <span>fast charger</span>
                <span className="ml-auto text-xs text-muted-foreground">
                  {live.data
                    ? `${t(`discovery.home.demoItems.${plural(language, live.data.total)}`, { count: formatNumber(live.data.total) })} · ${t(
                        `discovery.home.demoShops.${plural(language, live.data.shops.length)}`,
                        { count: formatNumber(live.data.shops.length) },
                      )}`
                    : t("discovery.home.searching")}
                </span>
              </div>
              <ul className="mt-3 space-y-2">
                {live.isLoading || !live.data
                  ? Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-[88px] rounded-2xl" />)
                  : live.data.groups.map((g, i) => (
                      <li key={g.key} className={`${ENTER} slide-in-from-right-4`} style={{ animationDelay: `${400 + i * 120}ms` }}>
                        <Link
                          href={`/product/${g.best_offer.id}`}
                          className="flex items-center gap-3 rounded-2xl border bg-background p-3 transition-colors hover:bg-accent/60"
                        >
                          <ProductThumb icon={g.icon} category={g.category} imageUrl={g.image_url} thumbnailUrl={g.thumbnail_url} alt="" size="md" />
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-sm font-semibold">{g.name}</p>
                            <p className="mt-0.5 truncate text-xs text-muted-foreground">
                              {g.best_offer.shop.name} · {formatDistance(g.best_offer.shop.distance_km)}
                              {g.shop_count > 1 &&
                                ` · ${t(`discovery.home.moreShops.${plural(language, g.shop_count - 1)}`, { count: formatNumber(g.shop_count - 1) })}`}
                            </p>
                            <div className="mt-1.5 flex items-center gap-2">
                              <StockPill status={g.best_offer.stock_status} quantity={g.best_offer.quantity} compact />
                              <Freshness at={g.best_offer.stock_updated_at} />
                            </div>
                          </div>
                          <p className="self-start font-semibold tabular">{formatPrice(g.min_price)}</p>
                        </Link>
                      </li>
                    ))}
              </ul>
              <div className="mt-3 flex items-center justify-between rounded-xl bg-brand-soft px-3 py-2.5 text-sm text-brand-ink">
                <span className="inline-flex items-center gap-2 font-medium">
                  <Footprints className="size-4" /> {t("discovery.home.reserveBanner")}
                </span>
                <ArrowRight className="size-4" />
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ---------------------------------------------------------------- categories */}
      <section className="mx-auto max-w-7xl px-4 pt-14 md:px-6">
        <div className="mb-6 flex items-end justify-between">
          <div>
            <h2 className="text-2xl font-semibold md:text-3xl">{t("discovery.home.browseTitle")}</h2>
            <p className="mt-1 text-muted-foreground">{t("discovery.home.browseBody")}</p>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {(meta?.categories ?? Array.from({ length: 10 }).map(() => null)).map((c, i) =>
            c ? (
              <div key={c.slug}>
                <Link
                  href={`/search?category=${c.slug}`}
                  className="group flex h-full flex-col gap-3 rounded-2xl border bg-card p-4 transition-[transform,box-shadow] duration-200 hover:-translate-y-0.5 hover:shadow-lift"
                >
                  <span
                    className="grid size-11 place-items-center rounded-xl transition-transform duration-300 group-hover:scale-105"
                    style={{
                      background: `light-dark(oklch(0.95 0.035 ${c.color_hue}), oklch(0.3 0.045 ${c.color_hue}))`,
                      color: `light-dark(oklch(0.5 0.12 ${c.color_hue}), oklch(0.82 0.09 ${c.color_hue}))`,
                    }}
                  >
                    <AppIcon name={c.icon} className="size-5" />
                  </span>
                  <span>
                    <span className="block font-medium">{c.name}</span>
                    <span className="text-xs text-muted-foreground tabular">{t("discovery.home.categoryInStock", { count: formatNumber(c.in_stock_listings ?? 0) })}</span>
                  </span>
                </Link>
              </div>
            ) : (
              <Skeleton key={i} className="h-[118px] rounded-2xl" />
            ),
          )}
        </div>
      </section>

      {/* ---------------------------------------------------------------- nearby shops */}
      <section className="mx-auto max-w-7xl px-4 pt-16 md:px-6">
        <div className="mb-6 flex items-end justify-between gap-4">
          <div>
            <h2 className="text-2xl font-semibold md:text-3xl">
              {location?.label ? t("discovery.home.shopsNear", { place: location.label }) : t("discovery.home.shopsNearYou")}
            </h2>
            <p className="mt-1 text-muted-foreground">{t("discovery.home.shopsBody")}</p>
          </div>
          <Button asChild variant="outline" className="hidden rounded-full sm:inline-flex">
            <Link href="/shops">
              {t("discovery.home.seeMap")} <ArrowRight />
            </Link>
          </Button>
        </div>
        <div className="-mx-4 flex snap-x gap-3 overflow-x-auto px-4 pb-2 scrollbar-none md:mx-0 md:grid md:grid-cols-3 md:overflow-visible md:px-0 lg:grid-cols-4" onFocus={revealFocused}>
          {shops.isLoading || !shops.data
            ? Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-44 w-72 shrink-0 rounded-2xl md:w-auto" />)
            : shops.data.shops.slice(0, 8).map((s) => (
                <ShopCard key={s.id} shop={s} categories={meta?.categories} className="w-72 shrink-0 snap-start md:w-auto" />
              ))}
        </div>
      </section>

      {/* ---------------------------------------------------------------- how it works */}
      <section id="how" className="mx-auto max-w-7xl px-4 pt-20 md:px-6">
        <div className="max-w-2xl">
          <h2 className="text-2xl font-semibold md:text-3xl">{t("discovery.home.howTitle")}</h2>
          <p className="mt-2 text-muted-foreground">{t("discovery.home.howBody")}</p>
        </div>
        <div className="mt-8 grid gap-4 md:grid-cols-3">
          {(
            [
              { key: "search", icon: PackageSearch },
              { key: "reserve", icon: ClipboardCheck },
              { key: "pay", icon: Footprints },
            ] as const
          ).map((s, i) => (
            <div key={s.key} className="relative rounded-2xl border bg-card p-6">
              <span aria-hidden className="absolute top-6 right-6 font-heading text-4xl font-semibold text-muted-foreground/75 tabular">0{i + 1}</span>
              <s.icon className="size-6 text-brand" />
              <h3 className="mt-4 text-lg font-semibold">{t(`discovery.home.steps.${s.key}.title`)}</h3>
              <p className="mt-1.5 text-sm text-muted-foreground">{t(`discovery.home.steps.${s.key}.body`)}</p>
              <span className="mt-4 inline-flex rounded-full bg-muted px-2.5 py-1 text-xs font-medium">
                {t(`discovery.home.steps.${s.key}.tag`)}
              </span>
            </div>
          ))}
        </div>
      </section>

      {/* ---------------------------------------------------------------- for shops */}
      <section className="mx-auto max-w-7xl px-4 pt-20 md:px-6">
        <div className="overflow-hidden rounded-3xl bg-primary text-primary-foreground">
          <div className="grid gap-10 p-8 md:p-12 lg:grid-cols-[1.1fr_1fr]">
            <div>
              <span className="inline-flex items-center gap-2 rounded-full bg-primary-foreground/10 px-3 py-1 text-xs font-medium">
                <Store className="size-3.5" /> {t("discovery.home.owners.badge")}
              </span>
              <h2 className="mt-4 text-3xl font-semibold md:text-4xl">{t("discovery.home.owners.title")}</h2>
              <p className="mt-3 max-w-lg text-primary-foreground/70">{t("discovery.home.owners.body")}</p>
              <div className="mt-8 flex flex-wrap gap-3">
                <Button asChild size="lg" className="h-11 rounded-full bg-brand px-6 text-brand-foreground hover:bg-brand/90">
                  <Link href="/register/shop">
                    {t("discovery.home.owners.listFree")} <ArrowRight />
                  </Link>
                </Button>
                <Button asChild size="lg" variant="ghost" className="h-11 rounded-full px-5 text-primary-foreground hover:bg-primary-foreground/10 hover:text-primary-foreground">
                  <Link href="/login?next=/shop">{t("discovery.home.owners.seeDashboard")}</Link>
                </Button>
              </div>
            </div>
            <ul className="grid gap-3 sm:grid-cols-2">
              {(
                [
                  { key: "alerts", icon: BellRing },
                  { key: "forecasts", icon: TrendingUp },
                  { key: "demand", icon: BrainCircuit },
                  { key: "trust", icon: ShieldCheck },
                  { key: "delivery", icon: Bike },
                  { key: "commission", icon: Check },
                ] as const
              ).map((f) => (
                <li key={f.key} className="rounded-2xl bg-primary-foreground/[0.06] p-4">
                  <f.icon className="size-5 text-brand" />
                  <p className="mt-3 text-sm font-medium">{t(`discovery.home.owners.features.${f.key}.title`)}</p>
                  <p className="mt-0.5 text-sm text-primary-foreground/60">{t(`discovery.home.owners.features.${f.key}.body`)}</p>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>
    </>
  );
}
