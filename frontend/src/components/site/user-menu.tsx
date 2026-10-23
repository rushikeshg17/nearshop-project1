"use client";

import { BarChart3, Languages, LayoutDashboard, LogOut, Monitor, Moon, ShieldCheck, Sparkles, Store, Sun, User2 } from "lucide-react";
import Link from "next/link";
import { useTheme } from "next-themes";

import { useI18n } from "@/components/providers";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useMeta, useSignOut } from "@/hooks/use-session";
import { initials } from "@/lib/format";
import { isLanguage } from "@/lib/i18n";
import type { User } from "@/lib/types";

export function UserMenu({ user }: { user: User }) {
  const signOut = useSignOut();
  const { theme, setTheme } = useTheme();
  const { t, selected, languages, setLanguage } = useI18n();
  const { data: meta } = useMeta();
  // The AI Lab item shows only where the lab answers (P7.1): open to everyone (ai_lab_public) or to an admin.
  const labPublic = (meta as { ai_lab_public?: boolean } | undefined)?.ai_lab_public === true;
  const showLab = labPublic || user.role === "admin";
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-lg" className="rounded-full" aria-label={t("shell.userMenu.trigger")}>
          <Avatar className="size-8">
            <AvatarFallback className="bg-primary text-xs font-semibold text-primary-foreground">{initials(user.name)}</AvatarFallback>
          </Avatar>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="font-normal">
          <p className="truncate text-sm font-medium">{user.name}</p>
          <p className="truncate text-xs text-muted-foreground">{user.email}</p>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {user.role === "customer" && (
          <DropdownMenuItem asChild>
            <Link href="/account">
              <User2 /> {t("shell.userMenu.reservations")}
            </Link>
          </DropdownMenuItem>
        )}
        {(user.role === "owner" || user.role === "staff") && (
          <>
            <DropdownMenuItem asChild>
              <Link href={user.role === "staff" ? "/shop/reservations" : "/shop"}>
                <LayoutDashboard /> {t("shell.nav.shopDashboard")}
              </Link>
            </DropdownMenuItem>
            {user.shop && (
              <DropdownMenuItem asChild>
                <Link href={`/shops/${user.shop.slug}`}>
                  <Store /> {t("shell.userMenu.viewShopPage")}
                </Link>
              </DropdownMenuItem>
            )}
          </>
        )}
        {user.role === "admin" && (
          <DropdownMenuItem asChild>
            <Link href="/admin">
              <ShieldCheck /> {t("shell.userMenu.adminConsole")}
            </Link>
          </DropdownMenuItem>
        )}
        {showLab && (
          <DropdownMenuItem asChild>
            <Link href="/ai">
              <Sparkles /> {t("shell.nav.aiLab")}
            </Link>
          </DropdownMenuItem>
        )}
        {user.role === "admin" && (
          <DropdownMenuItem asChild>
            <Link href="/admin/anomalies">
              <BarChart3 /> {t("shell.userMenu.priceReview")}
            </Link>
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuSub>
          <DropdownMenuSubTrigger>
            <Sun className="dark:hidden" />
            <Moon className="hidden dark:block" /> {t("shell.userMenu.theme")}
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent>
            <DropdownMenuRadioGroup value={theme} onValueChange={setTheme}>
              <DropdownMenuRadioItem value="light">
                <Sun /> {t("shell.userMenu.themeLight")}
              </DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="dark">
                <Moon /> {t("shell.userMenu.themeDark")}
              </DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="system">
                <Monitor /> {t("shell.userMenu.themeSystem")}
              </DropdownMenuRadioItem>
            </DropdownMenuRadioGroup>
          </DropdownMenuSubContent>
        </DropdownMenuSub>
        {/* Only when the build offers more than English (src/lib/i18n.ts). */}
        {languages.length > 1 && (
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>
              <Languages /> {t("shell.language.label")}
            </DropdownMenuSubTrigger>
            <DropdownMenuSubContent>
              <DropdownMenuRadioGroup
                value={selected}
                onValueChange={(code) => {
                  if (isLanguage(code)) setLanguage(code);
                }}
              >
                {languages.map((l) => (
                  <DropdownMenuRadioItem key={l.code} value={l.code} lang={l.htmlLang}>
                    {l.name}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={signOut} variant="destructive">
          <LogOut /> {t("shell.userMenu.signOut")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
