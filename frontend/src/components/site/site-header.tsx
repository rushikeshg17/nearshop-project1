"use client";

import { Languages, Store } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useEffect } from "react";

import { Logo } from "@/components/brand/logo";
import { registerCategoryHues } from "@/components/common/product-thumb";
import { useI18n } from "@/components/providers";
import { LocationPicker } from "@/components/site/location-picker";
import { SearchBox } from "@/components/site/search-box";
import { ThemeToggle } from "@/components/site/theme-toggle";
import { Button } from "@/components/ui/button";
import { useMe, useMeta } from "@/hooks/use-session";
import { isLanguage, LANGUAGES } from "@/lib/i18n";
import { cn } from "@/lib/utils";

// Signed-in only, and the session is read in the browser: the menus (Radix dropdown and popover) load after
// the page, never with its first-load JavaScript (P2.9). Each keeps its button's size while it loads.
const NotificationsBell = dynamic(() => import("@/components/site/notifications-bell").then((m) => m.NotificationsBell), {
  ssr: false,
  loading: () => <div className="size-9" />,
});
const UserMenu = dynamic(() => import("@/components/site/user-menu").then((m) => m.UserMenu), {
  ssr: false,
  loading: () => <div className="size-9" />,
});

/** The interface language: a native select (no menu script on the first load, the phone's own picker), each
 * language in its own name and script. Under the sm breakpoint only its icon shows, a 44 px target; the select
 * covers the whole control. Choosing re-renders the page in that language without a full reload. */
export function LanguageSwitch({ className }: { className?: string }) {
  const { t, selected, setLanguage } = useI18n();
  const current = LANGUAGES.find((l) => l.code === selected) ?? LANGUAGES[0];
  return (
    <div
      className={cn(
        "relative inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center gap-1.5 rounded-full px-2.5 text-sm leading-normal font-medium text-muted-foreground transition-colors has-[select:focus-visible]:ring-2 has-[select:focus-visible]:ring-ring has-[select:focus-visible]:ring-offset-2 has-[select:focus-visible]:ring-offset-background hover:bg-muted hover:text-foreground",
        className,
      )}
    >
      <Languages className="size-[18px] shrink-0" aria-hidden />
      <span aria-hidden lang={current.htmlLang} className="hidden whitespace-nowrap sm:inline">
        {current.name}
      </span>
      <select
        aria-label={t("shell.language.label")}
        value={selected}
        onChange={(e) => {
          if (isLanguage(e.target.value)) setLanguage(e.target.value);
        }}
        className="absolute inset-0 size-full cursor-pointer appearance-none rounded-full opacity-0"
      >
        {LANGUAGES.map((l) => (
          <option key={l.code} value={l.code} lang={l.htmlLang}>
            {l.name}
          </option>
        ))}
      </select>
    </div>
  );
}

export function SiteHeader() {
  const { t } = useI18n();
  const pathname = usePathname();
  const { data: me, isLoading } = useMe();
  const { data: meta } = useMeta();
  const isHome = pathname === "/";

  useEffect(() => {
    if (meta) registerCategoryHues(meta.categories);
  }, [meta]);

  return (
    <header className="sticky top-0 z-40 border-b border-transparent bg-background/80 backdrop-blur-xl supports-[backdrop-filter]:bg-background/70 [&:not(:hover)]:border-border/60">
      {/* Room for longer labels at 360 px (Kannada): min-h, not h, so a taller script grows the bar and never
          clips; the nav is pushed right by its own margin (an empty spacer would add a gap); and the sign-in
          label may wrap onto a second line rather than push the page sideways. */}
      <div className="mx-auto flex min-h-16 max-w-7xl items-center gap-3 px-4 md:gap-5 md:px-6">
        <Logo className="shrink-0" />
        <div className={cn("hidden min-w-0 flex-1 items-center gap-3 md:flex", isHome && "md:invisible")}>
          <Suspense>
            <SearchBox className="max-w-xl" />
          </Suspense>
          <LocationPicker className="max-w-56 shrink-0" />
        </div>
        <nav className="ml-auto flex min-w-0 items-center gap-1">
          {/* Header links are not prefetched (P2.9): see mobile-tab-bar.tsx. */}
          <Link
            href="/shops"
            prefetch={false}
            className={cn(
              "hidden rounded-full px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground xl:inline-flex",
              pathname.startsWith("/shops") && "text-foreground",
            )}
          >
            {t("shell.nav.shops")}
          </Link>
          <Link
            href="/ai"
            prefetch={false}
            className={cn(
              "hidden rounded-full px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground xl:inline-flex",
              pathname === "/ai" && "text-foreground",
            )}
          >
            {t("shell.nav.aiLab")}
          </Link>
          <LanguageSwitch />
          {me && <NotificationsBell />}
          {isLoading ? (
            <div className="size-9" />
          ) : me ? (
            <UserMenu user={me} />
          ) : (
            <>
              <ThemeToggle />
              <Button asChild variant="ghost" className="hidden h-auto min-h-9 rounded-full px-4 py-1.5 leading-normal sm:inline-flex">
                <Link href="/register/shop" prefetch={false}>
                  <Store /> {t("shell.nav.listShop")}
                </Link>
              </Button>
              <Button
                asChild
                className="h-auto min-h-9 min-w-0 shrink rounded-full px-3 py-1.5 text-center leading-snug whitespace-normal sm:px-4"
              >
                <Link href={`/login?next=${encodeURIComponent(pathname)}`} prefetch={false}>
                  {t("shell.nav.signIn")}
                </Link>
              </Button>
            </>
          )}
        </nav>
      </div>
    </header>
  );
}
