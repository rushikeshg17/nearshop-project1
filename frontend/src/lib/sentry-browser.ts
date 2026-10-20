/**
 * The browser's error tracker, loaded off the critical path (P2.9, docs/perf/frontend-budget.md).
 *
 * The Sentry SDK is about 150 KB gzip (twice React DOM), so it is never in a page's first-load JavaScript:
 * src/instrumentation-client.ts calls loadSentry 1 s after the page's load event, when the main thread is idle
 * (at most 2 s later), and earlier when it is needed sooner:
 * - an uncaught error or unhandled promise rejection: bufferEarlyErrors keeps it and starts loading the SDK at
 *   once, and loadSentry reports it as the SDK's own handlers would have, once the SDK has started;
 * - an error caught by an error boundary (captureException);
 * - the page being hidden (the visitor switches app or tab, or leaves): loadWhenHidden starts loading at once,
 *   and loadSentry then lets the SDK send the Web Vitals it would have sent on hiding.
 * Nothing is loaded when NEXT_PUBLIC_SENTRY_DSN was unset at build time.
 *
 * Starting late loses little: the pageload span starts at the navigation's time origin, and the Web Vitals
 * observers read buffered entries (LCP, layout shifts, event timing, navigation timing). What is still lost:
 * a visit whose page is unloaded before the SDK's download finishes, and interactions under 104 ms before the
 * SDK started (the browser buffers only longer ones), which can only make INP read low.
 */
import type * as SentryModule from "@sentry/nextjs";

type Sentry = typeof SentryModule;

// Inlined at build time (next.config.ts). The settings and scrubbing rules (src/lib/observability.ts) load
// with the SDK, so a page's first-load JavaScript holds only this module.
export const SENTRY_DSN = process.env.NEXT_PUBLIC_SENTRY_DSN || undefined;

let loading: Promise<Sentry | undefined> | undefined;
let started = false;

// Uncaught errors and unhandled rejections from before the SDK started (bufferEarlyErrors), at most
// MAX_EARLY_ERRORS of them: a page that throws in a loop must not hold an unbounded queue.
const MAX_EARLY_ERRORS = 20;
type EarlyError = { error: unknown; mechanism: "onerror" | "onunhandledrejection" };
const earlyErrors: EarlyError[] = [];
let stopBuffering: (() => void) | undefined;
// The page was hidden before the SDK started, so the SDK missed the visibilitychange it reports Web Vitals on.
let hiddenBeforeStart = false;

/** The initialised SDK, imported and set up on the first call; undefined when error tracking is off or the
 * SDK failed to load (error tracking must never break the site). */
export function loadSentry(): Promise<Sentry | undefined> {
  if (!SENTRY_DSN) return Promise.resolve(undefined);
  loading ??= Promise.all([import("@sentry/nextjs"), import("@/lib/observability")])
    .then(([sdk, { sentryOptions }]) => {
      sdk.init({
        ...sentryOptions(),
        // Web Vitals per page load only, reported when the page is hidden or the visitor navigates. The SDK's
        // default also measures back/forward-cache restores and soft navigations separately, and then reports
        // LCP only on a trusted click, key press or visibilitychange, which a page hidden before the SDK
        // started never gets again (replayHide). The field budget is on page loads.
        integrations: [sdk.webVitalsIntegration({ softNavigations: false, bfcacheNavigations: false })],
      });
      started = true;
      // From here the SDK's own global handlers report errors: stop buffering, then report the early ones as
      // those handlers would have (unhandled, with the same mechanism), in the same task, so none is missed
      // or reported twice.
      stopBuffering?.();
      for (const { error, mechanism } of earlyErrors.splice(0)) {
        sdk.captureException(error, { mechanism: { type: `auto.browser.global_handlers.${mechanism}`, handled: false } });
      }
      if (hiddenBeforeStart) replayHide();
      return sdk;
    })
    .catch(() => {
      stopBuffering?.();
      earlyErrors.length = 0;
      return undefined;
    });
  return loading;
}

/** The page was hidden before the SDK started and may still be: replay the visibilitychange the SDK missed, so
 * it sends this page's LCP, CLS and INP and ends the pageload span exactly as it does when a page it watches is
 * hidden. Not at once: the SDK's performance observers first receive the page's buffered entries, in a task
 * of their own. An observer registered after theirs is called after them in that task, so its callback is the
 * signal; a page with no largest paint (or a browser without the entry type) waits at most 1 s. */
function replayHide(): void {
  let done = false;
  let observer: PerformanceObserver | undefined;
  const fire = () => {
    if (done) return;
    done = true;
    observer?.disconnect();
    if (document.visibilityState === "hidden") document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
  };
  try {
    observer = new PerformanceObserver(() => setTimeout(fire, 0));
    observer.observe({ type: "largest-contentful-paint", buffered: true });
  } catch {
    // the entry type is not supported: the timeout below is enough
  }
  setTimeout(fire, 1000);
}

/** Report an error caught by an error boundary (a no-op while error tracking is off). */
export function captureException(error: unknown): void {
  void loadSentry().then((sdk) => sdk?.captureException(error));
}

/** Until the SDK has started, keep uncaught errors and unhandled promise rejections, and start loading the SDK
 * at the first one. Call once, as early as possible (src/instrumentation-client.ts). */
export function bufferEarlyErrors(): void {
  if (!SENTRY_DSN || stopBuffering) return;
  const keep = (error: unknown, mechanism: EarlyError["mechanism"]) => {
    if (earlyErrors.length < MAX_EARLY_ERRORS) earlyErrors.push({ error, mechanism });
    void loadSentry();
  };
  // A script error is an ErrorEvent; a failed image or script download is a plain Event that does not
  // bubble to window, so it never reaches this listener (the SDK ignores those too).
  const onError = (event: ErrorEvent) => keep(event.error ?? event.message, "onerror");
  const onRejection = (event: PromiseRejectionEvent) => keep(event.reason, "onunhandledrejection");
  window.addEventListener("error", onError);
  window.addEventListener("unhandledrejection", onRejection);
  stopBuffering = () => {
    window.removeEventListener("error", onError);
    window.removeEventListener("unhandledrejection", onRejection);
  };
}

/** Start loading the SDK at once when the page is hidden before it has loaded (the visitor switches app or
 * tab, or leaves). A phone keeps a hidden page running for a while, so the SDK usually still starts and sends
 * the visit's Web Vitals; a page being unloaded may not finish the download. */
export function loadWhenHidden(): void {
  if (!SENTRY_DSN) return;
  const onHide = () => {
    if (document.visibilityState !== "hidden") return;
    window.removeEventListener("visibilitychange", onHide, true);
    window.removeEventListener("pagehide", onHide, true);
    if (started) return; // the SDK saw this hide itself
    hiddenBeforeStart = true;
    void loadSentry();
  };
  window.addEventListener("visibilitychange", onHide, true);
  window.addEventListener("pagehide", onHide, true);
}

// How long after the load event the SDK waits before loading: past the page's largest paint on a slow phone,
// short enough that most visits are covered from their start (docs/perf/frontend-budget.md).
const LOAD_DELAY_MS = 1000;
const IDLE_TIMEOUT_MS = 2000;

/** Run load LOAD_DELAY_MS after the page's load event, when the main thread is next idle (at most
 * IDLE_TIMEOUT_MS later). */
export function whenIdleAfterLoad(load: () => void): void {
  const idle = () =>
    "requestIdleCallback" in window ? window.requestIdleCallback(load, { timeout: IDLE_TIMEOUT_MS }) : setTimeout(load, 300);
  const later = () => setTimeout(idle, LOAD_DELAY_MS);
  if (document.readyState === "complete") later();
  else window.addEventListener("load", later, { once: true });
}
