"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import {
  ArrowUpDown,
  Bike,
  ChevronDown,
  List,
  Loader2,
  Map as MapIcon,
  PackageSearch,
  SlidersHorizontal,
  Sparkles,
  Store,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Fragment, lazy, Suspense, useMemo, useState } from "react";

import { AppIcon } from "@/components/common/app-icon";
import { Distance, Rating } from "@/components/common/indicators";
import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { ShopMap } from "@/components/map/shop-map";
import { useI18n } from "@/components/providers";
import { ResultCard } from "@/components/search/result-card";
import { LocationPicker } from "@/components/site/location-picker";
import { SearchBox } from "@/components/site/search-box";
import { Button } from "@/components/ui/button";
import { useMeta } from "@/hooks/use-session";
import { api } from "@/lib/api";
import { formatNumber, formatPrice } from "@/lib/format";
import { plural, richText } from "@/lib/i18n";
import { useLocation } from "@/lib/location";
import type { MapPin, SearchResponse, SponsoredCard } from "@/lib/types";
import { cn, revealFocused } from "@/lib/utils";

// The filters sheet loads when the pointer or focus reaches its button and opens on the first tap; it is not
// first-load JavaScript (P2.9).
const loadFilters = () => import("@/components/search/search-filters");
const SearchFiltersSheet = lazy(() => loadFilters().then((m) => ({ default: m.SearchFiltersSheet })));
// The sponsored card (Boost, P10.13) loads only when a results page carries one, which almost no search does, so it
// adds nothing to the route's first-load JavaScript (P2.9). While it loads, a placeholder of its size holds its place.
const SponsoredResultCard = lazy(() => import("@/components/search/sponsored-card").then((m) => ({ default: m.SponsoredResultCard })));

const SORTS = [
  { value: "relevance", label: "discovery.search.sort.relevance" },
  { value: "distance", label: "discovery.search.sort.distance" },
  { value: "price_asc", label: "discovery.search.sort.priceAsc" },
  { value: "price_desc", label: "discovery.search.sort.priceDesc" },
] as const;

function SponsoredSlot(props: { card: SponsoredCard; onHover: (shopIds: number[] | null) => void }) {
  return (
    <Suspense fallback={<div aria-hidden className="h-40 animate-pulse rounded-2xl border-2 border-info/40 bg-card" />}>
      <SponsoredResultCard {...props} />
    </Suspense>
  );
}

export function SearchView() {
  const { t, language } = useI18n();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const { data: meta } = useMeta();
  const { location, radiusKm, setRadiusKm, ready } = useLocation();
  const [mobileMap, setMobileMap] = useState(false);
  const [hoverShops, setHoverShops] = useState<number[] | null>(null);
  const [selectedShop, setSelectedShop] = useState<number | null>(null);
  const [filtersUsed, setFiltersUsed] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);

  const q = params.get("q") ?? "";
  const category = params.get("category") ?? "";
  const sort = params.get("sort") ?? "relevance";
  const inStock = params.get("stock") === "1";
  const fulfillment = params.get("fulfillment") ?? "";
  const minPrice = params.get("min") ?? "";
  const maxPrice = params.get("max") ?? "";

  function setParam(updates: Record<string, string | null>) {
    const next = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(updates)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    router.replace(`${pathname}?${next.toString()}`, { scroll: false });
  }

  const query = useInfiniteQuery({
    queryKey: ["search", q, category, sort, inStock, fulfillment, minPrice, maxPrice, location?.lat, location?.lng, radiusKm],
    queryFn: ({ pageParam }) =>
      api.get<SearchResponse>("/search", {
        q,
        category,
        sort,
        in_stock_only: inStock || undefined,
        fulfillment: fulfillment || undefined,
        min_price: minPrice || undefined,
        max_price: maxPrice || undefined,
        lat: location!.lat,
        lng: location!.lng,
        radius_km: radiusKm,
        page: pageParam,
        page_size: 20,
      }),
    initialPageParam: 1,
    getNextPageParam: (last) => (last.has_more ? last.page + 1 : undefined),
    enabled: ready,
    placeholderData: (prev) => prev,
  });

  const first = query.data?.pages[0];
  const groups = useMemo(() => query.data?.pages.flatMap((p) => p.groups) ?? [], [query.data]);
  const pins: MapPin[] = useMemo(() => first?.shops ?? [], [first]);
  // Boost (P10.13): the one sponsored card comes with the first page only (later pages never carry one, and any that
  // did would be ignored), and goes in front of the organic card now at its 1-based position, or after the last one
  // of a shorter list. Organic cards keep their own order and positions (the click positions count them only).
  const sponsored = first?.sponsored;
  const slot = sponsored ? Math.max(0, sponsored.position - 1) : -1;
  const points = useMemo(
    () =>
      pins.map((s) => ({
        id: s.id,
        lat: s.lat,
        lng: s.lng,
        label: s.min_price !== null ? formatPrice(s.min_price) : t("discovery.search.pinOutOfStock"),
        title: t(`discovery.search.pinTitle.${plural(language, s.in_stock_count)}`, {
          shop: s.name,
          count: formatNumber(s.in_stock_count),
        }),
        muted: s.in_stock_count === 0,
      })),
    [pins, t, language],
  );
  const selected = pins.find((s) => s.id === selectedShop);
  const catName = meta?.categories.find((c) => c.slug === category)?.name;
  const activeFilters = [inStock, !!fulfillment, !!minPrice || !!maxPrice].filter(Boolean).length;
  const heading = q ? `"${first?.corrected_from ? first.normalized_query : q}"` : catName ?? t("discovery.search.everythingNearby");
  const counts = first && {
    items: t(`discovery.search.items.${plural(language, first.total)}`, { count: formatNumber(first.total) }),
    shops: t(`discovery.search.shops.${plural(language, first.shops.length)}`, { count: formatNumber(first.shops.length) }),
    radius: radiusKm,
  };

  const filtersButton = (
    <Button
      variant="outline"
      className="h-9 rounded-full"
      aria-haspopup="dialog"
      onPointerEnter={() => void loadFilters()}
      onFocus={() => void loadFilters()}
      onClick={
        filtersUsed
          ? undefined
          : () => {
              setFiltersUsed(true);
              setFiltersOpen(true);
            }
      }
    >
      <SlidersHorizontal /> {t("discovery.search.filters")}
      {activeFilters > 0 && (
        <span className="grid size-5 place-items-center rounded-full bg-primary text-[11px] text-primary-foreground tabular">{activeFilters}</span>
      )}
    </Button>
  );

  return (
    <div className="mx-auto max-w-[1440px] lg:grid lg:grid-cols-[minmax(0,1fr)_minmax(380px,44%)]">
      {/* ------------------------------------------------------------ results column */}
      <div className={cn("min-w-0 px-4 pt-4 pb-10 md:px-6", mobileMap && "max-lg:hidden")}>
        <div className="space-y-3 md:hidden">
          <SearchBox />
          <LocationPicker className="max-w-full" />
        </div>

        {/* category chips */}
        <div className="-mx-4 mt-4 flex gap-2 overflow-x-auto px-4 pb-1 scrollbar-none md:mx-0 md:mt-1 md:px-0" onFocus={revealFocused}>
          <Chip active={!category} onClick={() => setParam({ category: null })}>{t("discovery.search.all")}</Chip>
          {meta?.categories.map((c) => (
            <Chip key={c.slug} active={category === c.slug} onClick={() => setParam({ category: category === c.slug ? null : c.slug })}>
              <AppIcon name={c.icon} className="size-3.5" />
              {c.name}
              {q && first?.category_counts[c.slug] ? (
                <span className="text-muted-foreground tabular">{first.category_counts[c.slug]}</span>
              ) : null}
            </Chip>
          ))}
        </div>

        {/* summary + controls */}
        <div className="mt-5 flex flex-wrap items-end justify-between gap-3">
          <div className="min-w-0">
            <h1 className="truncate text-2xl font-semibold">{heading}</h1>
            <p className="mt-0.5 text-sm text-muted-foreground" aria-live="polite">
              {query.isLoading || !counts
                ? t("discovery.search.checking")
                : location
                  ? t("discovery.search.summaryOf", { ...counts, place: location.label })
                  : t("discovery.search.summary", counts)}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {/* A native select: the phone's own picker, and no Radix select on first load (P2.9). */}
            <label className="relative inline-flex h-9 items-center rounded-full border border-input text-sm transition-colors focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50 hover:bg-accent dark:bg-input/30">
              <ArrowUpDown className="pointer-events-none absolute left-3 size-3.5" aria-hidden />
              <select
                value={sort}
                onChange={(e) => setParam({ sort: e.target.value === "relevance" ? null : e.target.value })}
                aria-label={t("discovery.search.sort.label")}
                className="h-full cursor-pointer appearance-none rounded-full bg-transparent pr-8 pl-8 outline-none"
              >
                {SORTS.map((s) => (
                  <option key={s.value} value={s.value}>{t(s.label)}</option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-2.5 size-4 text-muted-foreground" aria-hidden />
            </label>
            {filtersUsed ? (
              <Suspense fallback={filtersButton}>
                <SearchFiltersSheet
                  trigger={filtersButton}
                  open={filtersOpen}
                  onOpenChange={setFiltersOpen}
                  values={{ inStock, fulfillment, minPrice, maxPrice }}
                  setParam={setParam}
                />
              </Suspense>
            ) : (
              filtersButton
            )}
          </div>
        </div>

        {/* AI hint / corrections */}
        {first?.corrected_from && (
          <p className="mt-3 animate-in text-sm duration-300 fade-in">
            {richText(t("discovery.search.corrected"), {
              query: <span className="font-medium">{first.normalized_query}</span>,
              original: first.corrected_from,
            })}
          </p>
        )}
        {first?.did_you_mean && (
          <p className="mt-3 animate-in text-sm duration-300 fade-in">
            {richText(t("discovery.search.didYouMean"), {
              suggestion: (
                <Link href={`/search?q=${encodeURIComponent(first.did_you_mean)}`} className="font-medium text-brand-ink underline underline-offset-4">
                  {first.did_you_mean}
                </Link>
              ),
            })}
          </p>
        )}
        {q && first && first.total > 0 && !first.corrected_from && (
          <p className="mt-3 inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <Sparkles className="size-3.5 text-brand" />
            {first.expanded_terms.length > 0
              ? t("discovery.search.matchedAlso", { terms: first.expanded_terms.slice(0, 3).join(", ") })
              : t("discovery.search.matched")}
          </p>
        )}

        {/* results */}
        <div className="mt-5">
          {query.isError ? (
            <ErrorState error={query.error} onRetry={() => query.refetch()} />
          ) : query.isLoading || !first ? (
            <ListSkeleton rows={5} />
          ) : groups.length === 0 ? (
            <EmptyState
              icon={PackageSearch}
              title={
                first.shops_in_radius === 0
                  ? t("discovery.search.noShopsTitle")
                  : t("discovery.search.nothingTitle", { radius: radiusKm })
              }
              description={
                first.shops_in_radius === 0 ? t("discovery.search.noShopsBody") : t("discovery.search.nothingBody")
              }
              action={
                <>
                  {radiusKm < 15 && (
                    <Button onClick={() => setRadiusKm(Math.min(15, radiusKm + 5))}>
                      {t("discovery.search.expand", { radius: Math.min(15, radiusKm + 5) })}
                    </Button>
                  )}
                  {activeFilters > 0 && (
                    <Button variant="outline" onClick={() => setParam({ stock: null, fulfillment: null, min: null, max: null })}>
                      {t("discovery.search.clearFilters")}
                    </Button>
                  )}
                </>
              }
            />
          ) : (
            <div className={cn("space-y-3 transition-opacity", query.isFetching && !query.isFetchingNextPage && "opacity-60")}>
              {groups.map((g, i) => (
                <Fragment key={g.key}>
                  {sponsored && slot === i && <SponsoredSlot card={sponsored} onHover={setHoverShops} />}
                  <ResultCard
                    group={g}
                    index={i}
                    highlighted={!!selectedShop && g.offers.some((o) => o.shop.id === selectedShop)}
                    onHover={setHoverShops}
                  />
                </Fragment>
              ))}
              {sponsored && slot >= groups.length && <SponsoredSlot card={sponsored} onHover={setHoverShops} />}
              {query.hasNextPage && (
                <Button variant="outline" className="w-full" onClick={() => query.fetchNextPage()} disabled={query.isFetchingNextPage}>
                  {query.isFetchingNextPage ? <Loader2 className="animate-spin" /> : null} {t("discovery.search.showMore")}
                </Button>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ------------------------------------------------------------ map column */}
      <div
        className={cn(
          "lg:sticky lg:top-16 lg:block lg:h-[calc(100dvh-4rem)] lg:p-3 lg:pl-0",
          mobileMap ? "fixed inset-x-0 top-16 bottom-[calc(3.9rem+env(safe-area-inset-bottom))] z-30 block" : "hidden",
        )}
      >
        {location && (
          <div className="relative h-full overflow-hidden lg:rounded-3xl lg:border">
            <ShopMap
              className="h-full"
              center={location}
              user={location.source === "gps" ? location : null}
              radiusKm={radiusKm}
              points={points}
              highlighted={hoverShops}
              selected={selectedShop}
              onSelect={(id) => setSelectedShop(id === selectedShop ? null : id)}
            />
            {selected && (
              <div className="absolute inset-x-3 bottom-3 animate-in rounded-2xl border bg-card p-4 shadow-lift duration-200 fade-in slide-in-from-bottom-4">
                <button onClick={() => setSelectedShop(null)} className="absolute top-3 right-3 rounded-full p-1 text-muted-foreground hover:bg-muted" aria-label={t("discovery.search.close")}>
                  <X className="size-4" />
                </button>
                <div className="flex items-center gap-2 pr-8">
                  <Store className="size-4 text-muted-foreground" />
                  <p className="truncate font-semibold">{selected.name}</p>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 text-sm text-muted-foreground">
                  <span>{selected.locality}</span>
                  <Distance km={selected.distance_km} className="text-sm" />
                  <Rating value={selected.rating_avg} count={selected.rating_count} />
                  {selected.offers_delivery && (
                    <span className="inline-flex items-center gap-1 text-xs"><Bike className="size-3.5" /> {t("discovery.search.delivers")}</span>
                  )}
                </div>
                <p className="mt-2 text-sm">
                  {selected.in_stock_count > 0
                    ? t(`discovery.search.matchingInStock.${plural(language, selected.in_stock_count)}`, {
                        count: formatNumber(selected.in_stock_count),
                        price: formatPrice(selected.min_price),
                      })
                    : t("discovery.search.matchingOutOfStock")}
                </p>
                <Button asChild size="sm" variant="outline" className="mt-3">
                  <Link href={`/shops/${selected.slug}${q ? `?q=${encodeURIComponent(q)}` : ""}`}>{t("discovery.search.openShop")}</Link>
                </Button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* mobile list/map toggle */}
      <Button
        onClick={() => setMobileMap((v) => !v)}
        className="fixed bottom-[calc(4.6rem+env(safe-area-inset-bottom))] left-1/2 z-30 h-11 -translate-x-1/2 rounded-full px-5 shadow-lift lg:hidden"
      >
        {mobileMap ? (
          <>
            <List /> {t("discovery.search.list")}
          </>
        ) : (
          <>
            <MapIcon /> {t("discovery.search.map")}
          </>
        )}
      </Button>
    </div>
  );
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm whitespace-nowrap transition-colors",
        active ? "border-primary bg-primary text-primary-foreground" : "bg-card hover:bg-accent",
      )}
    >
      {children}
    </button>
  );
}
