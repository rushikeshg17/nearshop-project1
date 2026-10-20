"use client";

import { MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { ThemeProvider } from "next-themes";
import { useEffect, useState } from "react";

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
        <LocationProvider>{children}</LocationProvider>
        <IdleToaster />
      </QueryClientProvider>
    </ThemeProvider>
  );
}
