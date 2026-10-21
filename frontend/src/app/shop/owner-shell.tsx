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
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useMe } from "@/hooks/use-session";
import { api } from "@/lib/api";
import type { OwnerOverview, ShopVerification } from "@/lib/types";

export function OwnerShell({ children }: { children: React.ReactNode }) {
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
        <EmptyState icon={Package} title="The shop dashboard is for shop owners" description="Sign in with a shop owner account, or list your shop in a few minutes."
          action={<><Button asChild><Link href="/register/shop">List your shop</Link></Button><Button asChild variant="outline"><Link href="/login?next=/shop">Sign in</Link></Button></>} />
        <LegalFooter className="mt-16" />
      </div>
    );
  }

  const q = overview.data?.queue;
  const items = [
    { href: "/shop", label: "Overview", icon: LayoutDashboard, exact: true },
    { href: "/shop/reservations", label: "Reservations", icon: ClipboardList, badge: q?.reservation_requests },
    { href: "/shop/orders", label: "Delivery orders", icon: Truck, badge: q?.orders_pending },
    { href: "/shop/inventory", label: "Inventory", icon: Package },
    { href: "/shop/import", label: "Import from Excel", icon: FileSpreadsheet },
    { href: "/shop/catalog", label: "Add from catalogue", icon: Library },
    { href: "/shop/insights", label: "AI insights", icon: BrainCircuit },
    { href: "/shop/reviews", label: "Reviews", icon: Star },
    { href: "/shop/settings", label: "Shop settings", icon: Settings },
  ];
  return (
    <DashboardShell title={me.shop?.name ?? "Your shop"} subtitle={me.name} items={items} publicHref={me.shop ? `/shops/${me.shop.slug}` : undefined}>
      {verification.data && <VerificationBanner v={verification.data} />}
      {children}
    </DashboardShell>
  );
}

/** A date such as "21 Oct 2027", in India time. */
export function formatDay(iso: string | null | undefined) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" });
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
  const notice = verificationNotice(v);
  if (!notice) return null;
  const until = formatDay(v.verified_until);
  const link = (label: string) => (
    <div className="col-start-2 mt-2">
      <Button asChild size="sm" variant="outline">
        <Link href={VERIFY_HREF}>{label}</Link>
      </Button>
    </div>
  );
  const reason = v.rejection_reason ? <p className="mt-1"><span className="font-medium">Reason:</span> {v.rejection_reason}</p> : null;
  const body = {
    draft: {
      icon: EyeOff,
      title: "Customers cannot see your shop yet",
      text: <>Verify your shop with your GSTIN, Udyam number or trade licence. You can set up everything else meanwhile.</>,
      action: link("Verify your shop"),
      destructive: false,
    },
    submitted: {
      icon: Clock,
      title: "Verification submitted",
      text: <>An admin is checking your evidence, usually within 2 working days. Your shop stays hidden from customers until it is approved.</>,
      action: null,
      destructive: false,
    },
    rejected: {
      icon: ShieldAlert,
      title: "Your verification was not approved",
      text: <>{reason}<p>Fix the problem and submit again. Customers cannot see your shop until it is verified.</p></>,
      action: link("Resubmit"),
      destructive: true,
    },
    suspended: {
      icon: ShieldAlert,
      title: "Your shop is suspended",
      text: <>{reason}<p className="mt-1">Customers cannot see your shop until it is verified again.</p></>,
      action: link("Submit fresh evidence"),
      destructive: true,
    },
    renewal_pending: {
      icon: BadgeCheck,
      title: "Renewal submitted",
      text: <>An admin is checking your fresh evidence. Your shop stays visible while it is reviewed.</>,
      action: null,
      destructive: false,
    },
    renew: {
      icon: CalendarClock,
      title: `Renew your verification by ${until}`,
      text: <>{reason}<p className={reason ? "mt-1" : undefined}>Your verification ends on {until}. Submit fresh evidence before then to stay visible to customers.</p></>,
      action: link("Renew"),
      destructive: false,
    },
    expired: {
      icon: EyeOff,
      title: "Your verification has ended",
      text: <>It ended on {until}. Your shop is hidden from customers until it is renewed.</>,
      action: link("Renew"),
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
