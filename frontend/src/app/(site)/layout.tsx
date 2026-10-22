import { cookies, headers } from "next/headers";

import { I18nAreas } from "@/components/providers";
import { DemoBanner } from "@/components/site/demo-banner";
import { MobileTabBar } from "@/components/site/mobile-tab-bar";
import { SiteFooter } from "@/components/site/site-footer";
import { SiteHeader } from "@/components/site/site-header";
import { pickAreas, requestI18n } from "@/lib/i18n";

export default async function SiteLayout({ children }: LayoutProps<"/">) {
  // The discovery screens' strings (home, search, product and shop: P7.6) and the transaction screens' (the reserve
  // and delivery dialogs, the account page and the reservation and order pages: P7.7) reach the browser on the
  // customer site only. With English the only language offered, the request is not read and pages stay static (src/lib/i18n.ts).
  const { messages } = await requestI18n({ cookies, headers });
  return (
    <>
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-primary px-3 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Skip to content
      </a>
      <DemoBanner />
      <SiteHeader />
      <main id="main" className="min-h-[calc(100dvh-4rem)] pb-20 md:pb-0">
        <I18nAreas messages={pickAreas(messages, ["discovery", "transactions"])}>{children}</I18nAreas>
      </main>
      <SiteFooter />
      <MobileTabBar />
    </>
  );
}
