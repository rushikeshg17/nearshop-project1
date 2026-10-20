import { withSentryConfig } from "@sentry/nextjs/config";
import type { NextConfig } from "next";

type RemotePattern = Exclude<NonNullable<NonNullable<NextConfig["images"]>["remotePatterns"]>[number], URL>;

const API_ORIGIN = process.env.NEARSHOP_API_ORIGIN ?? "http://127.0.0.1:8000";

// Staging and production serve photos from their object-storage bucket's public domain (P1.5): the API
// returns absolute image URLs there, and this build allows that domain wherever images are restricted.
// Set NEARSHOP_MEDIA_PUBLIC_BASE_URL to the same value the API has; unset, photos come from /media.
const MEDIA_BASE_URL = process.env.NEARSHOP_MEDIA_PUBLIC_BASE_URL?.trim().replace(/\/+$/, "") || undefined;

function mediaPattern(base: string): RemotePattern {
  const url = new URL(base);
  // The deployed site is https, and browsers block or flag http images on an https page, so a production
  // build (next build) needs https; next dev also accepts http, matching the API in development and test.
  const allowHttp = process.env.NODE_ENV !== "production";
  if (url.protocol !== "https:" && !(allowHttp && url.protocol === "http:")) {
    throw new Error(
      allowHttp
        ? "NEARSHOP_MEDIA_PUBLIC_BASE_URL must be an https:// or http:// URL"
        : "NEARSHOP_MEDIA_PUBLIC_BASE_URL must be an https:// URL in a production build",
    );
  }
  return {
    protocol: url.protocol === "https:" ? "https" : "http",
    hostname: url.hostname,
    port: url.port,
    pathname: `${url.pathname.replace(/\/+$/, "")}/**`,
    search: "",
  };
}

// Error tracking (src/instrumentation.ts, src/instrumentation-client.ts, src/lib/observability.ts): on only
// when NEXT_PUBLIC_SENTRY_DSN is set in the build's environment (Vercel project settings). Events are tagged
// with the environment, staging for main's preview deployment and production for release, and carry the
// git commit as the release.
function sentryEnvironment(): string {
  if (process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT) return process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT;
  if (process.env.VERCEL_ENV === "production") return "production";
  if (process.env.VERCEL_ENV === "preview") return process.env.VERCEL_GIT_COMMIT_REF === "main" ? "staging" : "preview";
  return "development";
}
const SENTRY_RELEASE = process.env.VERCEL_GIT_COMMIT_SHA ?? "";

const nextConfig: NextConfig = {
  reactCompiler: true,
  // Off unless the build sets it to "true". A default is needed so the flag is always inlined as a
  // constant: an unset NEXT_PUBLIC_ variable is not, and the demo-account module would then ship.
  // The Sentry values have defaults for the same reason: always inlined, so a build without a DSN ships
  // error tracking switched off.
  env: {
    NEXT_PUBLIC_NEARSHOP_DEMO_MODE: process.env.NEXT_PUBLIC_NEARSHOP_DEMO_MODE ?? "false",
    NEXT_PUBLIC_SENTRY_DSN: process.env.NEXT_PUBLIC_SENTRY_DSN ?? "",
    NEXT_PUBLIC_SENTRY_ENVIRONMENT: sentryEnvironment(),
    NEXT_PUBLIC_SENTRY_RELEASE: SENTRY_RELEASE,
  },
  // next/image accepts remote images only from these patterns.
  images: { remotePatterns: MEDIA_BASE_URL ? [mediaPattern(MEDIA_BASE_URL)] : [] },
  // The browser only ever talks to this origin for the API, which is proxied, so the session cookie
  // stays first-party and no CORS preflight is needed. /media is the API's local photo store: development
  // and test, and any deployment whose API still has NEARSHOP_MEDIA_BACKEND=local.
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` },
      { source: "/media/:path*", destination: `${API_ORIGIN}/media/:path*` },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(self)" },
        ],
      },
    ];
  },
};

// Source maps reach Sentry only when SENTRY_AUTH_TOKEN, SENTRY_ORG and SENTRY_PROJECT are set in the build's
// environment (Vercel); without them nothing is uploaded and the build is otherwise unchanged. No tunnel
// route: the content security policy (P2.6) allows the Sentry ingest host instead.
export default withSentryConfig(nextConfig, {
  org: process.env.SENTRY_ORG,
  project: process.env.SENTRY_PROJECT,
  authToken: process.env.SENTRY_AUTH_TOKEN,
  silent: !process.env.CI,
  telemetry: false,
  widenClientFileUpload: true,
  release: { name: SENTRY_RELEASE || undefined, create: Boolean(process.env.SENTRY_AUTH_TOKEN) },
  sourcemaps: { disable: !process.env.SENTRY_AUTH_TOKEN, deleteSourcemapsAfterUpload: true },
});
