"use client";

import { Home, LayoutDashboard, Receipt, Search, Store } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useI18n } from "@/components/providers";
import { useMe } from "@/hooks/use-session";
import { cn } from "@/lib/utils";

export function MobileTabBar() {
  const pathname = usePathname();
  const { data: me } = useMe();
  const { t } = useI18n();
  const tabs = [
    { id: "home", href: "/", label: t("shell.nav.home"), icon: Home, match: (p: string) => p === "/" },
    { id: "search", href: "/search", label: t("shell.nav.search"), icon: Search, match: (p: string) => p.startsWith("/search") || p.startsWith("/product") },
    { id: "shops", href: "/shops", label: t("shell.nav.shops"), icon: Store, match: (p: string) => p.startsWith("/shops") },
    me?.role === "owner" || me?.role === "staff"
      ? { id: "mine", href: "/shop", label: t("shell.nav.myShop"), icon: LayoutDashboard, match: (p: string) => p.startsWith("/shop/") || p === "/shop" }
      : { id: "mine", href: me ? "/account" : "/login?next=/account", label: t("shell.nav.activity"), icon: Receipt, match: (p: string) => p.startsWith("/account") },
  ];

  return (
    <nav
      aria-label={t("shell.tabBar.label")}
      className="fixed inset-x-0 bottom-0 z-40 border-t bg-background/90 pb-safe backdrop-blur-xl md:hidden"
    >
      <ul className="mx-auto grid max-w-md grid-cols-4">
        {tabs.map((tab) => {
          const active = tab.match(pathname);
          return (
            <li key={tab.id}>
              {/* No prefetch (P2.9): on a slow connection, fetching four other screens on every page load
                  competed with the page's own content and data. */}
              <Link
                href={tab.href}
                prefetch={false}
                aria-current={active ? "page" : undefined}
                // No fixed height, and a line height for scripts with stacked vowel signs (Kannada): a longer label
                // wraps onto a second centred line and the bar grows, so nothing is clipped at 360 px.
                className={cn(
                  "relative flex h-full flex-col items-center gap-1 px-1 pt-2.5 pb-1.5 text-center text-[11px] leading-[1.5] font-medium text-balance break-words transition-colors",
                  active ? "text-foreground" : "text-muted-foreground",
                )}
              >
                {/* The active tab's indicator grows in with a CSS transition (no animation library, P2.9). */}
                <span
                  aria-hidden
                  className={cn(
                    "absolute top-0 h-0.5 w-8 rounded-full bg-brand transition-[opacity,transform] duration-200 ease-out",
                    active ? "scale-x-100 opacity-100" : "scale-x-0 opacity-0",
                  )}
                />
                <tab.icon className="size-5" strokeWidth={active ? 2.2 : 1.8} />
                {tab.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
