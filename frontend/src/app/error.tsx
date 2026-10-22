"use client";

import { RotateCcw, TriangleAlert } from "lucide-react";
import { useEffect } from "react";

import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { captureException } from "@/lib/sentry-browser";

export default function Error({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  // The root layout's Providers wrap this boundary, so the page's language and the common messages are here.
  const { t } = useI18n();
  // The boundary catches the error before Sentry's global handlers see it, so report it here (a no-op
  // without NEXT_PUBLIC_SENTRY_DSN; events are scrubbed by src/lib/observability.ts).
  useEffect(() => {
    captureException(error);
  }, [error]);

  return (
    <div className="grid min-h-[70dvh] place-items-center px-4">
      <div className="max-w-md text-center">
        <div className="mx-auto grid size-14 place-items-center rounded-2xl bg-destructive/10"><TriangleAlert className="size-6 text-destructive" /></div>
        <h1 className="mt-5 text-2xl font-semibold">{t("common.errorPage.title")}</h1>
        <p className="mt-2 text-muted-foreground">{t("common.errorPage.body")}</p>
        <div className="mt-6 flex justify-center gap-2">
          <Button onClick={reset}><RotateCcw /> {t("common.states.retry")}</Button>
          {/* A plain link, not next/link: see not-found.tsx (P2.9). */}
          {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- keeps next/link out of a chunk every page loads */}
          <Button asChild variant="outline"><a href="/">{t("common.errorPage.home")}</a></Button>
        </div>
      </div>
    </div>
  );
}
