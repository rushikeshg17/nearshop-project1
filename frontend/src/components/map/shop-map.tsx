"use client";

import { Map as MapIcon } from "lucide-react";
import { lazy, Suspense, useEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

export interface MapPoint {
  id: number;
  lat: number;
  lng: number;
  label: string; // short text in the pin, e.g. a price
  title: string; // accessible name
  muted?: boolean; // e.g. only out-of-stock matches
}

export interface ShopMapProps {
  center: { lat: number; lng: number };
  points: MapPoint[];
  radiusKm?: number;
  user?: { lat: number; lng: number } | null;
  highlighted?: number[] | null;
  selected?: number | null;
  onSelect?: (id: number) => void;
  onMapClick?: (pos: { lat: number; lng: number }) => void;
  fitToPoints?: boolean;
  zoom?: number;
  className?: string;
  interactive?: boolean;
}

// The map itself (map-canvas.tsx), MapLibre (about 300 KB gzip), its style and its tiles.
const MapCanvas = lazy(() => import("@/components/map/map-canvas").then((m) => ({ default: m.MapCanvas })));

/** Run load once the page has loaded and the main thread is idle (at most 2 s later). Returns a cancel. */
function afterLoadWhenIdle(load: () => void): () => void {
  let idleId: number | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const idle = () => {
    if ("requestIdleCallback" in window) idleId = window.requestIdleCallback(load, { timeout: 2000 });
    else timer = setTimeout(load, 300);
  };
  if (document.readyState === "complete") idle();
  else window.addEventListener("load", idle, { once: true });
  return () => {
    window.removeEventListener("load", idle);
    if (idleId !== undefined) window.cancelIdleCallback(idleId);
    if (timer !== undefined) clearTimeout(timer);
  };
}

/**
 * OpenStreetMap vector map of shops (map-canvas.tsx), loaded only when it is wanted (P2.9,
 * docs/perf/frontend-budget.md): when it scrolls into view, once the page has loaded and the browser is idle, or
 * at once when the customer taps "Show map". MapLibre's code, style and tiles cost a phone on slow 4G several
 * hundred kilobytes and its start-up blocks the main thread. An interactive map (search, shops), the page's
 * main feature, starts loading when it comes within 200 px of the screen. A small static map (interactive
 * false: a shop's, a reservation's or an order's location) loads when the customer has scrolled the page and
 * it is on screen: one already on screen at load (the shop page's, on a phone) waits for the first scroll or
 * the tap, so its start-up never lands on the visitor's first interactions. Until then a placeholder fills the
 * same frame, so nothing shifts. A map hidden with display: none (the search page's map on a phone, until its
 * Map button is tapped) never intersects, so it loads when it is shown.
 */
export function ShopMap({ className, ...props }: ShopMapProps) {
  const staticMap = props.interactive === false;
  const frame = useRef<HTMLDivElement>(null);
  // waiting: off screen; near: on or near the screen, waiting for the page to load and go idle; show: load it.
  const [stage, setStage] = useState<"waiting" | "near" | "show">("waiting");
  // A static map watches the screen only once the page has been scrolled.
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    if (!staticMap || scrolled) return;
    const onScroll = () => setScrolled(true);
    window.addEventListener("scroll", onScroll, { passive: true, once: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [staticMap, scrolled]);

  // Watch for the map coming near the screen.
  useEffect(() => {
    const el = frame.current;
    if (!el || stage !== "waiting" || (staticMap && !scrolled)) return;
    if (!("IntersectionObserver" in window)) return afterLoadWhenIdle(() => setStage("show"));
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          observer.disconnect();
          setStage("near");
        }
      },
      { rootMargin: staticMap ? "0px" : "200px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [stage, staticMap, scrolled]);

  // Near the screen: start once the page has loaded and the browser is idle.
  useEffect(() => {
    if (stage === "near") return afterLoadWhenIdle(() => setStage("show"));
  }, [stage]);

  return (
    <div ref={frame} className={cn("relative overflow-hidden bg-muted", className)}>
      {stage === "show" ? (
        <Suspense fallback={<div className="absolute inset-0 animate-pulse bg-muted" aria-hidden />}>
          <MapCanvas {...props} />
        </Suspense>
      ) : (
        <div className="absolute inset-0 grid place-items-center">
          <button
            type="button"
            onClick={() => setStage("show")}
            className="inline-flex items-center gap-1.5 rounded-full border bg-card px-3 py-1.5 text-sm font-medium shadow-soft transition-colors hover:bg-accent focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
          >
            <MapIcon className="size-4 text-brand" aria-hidden /> Show map
          </button>
        </div>
      )}
    </div>
  );
}
