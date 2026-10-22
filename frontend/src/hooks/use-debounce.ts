"use client";

import { useEffect, useState, useSyncExternalStore } from "react";

export function useDebounce<T>(value: T, delay = 200) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

type Clock = { subscribe: (onChange: () => void) => () => void; read: () => number };

/** One clock per interval, shared by every component on the page that asks for it: a queue of fifty cards runs one
 * timer, not fifty. It ticks only while something is subscribed. */
const clocks = new Map<number, Clock>();

function makeClock(intervalMs: number): Clock {
  let now = Date.now();
  let timer: ReturnType<typeof setInterval> | null = null;
  const listeners = new Set<() => void>();
  return {
    subscribe(onChange) {
      listeners.add(onChange);
      if (!timer) {
        now = Date.now();
        timer = setInterval(() => {
          now = Date.now();
          for (const listener of listeners) listener();
        }, intervalMs);
      }
      return () => {
        listeners.delete(onChange);
        if (!listeners.size && timer) {
          clearInterval(timer);
          timer = null;
        }
      };
    },
    read() {
      // An idle clock (the server, or before the first subscriber) is brought up to date at most once per interval,
      // so successive reads within one render agree.
      if (!timer && Date.now() - now >= intervalMs) now = Date.now();
      return now;
    },
  };
}

/** Re-render on an interval so relative times ("2 min ago") and countdowns stay current. */
export function useNow(intervalMs = 30_000) {
  let clock = clocks.get(intervalMs);
  if (!clock) {
    clock = makeClock(intervalMs);
    clocks.set(intervalMs, clock);
  }
  return useSyncExternalStore(clock.subscribe, clock.read, clock.read);
}
