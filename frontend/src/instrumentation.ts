import * as Sentry from "@sentry/nextjs";

import { SENTRY_DSN, sentryOptions } from "@/lib/observability";

/**
 * Server-side error tracking (Next.js instrumentation, src/ folder convention): Sentry for the Node.js
 * and edge runtimes, only when NEXT_PUBLIC_SENTRY_DSN was set at build time. Settings and scrubbing:
 * src/lib/observability.ts.
 */
export function register() {
  if (!SENTRY_DSN) return;
  if (process.env.NEXT_RUNTIME === "nodejs" || process.env.NEXT_RUNTIME === "edge") {
    Sentry.init(sentryOptions());
  }
}

// Errors in Server Components, route handlers, server actions and the proxy. A no-op while Sentry is off.
export const onRequestError = Sentry.captureRequestError;
