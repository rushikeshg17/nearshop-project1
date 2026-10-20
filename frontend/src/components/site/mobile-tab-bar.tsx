"use client";

import { Home, LayoutDashboard, Receipt, Search, Store } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useMe } from "@/hooks/use-session";
import { cn } from "@/lib/utils";

export function MobileTabBar() {
  const pathname = usePathname();
  const { data: me } = useMe();
  const tabs = [
    { href: "/", label: "Home", icon: Home, match: (p: string) => p === "/" },
    { href: "/search", label: "Search", icon: Search, match: (p: string) => p.startsWith("/search") || p.startsWith("/product") },
    { href: "/shops", label: "Shops", icon: Store, match: (p: string) => p.startsWith("/shops") },
    me?.role === "owner"
      ? { href: "/shop", label: "My shop", icon: LayoutDashboard, match: (p: string) => p.startsWith("/shop/") || p === "/shop" }
      : { href: me ? "/account" : "/login?next=/account", label: "Activity", icon: Receipt, match: (p: string) => p.startsWith("/account") },
  ];

  return (
    <nav
      aria-label="Primary"
      className="fixed inset-x-0 bottom-0 z-40 border-t bg-background/90 pb-safe backdrop-blur-xl md:hidden"
    >
      <ul className="mx-auto grid max-w-md grid-cols-4">
        {tabs.map((t) => {
          const active = t.match(pathname);
          return (
            <li key={t.label}>
              {/* No prefetch (P2.9): on a slow connection, fetching four other screens on every page load
                  competed with the page's own content and data. */}
              <Link
                href={t.href}
                prefetch={false}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "relative flex flex-col items-center gap-1 pt-2.5 pb-1.5 text-[11px] font-medium transition-colors",
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
                <t.icon className="size-5" strokeWidth={active ? 2.2 : 1.8} />
                {t.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
