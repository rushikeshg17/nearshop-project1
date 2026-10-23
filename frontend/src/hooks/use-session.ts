"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import { api } from "@/lib/api";
import type { Meta, Reservation, User } from "@/lib/types";

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: () => api.get<User | null>("/auth/me"),
    staleTime: 5 * 60_000,
  });
}

export function useMeta() {
  return useQuery({
    queryKey: ["meta"],
    queryFn: () => api.get<Meta>("/meta"),
    staleTime: 10 * 60_000,
  });
}

export function useSignOut() {
  const qc = useQueryClient();
  const router = useRouter();
  return async () => {
    // Stop the page's own requests first, so none that started while signed in stores a reservation afterwards.
    await qc.cancelQueries();
    await api.post("/auth/logout").catch(() => undefined);
    qc.clear();
    qc.setQueryData(["me"], null);
    await clearOfflineReservations();
    router.push("/");
    router.refresh();
  };
}

// ------------------------------------------------------------------ offline reservation codes (P7.12, public/sw.js)
//
// The signed-in customer's open reservations are kept on the phone so that the code shows at the counter without a
// network: one entry per reservation in the nearshop-reservations cache, under its API path, holding only what the
// offline view shows. The reservation page writes the entry when it loads an open reservation and removes it when
// the reservation closes; sign-out removes every entry. The worker reads them for its offline screen.

/** The cache the worker (public/sw.js) reads the stored reservations from. */
const OFFLINE_RESERVATIONS = "nearshop-reservations";
const OPEN_STATUSES: readonly string[] = ["REQUESTED", "CONFIRMED", "READY_FOR_PICKUP"];

/** What is stored of an open reservation: the code, the item and where to collect it, and when it was saved. */
export type OfflineReservation = Pick<Reservation, "id" | "code" | "status" | "quantity" | "expires_at"> & {
  product: Pick<Reservation["product"], "name">;
  shop: Pick<Reservation["shop"], "name" | "address_line" | "hours_label">;
  /** When this copy was saved (ISO 8601). */
  saved_at: string;
};

export function isOpenReservation(status: string): boolean {
  return OPEN_STATUSES.includes(status);
}

function offlineKey(id: number) {
  return `/api/reservations/${id}`;
}

function offlineCacheAvailable() {
  return typeof window !== "undefined" && "caches" in window;
}

// Writes, removals and sign-out's clearing run one after another, in the order asked, so a write that was asked for
// before sign-out never lands after it.
let offlineQueue: Promise<unknown> = Promise.resolve();
function queueOffline<T>(task: () => Promise<T>): Promise<T | undefined> {
  const next = offlineQueue.then(task, task).catch(() => undefined);
  offlineQueue = next;
  return next;
}

/** Store an open reservation for offline use, or remove it once it has closed. */
export function storeOfflineReservation(r: Reservation) {
  if (!offlineCacheAvailable()) return Promise.resolve(undefined);
  if (!isOpenReservation(r.status)) return forgetOfflineReservation(r.id);
  const entry: OfflineReservation = {
    id: r.id,
    code: r.code,
    status: r.status,
    quantity: r.quantity,
    expires_at: r.expires_at,
    product: { name: r.product.name },
    shop: { name: r.shop.name, address_line: r.shop.address_line, hours_label: r.shop.hours_label },
    saved_at: new Date().toISOString(),
  };
  return queueOffline(async () => {
    const cache = await caches.open(OFFLINE_RESERVATIONS);
    await cache.put(offlineKey(r.id), new Response(JSON.stringify(entry), { headers: { "Content-Type": "application/json" } }));
  });
}

export function forgetOfflineReservation(id: number) {
  if (!offlineCacheAvailable()) return Promise.resolve(undefined);
  return queueOffline(async () => {
    if (!(await caches.has(OFFLINE_RESERVATIONS))) return;
    await (await caches.open(OFFLINE_RESERVATIONS)).delete(offlineKey(id));
  });
}

export async function readOfflineReservation(id: number): Promise<OfflineReservation | null> {
  if (!offlineCacheAvailable()) return null;
  try {
    if (!(await caches.has(OFFLINE_RESERVATIONS))) return null;
    const entry = await (await caches.open(OFFLINE_RESERVATIONS)).match(offlineKey(id));
    return entry ? ((await entry.json()) as OfflineReservation) : null;
  } catch {
    return null;
  }
}

/** Sign-out (here, and every other way the account signs out of this browser: src/app/(site)/account/account-view.tsx):
 * the worker deletes every stored reservation and every saved /account page. The page also deletes them itself, for a
 * browser where the worker is not controlling this page (a first visit, a hard reload) or does not answer. */
export function clearOfflineReservations() {
  return queueOffline(async () => {
    const worker = typeof navigator !== "undefined" ? navigator.serviceWorker?.controller : null;
    if (worker) {
      await new Promise<void>((resolve) => {
        const channel = new MessageChannel();
        const timer = setTimeout(resolve, 2_000);
        channel.port1.onmessage = () => {
          clearTimeout(timer);
          resolve();
        };
        worker.postMessage({ type: "sign-out" }, [channel.port2]);
      });
    }
    if (!offlineCacheAvailable()) return;
    await caches.delete(OFFLINE_RESERVATIONS);
    // The worker's saved pages (nearshop-pages-<build>, public/sw.js): every /account page in any version's cache.
    for (const name of await caches.keys()) {
      if (!name.startsWith("nearshop-pages-")) continue;
      const pages = await caches.open(name);
      for (const key of await pages.keys()) {
        if (/^\/account(\/|$)/.test(new URL(key.url).pathname)) await pages.delete(key);
      }
    }
  });
}

export function homeFor(user: User | null | undefined) {
  if (!user) return "/";
  if (user.role === "owner") return "/shop";
  if (user.role === "staff") return "/shop/reservations"; // P9.13: the overview shows the shop's sales, owner only
  if (user.role === "admin") return "/admin";
  return "/account";
}
