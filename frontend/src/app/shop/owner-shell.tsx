"use client";

import { useQuery } from "@tanstack/react-query";
import {
  BadgeCheck,
  BrainCircuit,
  CalendarClock,
  ClipboardList,
  Clock,
  CreditCard,
  EyeOff,
  FileSpreadsheet,
  FileText,
  LayoutDashboard,
  Library,
  Megaphone,
  Package,
  Settings,
  ShieldAlert,
  Star,
  Truck,
  Users,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { DashboardShell, LegalFooter } from "@/components/dashboard/dashboard-shell";
import { EmptyState, ListSkeleton } from "@/components/common/states";
import { useI18n } from "@/components/providers";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useMe } from "@/hooks/use-session";
import { api } from "@/lib/api";
import { DEFAULT_LANGUAGE, type LanguageCode, languageInfo } from "@/lib/i18n";
import { formatNumber } from "@/lib/format";
import type { BoostEligibility, BoostPack, LeadCounts, ShopVerification, WorkOverview } from "@/lib/types";

/** The console pages an active staff member may open (P9.13): the daily work. Every other page is the owner's alone,
 * as the API's owner-only routes are (backend/app/core/deps.py WorkShop); the overview too, since it shows the shop's
 * sales (staff land on the reservations instead, STAFF_HOME). */
const STAFF_PAGES = ["/shop/reservations", "/shop/orders", "/shop/inventory", "/shop/import", "/shop/catalog"];
export const STAFF_HOME = "/shop/reservations";

export function staffMayOpen(pathname: string) {
  return STAFF_PAGES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

export function OwnerShell({ children }: { children: React.ReactNode }) {
  const { t } = useI18n();
  const pathname = usePathname();
  const router = useRouter();
  const { data: me, isLoading } = useMe();
  const isStaff = me?.role === "staff";
  const staffActive = isStaff && me.shop?.staff_status === "active";
  // The overview is the owner's; a staff member who opens the console's front page goes to the queue.
  useEffect(() => {
    if (staffActive && pathname === "/shop") router.replace(STAFF_HOME);
  }, [staffActive, pathname, router]);
  const verification = useQuery({
    queryKey: ["owner", "verification"],
    queryFn: () => api.get<ShopVerification>("/owner/verification"),
    enabled: me?.role === "owner" && !!me.shop,
    refetchInterval: 30_000,
  });
  const overview = useQuery({
    queryKey: ["owner", "overview"],
    queryFn: () => api.get<WorkOverview>("/owner/overview"),
    enabled: me?.role === "owner" || staffActive,
    refetchInterval: 15_000,
  });

  // Boost (P10.13): the menu item shows while the shop's city is eligible (plan section 8.22), and also while the shop
  // has a pack that has not ended, so an owner whose city dropped below a threshold (the pack is paused) or whose
  // paid pack outlives a sales switch-off still reaches the page with the pause, the days added back and the figures.
  // The daily check moves slowly, so the answers are kept for 10 minutes; any error leaves the item hidden.
  const boost = useQuery({
    queryKey: ["owner", "boost", "eligibility"],
    queryFn: () => api.get<BoostEligibility>("/owner/boost/eligibility"),
    enabled: me?.role === "owner" && !!me.shop,
    staleTime: 600_000,
    retry: false,
  });
  const boostPacks = useQuery({
    queryKey: ["owner", "boost", "packs"],
    queryFn: () => api.get<{ packs: BoostPack[] }>("/owner/boost/packs"),
    enabled: !!boost.data && !boost.data.city.eligible,
    staleTime: 600_000,
    retry: false,
  });

  if (isLoading) return <div className="p-10"><ListSkeleton /><LegalFooter className="mt-16" /></div>;
  if (isStaff && !staffActive) {
    return (
      <div className="mx-auto max-w-lg px-4 py-24">
        <EmptyState icon={Users} title={t("owner.shell.staffPausedTitle")} description={t("owner.shell.staffPausedBody")} />
        <LegalFooter className="mt-16" />
      </div>
    );
  }
  if (!me || (me.role !== "owner" && !isStaff)) {
    return (
      <div className="mx-auto max-w-lg px-4 py-24">
        <EmptyState icon={Package} title={t("owner.shell.notOwnerTitle")} description={t("owner.shell.notOwnerBody")}
          action={<><Button asChild><Link href="/register/shop">{t("owner.shell.listShop")}</Link></Button><Button asChild variant="outline"><Link href="/login?next=/shop">{t("owner.shell.signIn")}</Link></Button></>} />
        <LegalFooter className="mt-16" />
      </div>
    );
  }

  const q = overview.data?.queue;
  const all = [
    { href: "/shop", label: t("owner.shell.nav.overview"), icon: LayoutDashboard, exact: true },
    { href: "/shop/reservations", label: t("owner.shell.nav.reservations"), icon: ClipboardList, badge: q?.reservation_requests },
    { href: "/shop/orders", label: t("owner.shell.nav.orders"), icon: Truck, badge: q?.orders_pending },
    { href: "/shop/inventory", label: t("owner.shell.nav.inventory"), icon: Package },
    { href: "/shop/import", label: t("owner.shell.nav.import"), icon: FileSpreadsheet },
    { href: "/shop/catalog", label: t("owner.shell.nav.catalog"), icon: Library },
    { href: "/shop/insights", label: t("owner.shell.nav.insights"), icon: BrainCircuit },
    { href: "/shop/statements", label: t("owner.shell.nav.statements"), icon: FileText },
    { href: "/shop/reviews", label: t("owner.shell.nav.reviews"), icon: Star },
    { href: "/shop/billing", label: t("owner.shell.nav.billing"), icon: CreditCard },
    { href: "/shop/staff", label: t("owner.shell.nav.staff"), icon: Users },
    ...(boostShown(boost.data, boostPacks.data?.packs) ? [{ href: "/shop/boost", label: t("owner.shell.nav.boost"), icon: Megaphone }] : []),
    { href: "/shop/settings", label: t("owner.shell.nav.settings"), icon: Settings },
  ];
  // Staff see the daily work only; the owner sees everything, Staff included.
  const items = isStaff ? all.filter((i) => staffMayOpen(i.href)) : all;
  const ownerOnly = isStaff && !staffMayOpen(pathname);
  return (
    <DashboardShell title={me.shop?.name ?? t("owner.shell.yourShop")} subtitle={me.name} items={items} publicHref={me.shop ? `/shops/${me.shop.slug}` : undefined}>
      {verification.data && <VerificationBanner v={verification.data} />}
      {ownerOnly ? (
        <EmptyState icon={ShieldAlert} title={t("owner.shell.ownerOnlyTitle")} description={t("owner.shell.ownerOnlyBody")}
          action={<Button asChild><Link href="/shop/reservations">{t("owner.shell.toQueue")}</Link></Button>} />
      ) : children}
    </DashboardShell>
  );
}

/** Whether the owner menu offers Boost: when the shop's city meets every threshold, or the shop has a pack that has
 * not ended (pending, active, paused or in its make-good period). */
export function boostShown(e: BoostEligibility | undefined, packs: BoostPack[] | undefined) {
  if (!e) return false;
  return e.city.eligible || (packs ?? []).some((p) => p.status !== "ended");
}

/** A date such as "21 Oct 2027", in India time, with the month named in the interface language (English when not
 * given). */
export function formatDay(iso: string | null | undefined, language: LanguageCode = DEFAULT_LANGUAGE) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString(languageInfo(language).htmlLang, { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" });
}

/** A statement month (YYYY-MM) such as "May 2027", with the month named in the interface language. */
export function formatMonth(month: string, language: LanguageCode = DEFAULT_LANGUAGE) {
  const [year, number] = month.split("-").map(Number);
  if (!year || !number) return month;
  return new Date(Date.UTC(year, number - 1, 1)).toLocaleDateString(languageInfo(language).htmlLang, { month: "long", year: "numeric", timeZone: "UTC" });
}

/** The counts a monthly statement states (P9.9, plan section 8.18), in the order it states them: customer actions on
 * NearShop, never a value of the shop's sales. */
export const STATEMENT_COUNTS = ["reservation_requests", "pickups_completed", "delivery_orders", "call_taps", "directions_taps"] as const;

/** A statement's counts, or the current month's so far, as labelled numbers (the statements page and the overview). */
export function LeadCountList({ counts, className, testId }: { counts: LeadCounts; className?: string; testId?: string }) {
  const { t } = useI18n();
  return (
    <dl className={className ?? "grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5"} data-testid={testId}>
      {STATEMENT_COUNTS.map((field) => (
        <div key={field} className="rounded-xl bg-muted/50 p-3" data-count={field}>
          <dt className="text-xs text-muted-foreground">{t(`owner.statements.counts.${field}`)}</dt>
          <dd className="mt-1 text-xl font-semibold tabular" data-value={counts[field]}>{formatNumber(counts[field])}</dd>
        </div>
      ))}
    </dl>
  );
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
