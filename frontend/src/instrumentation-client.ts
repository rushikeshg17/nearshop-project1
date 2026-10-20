import { bufferEarlyErrors, loadSentry, loadWhenHidden, SENTRY_DSN, whenIdleAfterLoad } from "@/lib/sentry-browser";

/**
 * Browser error tracking and performance sampling, only when NEXT_PUBLIC_SENTRY_DSN was set at build time.
 * No session replay. Settings and scrubbing: src/lib/observability.ts.
 *
 * The SDK is loaded 1 s after the page has loaded, when the main thread is idle (src/lib/sentry-browser.ts), so
 * it never counts against the first-load JavaScript budget (docs/perf/frontend-budget.md). Until then, uncaught
 * errors and unhandled rejections are kept and load it at once, and hiding the page loads it at once, so early
 * errors and short visits are still reported. Web Vitals from real users: LCP, CLS and INP spans and the
 * pageload span's TTFB, from sampled page loads (10%, 25% on search), each tagged with the route template
 * (nearshop.route, never a full URL or query string) and the network's effective connection type
 * (network.connection.effective_type).
 */
let sdk: Awaited<ReturnType<typeof loadSentry>>;
if (SENTRY_DSN) {
  bufferEarlyErrors();
  loadWhenHidden();
  whenIdleAfterLoad(() => void loadSentry().then((loaded) => (sdk = loaded)));
}

/** Navigation spans for App Router transitions, once the SDK has loaded; a no-op while Sentry is off. */
export function onRouterTransitionStart(href: string, navigationType: string): void {
  sdk?.captureRouterTransitionStart(href, navigationType);
}
