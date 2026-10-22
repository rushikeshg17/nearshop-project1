import type { Metadata } from "next";
import { cookies, headers } from "next/headers";
import { notFound } from "next/navigation";

import { AiLabView } from "./ai-lab-view";

export const metadata: Metadata = { title: "AI Lab", description: "How NearShop uses machine learning, with live metrics." };

// Who may see the AI Lab (P7.1) is the API's decision: everyone in the demo deployment and in development, where
// the data is synthetic; only admins in staging and production (NEARSHOP_AI_LAB_PUBLIC, backend/app/api/routes/ai.py).
// So before rendering, the page asks GET /api/ai/access with the visitor's session cookie, and answers 404 when the
// API does, or when the API cannot be asked: a production visitor never sees the lab, not even its shell. The API
// itself refuses every /api/ai route to the same visitors, so this check only spares them an empty page.
const API_ORIGIN = (process.env.NEARSHOP_API_ORIGIN ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
const SESSION_COOKIE = "nearshop_session";
const EDGE_HEADER = "x-nearshop-edge";
const CLIENT_IP_HEADER = "x-nearshop-client-ip";

async function labAllowed(): Promise<boolean> {
  const requestHeaders: Record<string, string> = { accept: "application/json" };
  const session = (await cookies()).get(SESSION_COOKIE);
  if (session) requestHeaders.cookie = `${SESSION_COOKIE}=${session.value}`;
  // As src/lib/server-api.ts: the edge secret that staging and production require, and with it the visitor's
  // address for the API's per-IP limits.
  const secret = process.env.NEARSHOP_EDGE_SECRET;
  if (secret) {
    requestHeaders[EDGE_HEADER] = secret;
    const h = await headers();
    const ip = h.get("x-real-ip")?.trim() || h.get("x-forwarded-for")?.split(",")[0]?.trim();
    if (ip) requestHeaders[CLIENT_IP_HEADER] = ip;
  }
  try {
    const res = await fetch(`${API_ORIGIN}/api/ai/access`, {
      headers: requestHeaders,
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

export default async function AiLabPage() {
  if (!(await labAllowed())) notFound();
  return <AiLabView />;
}
