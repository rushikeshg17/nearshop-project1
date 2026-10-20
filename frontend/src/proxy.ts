import { NextResponse, type NextRequest } from "next/server";

const SESSION_COOKIE = "nearshop_session";

// Edge-only API access (P2.3, backend/app/core/ratelimit.py). In staging and production the API answers 403
// to any request without the edge secret, so the browser's /api and /media requests, rewritten to
// NEARSHOP_API_ORIGIN by next.config.ts, carry it from here. NEARSHOP_EDGE_SECRET is a server-only variable
// (never NEXT_PUBLIC_): it is read at request time on the server and never reaches the browser.
const EDGE_HEADER = "x-nearshop-edge";
const CLIENT_IP_HEADER = "x-nearshop-client-ip";

/** The visitor's address as Vercel gives it to the proxy: x-real-ip, else the first X-Forwarded-For entry
 * (Vercel overwrites both with the client's address). Undefined when there is none, as in local development,
 * where the API counts the TCP peer instead. */
function clientAddress(request: NextRequest): string | undefined {
  const realIp = request.headers.get("x-real-ip")?.trim();
  if (realIp) return realIp;
  return request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || undefined;
}

function forwardToApi(request: NextRequest): NextResponse {
  const headers = new Headers(request.headers);
  // A visitor's own copies are never passed on: only this proxy speaks for the edge.
  headers.delete(EDGE_HEADER);
  headers.delete(CLIENT_IP_HEADER);
  const secret = process.env.NEARSHOP_EDGE_SECRET;
  if (secret) {
    headers.set(EDGE_HEADER, secret);
    const ip = clientAddress(request);
    if (ip) headers.set(CLIENT_IP_HEADER, ip);
  }
  // The changed request headers go upstream, to the rewrite destination; none of them reach the browser.
  return NextResponse.next({ request: { headers } });
}

/**
 * /api and /media: add the edge headers on the way to the API (above).
 * Private areas: fast redirect to sign-in when there is no session cookie at all. This is a UX guard only;
 * the API enforces authentication and roles on every request.
 */
export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (pathname.startsWith("/api/") || pathname === "/api" || pathname.startsWith("/media/")) {
    return forwardToApi(request);
  }
  if (request.cookies.has(SESSION_COOKIE)) return NextResponse.next();
  const url = new URL("/login", request.url);
  url.searchParams.set("next", request.nextUrl.pathname + request.nextUrl.search);
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/api/:path*", "/media/:path*", "/account/:path*", "/shop/:path*", "/admin/:path*"],
};
