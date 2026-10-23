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

// The build id (P7.12): NEARSHOP_BUILD_ID when the build sets one, else the Vercel deployment's id, else a fresh value
// per build. It versions the service worker (public/sw.js, registered as /sw.js?v=<build id> by
// src/components/site/sw-register.tsx), whose shell and page caches are named after it, so each deploy installs a new
// worker that drops the previous build's caches. A fresh value is written back to the environment, so the build's
// worker processes, which load this file again, use the same one.
const BUILD_ID =
  process.env.NEARSHOP_BUILD_ID?.trim() ||
  process.env.VERCEL_DEPLOYMENT_ID?.trim() ||
  `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
process.env.NEARSHOP_BUILD_ID = BUILD_ID;

// Content security policy (P2.6, plan section 8.13; every header and the reasons: docs/security/headers.md).
// Header-only, without nonces: nonces would make every page render dynamically, which turns off static output,
// ISR and CDN caching. So inline scripts, which hydration needs without nonces, are allowed, and eval only in
// next dev (React's debugging needs it). Every host comes from the build's environment:
// - the media domain: NEARSHOP_MEDIA_PUBLIC_BASE_URL (above), the same value the API has;
// - the map's tile host: NEARSHOP_MAP_TILE_ORIGIN, the origin of the map styles in
//   src/components/map/shop-map.tsx, whose style, tiles, sprites and glyphs all come from that one host;
// - the error tracker's ingest host and security-report endpoint: from NEXT_PUBLIC_SENTRY_DSN.
// NEARSHOP_CSP_MODE: report-only (the default until the 7 days of report-only mode on production are over)
// sends the policy as Content-Security-Policy-Report-Only, so nothing is blocked and violations are reported;
// enforce sends it as Content-Security-Policy. Reports go to Sentry in both modes when the DSN is set.
type CspMode = "report-only" | "enforce";
const CSP_MODES: readonly CspMode[] = ["report-only", "enforce"];
const DEV = process.env.NODE_ENV === "development";

function cspMode(): CspMode {
  const mode = (process.env.NEARSHOP_CSP_MODE?.trim().toLowerCase() || "report-only") as CspMode;
  if (!CSP_MODES.includes(mode)) throw new Error("NEARSHOP_CSP_MODE must be report-only or enforce");
  return mode;
}

function origin(name: string, value: string): string {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" && url.protocol !== "http:") throw new Error();
    return url.origin;
  } catch {
    throw new Error(`${name} must be an https:// (or, in development, http://) URL`);
  }
}

// The fallback is a deliberate, temporary exception to "hosts come from the environment": it mirrors the host
// that src/components/map/shop-map.tsx still hard-codes in its style URLs, so the policy always allows what the
// map really loads. When shop-map.tsx reads the same variable, remove the fallback and set the variable in
// Vercel for every environment (docs/security/headers.md, the hosts table).
const MAP_TILE_ORIGIN = origin(
  "NEARSHOP_MAP_TILE_ORIGIN",
  process.env.NEARSHOP_MAP_TILE_ORIGIN?.trim() || "https://tiles.openfreemap.org",
);

/** The Sentry ingest origin and its security-report URL for a DSN (https://<key>@<host>/<project id>). */
function sentryReporting(dsn: string): { origin: string; reportUri: string } | undefined {
  if (!dsn) return undefined;
  let url: URL;
  try {
    url = new URL(dsn);
  } catch {
    throw new Error("NEXT_PUBLIC_SENTRY_DSN is not a valid DSN");
  }
  const segments = url.pathname.split("/").filter(Boolean);
  const projectId = segments.pop();
  if (!url.username || !projectId) throw new Error("NEXT_PUBLIC_SENTRY_DSN is not a valid DSN");
  const query = new URLSearchParams({ sentry_key: url.username, sentry_environment: sentryEnvironment() });
  if (SENTRY_RELEASE) query.set("sentry_release", SENTRY_RELEASE);
  const prefix = segments.length ? `/${segments.join("/")}` : "";
  return { origin: url.origin, reportUri: `${url.origin}${prefix}/api/${projectId}/security/?${query}` };
}

const SENTRY_REPORTING = sentryReporting(process.env.NEXT_PUBLIC_SENTRY_DSN?.trim() ?? "");
const CSP_REPORT_GROUP = "csp";

function contentSecurityPolicy(mode: CspMode): string {
  const media = MEDIA_BASE_URL ? [origin("NEARSHOP_MEDIA_PUBLIC_BASE_URL", MEDIA_BASE_URL)] : [];
  const sentry = SENTRY_REPORTING ? [SENTRY_REPORTING.origin] : [];
  const directives: string[][] = [
    ["default-src", "'self'"],
    ["script-src", "'self'", "'unsafe-inline'", ...(DEV ? ["'unsafe-eval'"] : [])],
    ["style-src", "'self'", "'unsafe-inline'"],
    ["img-src", "'self'", "data:", "blob:", ...media, MAP_TILE_ORIGIN],
    ["connect-src", "'self'", MAP_TILE_ORIGIN, ...sentry],
    ["worker-src", "'self'", "blob:"],
    ["font-src", "'self'"],
    ["object-src", "'none'"],
    ["base-uri", "'self'"],
    ["form-action", "'self'"],
    ["frame-ancestors", "'none'"],
    // Only when enforcing (browsers ignore it in a report-only policy, with a console warning), and not in
    // next dev, which is plain http on localhost.
    ...(mode === "enforce" && !DEV ? [["upgrade-insecure-requests"]] : []),
    ...(SENTRY_REPORTING ? [["report-uri", SENTRY_REPORTING.reportUri], ["report-to", CSP_REPORT_GROUP]] : []),
  ];
  return directives.map((d) => [...new Set(d)].join(" ")).join("; ");
}

/** The site's headers on every page and static file (not /api or /media: the API sets its own). */
function siteHeaders(): { key: string; value: string }[] {
  const mode = cspMode();
  return [
    { key: "X-Content-Type-Options", value: "nosniff" },
    { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
    { key: "X-Frame-Options", value: "DENY" },
    { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(self)" },
    { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
    {
      key: mode === "enforce" ? "Content-Security-Policy" : "Content-Security-Policy-Report-Only",
      value: contentSecurityPolicy(mode),
    },
    ...(SENTRY_REPORTING
      ? [{ key: "Reporting-Endpoints", value: `${CSP_REPORT_GROUP}="${SENTRY_REPORTING.reportUri}"` }]
      : []),
  ];
}

const nextConfig: NextConfig = {
  reactCompiler: true,
  generateBuildId: () => BUILD_ID,
  // Off unless the build sets it to "true". A default is needed so the flag is always inlined as a
  // constant: an unset NEXT_PUBLIC_ variable is not, and the demo-account module would then ship.
  // The Sentry values have defaults for the same reason: always inlined, so a build without a DSN ships
  // error tracking switched off.
  env: {
    NEXT_PUBLIC_NEARSHOP_DEMO_MODE: process.env.NEXT_PUBLIC_NEARSHOP_DEMO_MODE ?? "false",
    NEXT_PUBLIC_SENTRY_DSN: process.env.NEXT_PUBLIC_SENTRY_DSN ?? "",
    NEXT_PUBLIC_SENTRY_ENVIRONMENT: sentryEnvironment(),
    NEXT_PUBLIC_SENTRY_RELEASE: SENTRY_RELEASE,
    NEXT_PUBLIC_NEARSHOP_BUILD_ID: BUILD_ID,
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
  // Strict-Transport-Security is left to Vercel, which sends a two-year max-age with includeSubDomains and
  // preload on every response (docs/security/headers.md). /api and /media are proxied to the API, whose
  // responses carry its own set (backend/app/core/security_headers.py), so they are left out here.
  //
  // The service worker (P7.12), as the Next.js progressive web apps guide sets it: never cached, so a deploy's new
  // worker is found on the next launch, and a policy of its own, which governs the worker: it may load and fetch
  // from this site only. The worker-src directive of the site policy above already allows a worker from the site.
  // It comes after the site's headers, so its own policy replaces the site's when that is enforced.
  async headers() {
    return [
      { source: "/((?!api(?:/|$)|media(?:/|$)).*)", headers: siteHeaders() },
      {
        source: "/sw.js",
        headers: [
          { key: "Content-Type", value: "application/javascript; charset=utf-8" },
          { key: "Cache-Control", value: "no-cache, no-store, must-revalidate" },
          { key: "Content-Security-Policy", value: "default-src 'self'; script-src 'self'" },
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
