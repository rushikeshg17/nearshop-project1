"use client";

import { useQuery } from "@tanstack/react-query";
import {
  BadgeCheck,
  BrainCircuit,
  CalendarClock,
  ClipboardList,
  Clock,
  EyeOff,
  FileSpreadsheet,
  LayoutDashboard,
  Library,
  Package,
  Settings,
  ShieldAlert,
  Star,
  Truck,
} from "lucide-react";
import Link from "next/link";

import { DashboardShell, LegalFooter } from "@/components/dashboard/dashboard-shell";
import { EmptyState, ListSkeleton } from "@/components/common/states";
import { useI18n } from "@/components/providers";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useMe } from "@/hooks/use-session";
import { api } from "@/lib/api";
import { DEFAULT_LANGUAGE, type LanguageCode, languageInfo } from "@/lib/i18n";
import type { OwnerOverview, ShopVerification } from "@/lib/types";

export function OwnerShell({ children }: { children: React.ReactNode }) {
  const { t } = useI18n();
  const { data: me, isLoading } = useMe();
  const verification = useQuery({
    queryKey: ["owner", "verification"],
    queryFn: () => api.get<ShopVerification>("/owner/verification"),
    enabled: me?.role === "owner" && !!me.shop,
    refetchInterval: 30_000,
  });
  const overview = useQuery({
    queryKey: ["owner", "overview"],
    queryFn: () => api.get<OwnerOverview>("/owner/overview"),
    enabled: me?.role === "owner",
    refetchInterval: 15_000,
  });

  if (isLoading) return <div className="p-10"><ListSkeleton /><LegalFooter className="mt-16" /></div>;
  if (!me || me.role !== "owner") {
    return (
      <div className="mx-auto max-w-lg px-4 py-24">
        <EmptyState icon={Package} title={t("owner.shell.notOwnerTitle")} description={t("owner.shell.notOwnerBody")}
          action={<><Button asChild><Link href="/register/shop">{t("owner.shell.listShop")}</Link></Button><Button asChild variant="outline"><Link href="/login?next=/shop">{t("owner.shell.signIn")}</Link></Button></>} />
        <LegalFooter className="mt-16" />
      </div>
    );
  }

  const q = overview.data?.queue;
  const items = [
    { href: "/shop", label: t("owner.shell.nav.overview"), icon: LayoutDashboard, exact: true },
    { href: "/shop/reservations", label: t("owner.shell.nav.reservations"), icon: ClipboardList, badge: q?.reservation_requests },
    { href: "/shop/orders", label: t("owner.shell.nav.orders"), icon: Truck, badge: q?.orders_pending },
    { href: "/shop/inventory", label: t("owner.shell.nav.inventory"), icon: Package },
    { href: "/shop/import", label: t("owner.shell.nav.import"), icon: FileSpreadsheet },
    { href: "/shop/catalog", label: t("owner.shell.nav.catalog"), icon: Library },
    { href: "/shop/insights", label: t("owner.shell.nav.insights"), icon: BrainCircuit },
    { href: "/shop/reviews", label: t("owner.shell.nav.reviews"), icon: Star },
    { href: "/shop/settings", label: t("owner.shell.nav.settings"), icon: Settings },
  ];
  return (
    <DashboardShell title={me.shop?.name ?? t("owner.shell.yourShop")} subtitle={me.name} items={items} publicHref={me.shop ? `/shops/${me.shop.slug}` : undefined}>
      {verification.data && <VerificationBanner v={verification.data} />}
      {children}
    </DashboardShell>
  );
}

/** A date such as "21 Oct 2027", in India time, with the month named in the interface language (English when not
 * given). */
export function formatDay(iso: string | null | undefined, language: LanguageCode = DEFAULT_LANGUAGE) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString(languageInfo(language).htmlLang, { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" });
}

const VERIFY_HREF = "/shop/settings#verify";

/** Which banner a shop's verification calls for (plan section 8.1, P3.3), or null when there is nothing to say. */
export function verificationNotice(v: ShopVerification, now: number = Date.now()) {
  const until = v.verified_until ? new Date(v.verified_until).getTime() : null;
  switch (v.status) {
    case "draft":
      return { kind: "draft" as const };
    case "submitted":
      return { kind: "submitted" as const };
    case "rejected":
      return { kind: "rejected" as const };
    case "suspended":
      return { kind: "suspended" as const };
    case "verified":
      if (v.renewal_pending) return { kind: "renewal_pending" as const };
      if (until !== null && until <= now) return { kind: "expired" as const };
      if (v.renewal_opens_at && new Date(v.renewal_opens_at).getTime() <= now) return { kind: "renew" as const };
      return null;
  }
}

/** The verification status banner on every owner page (P3.4). */
export function VerificationBanner({ v }: { v: ShopVerification }) {
  const { t, language } = useI18n();
  const notice = verificationNotice(v);
  if (!notice) return null;
  const date = formatDay(v.verified_until, language);
  const link = (label: string) => (
    <div className="col-start-2 mt-2">
      <Button asChild size="sm" variant="outline">
        <Link href={VERIFY_HREF}>{label}</Link>
      </Button>
    </div>
  );
  // The admin's reason is shown as they wrote it.
  const reason = v.rejection_reason ? <p className="mt-1"><span className="font-medium">{t("owner.verification.reason")}</span> {v.rejection_reason}</p> : null;
  const body = {
    draft: {
      icon: EyeOff,
      title: t("owner.verification.draft.title"),
      text: <>{t("owner.verification.draft.body")}</>,
      action: link(t("owner.verification.draft.action")),
      destructive: false,
    },
    submitted: {
      icon: Clock,
      title: t("owner.verification.submitted.title"),
      text: <>{t("owner.verification.submitted.body")}</>,
      action: null,
      destructive: false,
    },
    rejected: {
      icon: ShieldAlert,
      title: t("owner.verification.rejected.title"),
      text: <>{reason}<p>{t("owner.verification.rejected.body")}</p></>,
      action: link(t("owner.verification.rejected.action")),
      destructive: true,
    },
    suspended: {
      icon: ShieldAlert,
      title: t("owner.verification.suspended.title"),
      text: <>{reason}<p className="mt-1">{t("owner.verification.suspended.body")}</p></>,
      action: link(t("owner.verification.suspended.action")),
      destructive: true,
    },
    renewal_pending: {
      icon: BadgeCheck,
      title: t("owner.verification.renewalPending.title"),
      text: <>{t("owner.verification.renewalPending.body")}</>,
      action: null,
      destructive: false,
    },
    renew: {
      icon: CalendarClock,
      title: t("owner.verification.renew.title", { date }),
      text: <>{reason}<p className={reason ? "mt-1" : undefined}>{t("owner.verification.renew.body", { date })}</p></>,
      action: link(t("owner.verification.renew.action")),
      destructive: false,
    },
    expired: {
      icon: EyeOff,
      title: t("owner.verification.expired.title"),
      text: <>{t("owner.verification.expired.body", { date })}</>,
      action: link(t("owner.verification.expired.action")),
      destructive: true,
    },
  }[notice.kind];
  const Icon = body.icon;
  return (
    <Alert variant={body.destructive ? "destructive" : "default"} role="status" className="mb-6" data-testid="verification-banner" data-status={notice.kind}>
      <Icon />
      <AlertTitle>{body.title}</AlertTitle>
      <AlertDescription>{body.text}</AlertDescription>
      {body.action}
    </Alert>
  );
}
