"use client";

import { Store } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useEffect } from "react";

import { Logo } from "@/components/brand/logo";
import { registerCategoryHues } from "@/components/common/product-thumb";
import { LocationPicker } from "@/components/site/location-picker";
import { SearchBox } from "@/components/site/search-box";
import { ThemeToggle } from "@/components/site/theme-toggle";
import { Button } from "@/components/ui/button";
import { useMe, useMeta } from "@/hooks/use-session";
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

export function SiteHeader() {
  const pathname = usePathname();
  const { data: me, isLoading } = useMe();
  const { data: meta } = useMeta();
  const isHome = pathname === "/";
  // The AI Lab link shows only where the lab answers (P7.1): open to everyone (ai_lab_public: the demo and
  // development) or to an admin; production answers 404 to everyone else. Same rule as site-footer.tsx.
  const labPublic = (meta as { ai_lab_public?: boolean } | undefined)?.ai_lab_public === true;
  const showLab = labPublic || me?.role === "admin";

  useEffect(() => {
    if (meta) registerCategoryHues(meta.categories);
  }, [meta]);

  return (
    <header className="sticky top-0 z-40 border-b border-transparent bg-background/80 backdrop-blur-xl supports-[backdrop-filter]:bg-background/70 [&:not(:hover)]:border-border/60">
      <div className="mx-auto flex h-16 max-w-7xl items-center gap-3 px-4 md:gap-5 md:px-6">
        <Logo className="shrink-0" />
        <div className={cn("hidden min-w-0 flex-1 items-center gap-3 md:flex", isHome && "md:invisible")}>
          <Suspense>
            <SearchBox className="max-w-xl" />
          </Suspense>
          <LocationPicker className="max-w-56 shrink-0" />
        </div>
        <div className="flex-1 md:hidden" />
        <nav className="flex shrink-0 items-center gap-1">
          {/* Header links are not prefetched (P2.9): see mobile-tab-bar.tsx. */}
          <Link
            href="/shops"
            prefetch={false}
            className={cn(
              "hidden rounded-full px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground xl:inline-flex",
              pathname.startsWith("/shops") && "text-foreground",
            )}
          >
            Shops
          </Link>
          {showLab && (
            <Link
              href="/ai"
              prefetch={false}
              className={cn(
                "hidden rounded-full px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground xl:inline-flex",
                pathname === "/ai" && "text-foreground",
              )}
            >
              AI Lab
            </Link>
          )}
          {me && <NotificationsBell />}
          {isLoading ? (
            <div className="size-9" />
          ) : me ? (
            <UserMenu user={me} />
          ) : (
            <>
              <ThemeToggle />
              <Button asChild variant="ghost" className="hidden h-9 rounded-full px-4 sm:inline-flex">
                <Link href="/register/shop" prefetch={false}>
                  <Store /> List your shop
                </Link>
              </Button>
              <Button asChild className="h-9 rounded-full px-4">
                <Link href={`/login?next=${encodeURIComponent(pathname)}`} prefetch={false}>Sign in</Link>
              </Button>
            </>
          )}
        </nav>
      </div>
    </header>
  );
}
