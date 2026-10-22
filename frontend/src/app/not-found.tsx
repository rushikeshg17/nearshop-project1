import { cookies, headers } from "next/headers";

import { LogoMark } from "@/components/brand/logo";
import { buttonVariants } from "@/components/ui/button";
import { requestI18n } from "@/lib/i18n";

// Plain links and an inline icon, no next/link or lucide: this page sits at the root, so anything it imports
// from the client is copied into a chunk every page loads (P2.9). A full page load is fine from a 404.
// A server component: its text is translated on the server (P7.10), so no translation code reaches that chunk. With
// English the only language offered, the request is not read (src/lib/i18n.ts).
export default async function NotFound() {
  const { t } = await requestI18n({ cookies, headers });
  return (
    <div className="grid min-h-dvh place-items-center px-4">
      <div className="max-w-md text-center">
        {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- see above */}
        <a href="/" className="inline-flex items-center justify-center gap-2 rounded-lg" aria-label={t("common.notFound.homeLabel")}>
          <LogoMark />
          <span className="font-heading text-[1.2rem] leading-none font-semibold tracking-tight">
            near<span className="text-brand">shop</span>
          </span>
        </a>
        <div className="mx-auto mt-10 grid size-14 place-items-center rounded-2xl bg-muted">
          {/* lucide's map-pin-off */}
          <svg viewBox="0 0 24 24" aria-hidden className="size-6 fill-none stroke-current stroke-2 text-muted-foreground [stroke-linecap:round] [stroke-linejoin:round]">
            <path d="M12.75 7.09a3 3 0 0 1 2.16 2.16" />
            <path d="M17.072 17.072c-1.634 2.17-3.527 3.912-4.471 4.727a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 1.432-4.568" />
            <path d="m2 2 20 20" />
            <path d="M8.475 2.818A8 8 0 0 1 20 10c0 1.183-.31 2.377-.81 3.533" />
            <path d="M9.13 9.13a3 3 0 0 0 3.74 3.74" />
          </svg>
        </div>
        <h1 className="mt-5 text-3xl font-semibold">{t("common.notFound.title")}</h1>
        <p className="mt-2 text-muted-foreground">{t("common.notFound.body")}</p>
        <div className="mt-6 flex justify-center gap-2">
          <a href="/search" className={buttonVariants()}>{t("common.notFound.search")}</a>
          {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- see above */}
          <a href="/" className={buttonVariants({ variant: "outline" })}>{t("common.notFound.home")}</a>
        </div>
        <p className="mt-10 flex justify-center gap-4 text-xs text-muted-foreground">
          <a href="/privacy" className="hover:text-foreground">{t("shell.footer.privacy")}</a>
          <a href="/terms" className="hover:text-foreground">{t("shell.footer.terms")}</a>
          <a href="/grievance" className="hover:text-foreground">{t("shell.footer.grievances")}</a>
        </p>
      </div>
    </div>
  );
}
