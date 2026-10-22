import { cookies, headers } from "next/headers";
import Link from "next/link";

import { Logo } from "@/components/brand/logo";
import { requestI18n } from "@/lib/i18n";

// A server component: its text is rendered on the server in the request's language (src/lib/i18n.ts), and a
// language switch re-renders it with the rest of the page. Line heights leave room for Kannada's stacked vowel
// signs, and the legal links wrap rather than overflow at 360 px.
export async function SiteFooter() {
  const { t } = await requestI18n({ cookies, headers });
  return (
    <footer className="mt-24 border-t bg-surface/50 pb-24 md:pb-0">
      <div className="mx-auto grid max-w-7xl gap-10 px-4 py-12 md:grid-cols-[1.4fr_1fr_1fr_1fr] md:px-6">
        <div className="space-y-3">
          <Logo />
          <p className="max-w-xs text-sm leading-relaxed text-muted-foreground">{t("shell.footer.tagline")}</p>
        </div>
        <div>
          <p className="mb-3 text-sm leading-normal font-semibold">{t("shell.footer.shopHeading")}</p>
          <ul className="space-y-2 text-sm leading-normal text-muted-foreground">
            <li><Link href="/search" className="hover:text-foreground">{t("shell.footer.searchNearby")}</Link></li>
            <li><Link href="/shops" className="hover:text-foreground">{t("shell.footer.browseShops")}</Link></li>
            <li><Link href="/account" className="hover:text-foreground">{t("shell.footer.yourReservations")}</Link></li>
          </ul>
        </div>
        <div>
          <p className="mb-3 text-sm leading-normal font-semibold">{t("shell.footer.forShopsHeading")}</p>
          <ul className="space-y-2 text-sm leading-normal text-muted-foreground">
            <li><Link href="/register/shop" className="hover:text-foreground">{t("shell.nav.listShop")}</Link></li>
            <li><Link href="/shop" className="hover:text-foreground">{t("shell.nav.shopDashboard")}</Link></li>
          </ul>
        </div>
        <div>
          <p className="mb-3 text-sm leading-normal font-semibold">{t("shell.footer.howItWorksHeading")}</p>
          <ul className="space-y-2 text-sm leading-normal text-muted-foreground">
            <li><Link href="/ai" className="hover:text-foreground">{t("shell.nav.aiLab")}</Link></li>
            <li><Link href="/#how" className="hover:text-foreground">{t("shell.footer.pickupDelivery")}</Link></li>
          </ul>
        </div>
      </div>
      <div className="border-t">
        <div className="mx-auto flex max-w-7xl flex-col gap-2 px-4 py-5 text-xs leading-normal text-muted-foreground md:flex-row md:items-center md:justify-between md:px-6">
          <p>{t("shell.footer.notice")}</p>
          <nav aria-label={t("shell.footer.legal")} className="flex flex-wrap gap-x-4 gap-y-2">
            <Link href="/privacy" className="hover:text-foreground">{t("shell.footer.privacy")}</Link>
            <Link href="/terms" className="hover:text-foreground">{t("shell.footer.terms")}</Link>
            <Link href="/grievance" className="hover:text-foreground">{t("shell.footer.grievances")}</Link>
          </nav>
        </div>
      </div>
    </footer>
  );
}
