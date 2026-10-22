"use client";

import { MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Smartphone, X } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ThemeProvider } from "next-themes";
import { Suspense, useEffect, useState } from "react";

import { useMe } from "@/hooks/use-session";
import { ApiError, errorMessage } from "@/lib/api";
import { LocationProvider } from "@/lib/location";

// The toast viewport is not first-load JavaScript (P2.9): it mounts 2 s after the page has loaded, when the
// browser is idle. A toast raised before then is not lost: sonner replays active toasts to a new Toaster.
const Toaster = dynamic(() => import("@/components/ui/sonner").then((m) => m.Toaster), { ssr: false });

function IdleToaster() {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    // 2 s after the load event, when the main thread is idle: past the page's first paint and data on a slow phone.
    let timer: ReturnType<typeof setTimeout> | undefined;
    const idle = () =>
      "requestIdleCallback" in window ? requestIdleCallback(() => setReady(true), { timeout: 3000 }) : setReady(true);
    const start = () => {
      timer = setTimeout(idle, 2000);
    };
    if (document.readyState === "complete") start();
    else window.addEventListener("load", start, { once: true });
    return () => {
      window.removeEventListener("load", start);
      clearTimeout(timer);
    };
  }, []);
  return ready ? <Toaster position="top-center" richColors closeButton /> : null;
}

// ---------------------------------------------------------------- the move to phone sign-in (P4.2)

// Pages a signed-in account without a verified phone may still open after the sunset: the verify screen itself, and
// the grievance page (support, for a number already verified on another account) with the legal pages it links to.
// Signing out is a button on every page, and on /verify-phone.
const OPEN_AFTER_SUNSET = ["/verify-phone", "/grievance", "/privacy", "/terms"];
const DISMISS_KEY = "nearshop.phone-banner-dismissed";

function localDay() {
  const d = new Date();
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
}

function dismissedToday() {
  try {
    return typeof window !== "undefined" && window.localStorage.getItem(DISMISS_KEY) === localDay();
  } catch {
    return false; // storage blocked: the banner shows, and dismissing hides it until the page is reloaded
  }
}

/** /auth/me says what to ask of the signed-in account (phone_migration): "warn", from the day phone sign-in is
 * available until the sunset, shows a banner on every page with the sunset date and a link to /verify-phone, which can
 * be dismissed for the day and blocks nothing; "required", from the sunset, sends every page to /verify-phone. Admins
 * and accounts with a verified phone get null: nothing. */
function PhoneMigration() {
  const { data: me } = useMe();
  const pathname = usePathname();
  const router = useRouter();
  const [dismissed, setDismissed] = useState(dismissedToday);
  const state = me?.phone_migration ?? null;
  const open = OPEN_AFTER_SUNSET.some((p) => pathname === p || pathname.startsWith(`${p}/`));

  useEffect(() => {
    if (state === "required" && !open) router.replace(`/verify-phone?next=${encodeURIComponent(pathname)}`);
  }, [state, open, pathname, router]);

  if (state !== "warn" || dismissed || pathname.startsWith("/verify-phone") || !me) return null;
  const sunset = new Date(`${me.email_login_sunset}T00:00:00Z`).toLocaleDateString("en-IN", {
    day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
  });
  return (
    <div role="region" aria-label="Verify your phone number" className="border-b border-info/30 bg-info-soft text-info-ink">
      <div className="mx-auto flex max-w-7xl items-center gap-3 px-4 py-2 text-sm md:px-6">
        <Smartphone className="size-4 shrink-0" aria-hidden />
        <p className="flex-1">
          From {sunset} you sign in with a code sent to your phone.{" "}
          <Link href="/verify-phone" className="font-medium underline underline-offset-4">Verify your phone number</Link>
        </p>
        <button
          type="button"
          className="grid size-8 shrink-0 place-items-center rounded-md hover:bg-info/10"
          aria-label="Dismiss for today"
          onClick={() => {
            setDismissed(true);
            try {
              window.localStorage.setItem(DISMISS_KEY, localDay());
            } catch {
              // storage blocked: dismissed for this page view only
            }
          }}
        >
          <X className="size-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            refetchOnWindowFocus: false,
            retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
          },
        },
        mutationCache: new MutationCache({
          // Mutations show their own error unless they opt out with meta.silent.
          // sonner is loaded here, not imported: it is not first-load JavaScript (P2.9).
          onError: (error, _v, _c, mutation) => {
            if (!mutation.meta?.silent) void import("sonner").then(({ toast }) => toast.error(errorMessage(error)));
          },
        }),
      }),
  );

  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
      <QueryClientProvider client={client}>
        {/* No MotionConfig and no TooltipProvider on every page (P2.9): shared screens animate with CSS, which
            globals.css stops for the reduced-motion setting. That rule does not reach motion's JavaScript
            animations, so each screen that still uses motion wraps itself in its own MotionConfig with
            reducedMotion "user"; a screen that uses a Radix tooltip wraps it in its own TooltipProvider. */}
        {/* usePathname inside: a Suspense boundary keeps every page prerendered. */}
        <Suspense fallback={null}>
          <PhoneMigration />
        </Suspense>
        <LocationProvider>{children}</LocationProvider>
        <IdleToaster />
      </QueryClientProvider>
    </ThemeProvider>
  );
}
