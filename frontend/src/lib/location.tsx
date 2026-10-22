"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { useMeta } from "@/hooks/use-session";
import type { Translate } from "@/lib/i18n";

export type LocationSource = "gps" | "locality" | "city";

export interface UserLocation {
  lat: number;
  lng: number;
  label: string;
  source: LocationSource;
}

interface LocationContextValue {
  location: UserLocation | null;
  radiusKm: number;
  ready: boolean;
  setLocation: (loc: UserLocation) => void;
  setRadiusKm: (km: number) => void;
  requestGps: () => Promise<{ ok: boolean; message?: string }>;
}

const STORAGE_KEY = "nearshop.location.v1";
const RADIUS_KEY = "nearshop.radius.v1";
const MAX_DISTANCE_FROM_CITY_KM = 40;

const LocationContext = createContext<LocationContextValue | null>(null);

function readStorage<T>(key: string): T | null {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: unknown) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage unavailable (private mode); location still works for this session
  }
}

function distanceKm(a: { lat: number; lng: number }, b: { lat: number; lng: number }) {
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(h));
}

/** t: the interface language's translator (Providers passes it): the labels of the GPS and city-centre locations and
 * the GPS messages are in that language (P7.10). A locality's label is its name, as the city data spells it. */
export function LocationProvider({ t, children }: { t: Translate; children: React.ReactNode }) {
  const { data: meta } = useMeta();
  const [stored, setStored] = useState<UserLocation | null>(null);
  const [radiusKm, setRadius] = useState(5);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time hydration from localStorage
    setStored(readStorage<UserLocation>(STORAGE_KEY));
    const r = readStorage<number>(RADIUS_KEY);
    if (r) setRadius(r);
    setHydrated(true);
  }, []);

  const fallback = useMemo<UserLocation | null>(
    () =>
      meta
        ? { lat: meta.city.center.lat, lng: meta.city.center.lng, label: `${meta.city.name} centre`, source: "city" }
        : null,
    [meta],
  );

  // The GPS and city-centre labels are named in the interface language when shown, whatever language they were
  // stored in, so switching language renames them too.
  const location = useMemo<UserLocation | null>(() => {
    const raw = stored ?? fallback;
    if (raw?.source === "gps") return { ...raw, label: t("common.location.current") };
    if (raw?.source === "city" && meta) return { ...raw, label: t("common.location.cityCentre", { city: meta.city.name }) };
    return raw;
  }, [stored, fallback, meta, t]);

  const setLocation = useCallback((loc: UserLocation) => {
    setStored(loc);
    writeStorage(STORAGE_KEY, loc);
  }, []);

  const setRadiusKm = useCallback((km: number) => {
    setRadius(km);
    writeStorage(RADIUS_KEY, km);
  }, []);

  const requestGps = useCallback(async () => {
    if (!("geolocation" in navigator)) return { ok: false, message: t("common.location.gpsUnsupported") };
    return new Promise<{ ok: boolean; message?: string }>((resolve) => {
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          const here = { lat: pos.coords.latitude, lng: pos.coords.longitude };
          if (meta && distanceKm(here, meta.city.center) > MAX_DISTANCE_FROM_CITY_KM) {
            resolve({
              ok: false,
              message: t("common.location.gpsOutside", { city: meta.city.name }),
            });
            return;
          }
          setLocation({ ...here, label: "Current location", source: "gps" });
          resolve({ ok: true });
        },
        (err) =>
          resolve({
            ok: false,
            message: t(err.code === err.PERMISSION_DENIED ? "common.location.gpsDenied" : "common.location.gpsFailed"),
          }),
        { enableHighAccuracy: true, timeout: 10_000, maximumAge: 60_000 },
      );
    });
  }, [meta, setLocation, t]);

  const value = useMemo(
    () => ({ location, radiusKm, ready: hydrated && !!location, setLocation, setRadiusKm, requestGps }),
    [location, radiusKm, hydrated, setLocation, setRadiusKm, requestGps],
  );

  return <LocationContext.Provider value={value}>{children}</LocationContext.Provider>;
}

export function useLocation() {
  const ctx = useContext(LocationContext);
  if (!ctx) throw new Error("useLocation must be used inside LocationProvider");
  return ctx;
}
