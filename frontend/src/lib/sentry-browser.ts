/**
 * The browser's error tracker, loaded off the critical path (P2.9, docs/perf/frontend-budget.md).
 *
 * The Sentry SDK is about 85 KB gzip, as much as React DOM, so it is never in a page's first-load JavaScript:
 * src/instrumentation-client.ts calls loadSentry 3 s after the page's load event, when the main thread is idle,
 * and the error boundaries call it (through captureException) when they catch an error first. Nothing is loaded
 * when NEXT_PUBLIC_SENTRY_DSN was unset at build time.
 *
 * Starting late loses nothing that matters: the pageload span starts at the navigation's time origin, and the
 * Web Vitals observers read buffered entries (LCP, layout shifts, event timing, navigation timing). Errors
 * thrown before it loads, outside an error boundary, are not reported.
 */
import type * as SentryModule from "@sentry/nextjs";

type Sentry = typeof SentryModule;

// Inlined at build time (next.config.ts). The settings and scrubbing rules (src/lib/observability.ts) load
// with the SDK, so a page's first-load JavaScript holds only this module.
export const SENTRY_DSN = process.env.NEXT_PUBLIC_SENTRY_DSN || undefined;

let loading: Promise<Sentry | undefined> | undefined;

/** The initialised SDK, imported and set up on the first call; undefined when error tracking is off or the
 * SDK failed to load (error tracking must never break the site). */
export function loadSentry(): Promise<Sentry | undefined> {
  if (!SENTRY_DSN) return Promise.resolve(undefined);
  loading ??= Promise.all([import("@sentry/nextjs"), import("@/lib/observability")])
    .then(([sdk, { sentryOptions }]) => {
      sdk.init(sentryOptions());
      return sdk;
    })
    .catch(() => undefined);
  return loading;
}

/** Report an error caught by an error boundary (a no-op while error tracking is off). */
export function captureException(error: unknown): void {
  void loadSentry().then((sdk) => sdk?.captureException(error));
}

// How long after the load event the SDK waits before loading: past the page's largest paint and its first
// data on a slow phone, so its download and start-up never compete with them (docs/perf/frontend-budget.md).
const LOAD_DELAY_MS = 3000;

/** Run load LOAD_DELAY_MS after the page's load event, when the main thread is next idle (at most 5 s later). */
export function whenIdleAfterLoad(load: () => void): void {
  const idle = () =>
    "requestIdleCallback" in window ? window.requestIdleCallback(load, { timeout: 5000 }) : setTimeout(load, 1000);
  const later = () => setTimeout(idle, LOAD_DELAY_MS);
  if (document.readyState === "complete") later();
  else window.addEventListener("load", later, { once: true });
}
