"use client";

import { WifiOff } from "lucide-react";
import { useEffect, useSyncExternalStore } from "react";

import { useI18n } from "@/components/providers";

// The installable app (P7.12). Registers the service worker (public/sw.js) for this build and shows a notice while the
// browser is offline. The worker's URL carries the build id (next.config.ts), so each deploy installs a new worker,
// which replaces the old one at once: the installed app runs the new version by its second launch at the latest.
// Only in production builds: next dev rebuilds files under the same names, which a cache-first worker would hide.
// There is no install button: Chrome makes its own install offer from the manifest (src/app/manifest.ts).
const BUILD_ID = process.env.NEXT_PUBLIC_NEARSHOP_BUILD_ID;
const ENABLED = process.env.NODE_ENV === "production" && Boolean(BUILD_ID);

function subscribe(onChange: () => void) {
  window.addEventListener("online", onChange);
  window.addEventListener("offline", onChange);
  return () => {
    window.removeEventListener("online", onChange);
    window.removeEventListener("offline", onChange);
  };
}

export function ServiceWorkerRegister() {
  const { t, info } = useI18n();
  const offline = useSyncExternalStore(
    subscribe,
    () => !navigator.onLine,
    () => false,
  );

  useEffect(() => {
    if (!ENABLED || !("serviceWorker" in navigator)) return;
    // After the load event, so the worker's precache never competes with the page's own first load (P2.9 budgets).
    const register = () => {
      navigator.serviceWorker
        .register(`/sw.js?v=${encodeURIComponent(BUILD_ID!)}`, { scope: "/", updateViaCache: "none" })
        .then((registration) => registration.update().catch(() => undefined))
        .catch(() => undefined);
    };
    if (document.readyState === "complete") {
      register();
      return;
    }
    window.addEventListener("load", register, { once: true });
    return () => window.removeEventListener("load", register);
  }, []);

  // The worker's offline screen speaks the interface language: send it the text whenever the language changes.
  useEffect(() => {
    if (!ENABLED || !("serviceWorker" in navigator)) return;
    let cancelled = false;
    navigator.serviceWorker.ready
      .then((registration) => {
        if (cancelled) return;
        registration.active?.postMessage({
          type: "offline-strings",
          lang: info.htmlLang,
          strings: {
            title: t("shell.offline.title"),
            body: t("shell.offline.body"),
            retry: t("shell.offline.retry"),
            home: t("shell.offline.home"),
            savedCodeTitle: t("shell.offline.savedCodeTitle"),
            showAtCounter: t("shell.offline.showAtCounter"),
            savedAt: t("shell.offline.savedAt", { time: "{time}" }),
            mayBeOutdated: t("shell.offline.mayBeOutdated"),
          },
        });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [t, info.htmlLang]);

  // The live region is always there, so screen readers announce the notice when it appears.
  return (
    <div role="status">
      {offline && (
        <div className="fixed inset-x-0 top-0 z-50 border-b border-warning/30 bg-warning-soft pt-[env(safe-area-inset-top)] text-warning-ink">
          <p className="mx-auto flex max-w-7xl items-center justify-center gap-2 px-4 py-1.5 text-center text-xs font-medium md:px-6">
            <WifiOff className="size-3.5 shrink-0" aria-hidden />
            {t("shell.offline.banner")}
          </p>
        </div>
      )}
    </div>
  );
}
