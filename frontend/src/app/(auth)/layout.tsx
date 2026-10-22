import { cookies, headers } from "next/headers";
import Link from "next/link";

import { Logo } from "@/components/brand/logo";
import { I18nAreas } from "@/components/providers";
import { pickAreas, requestI18n } from "@/lib/i18n";

export default async function AuthLayout({ children }: LayoutProps<"/">) {
  // The sign-in and sign-up screens' strings (P7.7) reach the browser on these pages only. With English the only
  // language offered, the request is not read and the pages stay static (src/lib/i18n.ts).
  const { messages, t } = await requestI18n({ cookies, headers });
  return (
    <div className="grid min-h-dvh lg:grid-cols-[1fr_1.1fr]">
      <div className="flex flex-col px-5 py-6 md:px-10">
        <Logo />
        <main className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center py-10">
          <I18nAreas messages={pickAreas(messages, ["auth"])}>{children}</I18nAreas>
        </main>
        <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <Link href="/" className="hover:text-foreground">{t("auth.layout.back")}</Link>
          <Link href="/privacy" className="hover:text-foreground">{t("shell.footer.privacy")}</Link>
          <Link href="/terms" className="hover:text-foreground">{t("shell.footer.terms")}</Link>
          <Link href="/grievance" className="hover:text-foreground">{t("shell.footer.grievances")}</Link>
        </p>
      </div>
      <aside className="relative hidden overflow-hidden bg-primary text-primary-foreground lg:block">
        <div className="bg-dots absolute inset-0 opacity-40 [mask-image:linear-gradient(to_bottom,black,transparent)]" aria-hidden />
        <div className="relative flex h-full flex-col justify-end p-12">
          <div className="max-w-md">
            <p className="font-heading text-4xl leading-tight font-semibold">{t("auth.layout.pitchTitle")}</p>
            <p className="mt-4 text-primary-foreground/60">{t("auth.layout.pitchBody")}</p>
          </div>
          <div className="mt-12 grid max-w-md grid-cols-3 gap-4 text-sm">
            {(["search", "reserve", "pickUp"] as const).map((key) => (
              <div key={key} className="rounded-2xl bg-primary-foreground/[0.06] p-4">
                <p className="font-semibold">{t(`auth.layout.pillars.${key}.title`)}</p>
                <p className="text-primary-foreground/60">{t(`auth.layout.pillars.${key}.body`)}</p>
              </div>
            ))}
          </div>
        </div>
      </aside>
    </div>
  );
}
