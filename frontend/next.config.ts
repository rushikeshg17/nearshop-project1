import type { NextConfig } from "next";

type RemotePattern = Exclude<NonNullable<NonNullable<NextConfig["images"]>["remotePatterns"]>[number], URL>;

const API_ORIGIN = process.env.NEARSHOP_API_ORIGIN ?? "http://127.0.0.1:8000";

// Staging and production serve photos from their object-storage bucket's public domain (P1.5): the API
// returns absolute image URLs there, and this build allows that domain wherever images are restricted.
// Set NEARSHOP_MEDIA_PUBLIC_BASE_URL to the same value the API has; unset, photos come from /media.
const MEDIA_BASE_URL = process.env.NEARSHOP_MEDIA_PUBLIC_BASE_URL?.trim().replace(/\/+$/, "") || undefined;

function mediaPattern(base: string): RemotePattern {
  const url = new URL(base);
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error("NEARSHOP_MEDIA_PUBLIC_BASE_URL must be an https:// URL");
  }
  return {
    protocol: url.protocol === "https:" ? "https" : "http",
    hostname: url.hostname,
    port: url.port,
    pathname: `${url.pathname.replace(/\/+$/, "")}/**`,
    search: "",
  };
}

const nextConfig: NextConfig = {
  reactCompiler: true,
  // Off unless the build sets it to "true". A default is needed so the flag is always inlined as a
  // constant: an unset NEXT_PUBLIC_ variable is not, and the demo-account module would then ship.
  env: { NEXT_PUBLIC_NEARSHOP_DEMO_MODE: process.env.NEXT_PUBLIC_NEARSHOP_DEMO_MODE ?? "false" },
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

export default nextConfig;
