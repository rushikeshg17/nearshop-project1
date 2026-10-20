import * as Sentry from "@sentry/nextjs";

import { SENTRY_DSN, sentryOptions } from "@/lib/observability";

/**
 * Browser error tracking and performance sampling (Web Vitals included), set up before hydration, only
 * when NEXT_PUBLIC_SENTRY_DSN was set at build time. No session replay. Settings and scrubbing:
 * src/lib/observability.ts.
 */
if (SENTRY_DSN) {
  try {
    Sentry.init(sentryOptions());
  } catch {
    // error tracking must never stop the site from starting
  }
}

// Navigation spans for App Router transitions. A no-op while Sentry is off.
export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
