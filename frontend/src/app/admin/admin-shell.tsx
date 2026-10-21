"use client";

import { useQuery } from "@tanstack/react-query";
import { BadgeCheck, BrainCircuit, Flag, LayoutDashboard, Loader2, Scale, ShieldCheck, Store, Users } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { EmptyState, ListSkeleton } from "@/components/common/states";
import { DashboardShell, LegalFooter } from "@/components/dashboard/dashboard-shell";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useMe } from "@/hooks/use-session";
import { api } from "@/lib/api";
import type { AdminOverview, ModerationQueue, VerificationQueueItem } from "@/lib/types";

export function AdminShell({ children }: { children: React.ReactNode }) {
  const { data: me, isLoading } = useMe();
  const overview = useQuery({ queryKey: ["admin", "overview"], queryFn: () => api.get<AdminOverview>("/admin/overview"), enabled: me?.role === "admin" });
  const queue = useVerificationQueue(me?.role === "admin");
  const moderation = useModerationQueue(me?.role === "admin");
  if (isLoading) return <div className="p-10"><ListSkeleton /><LegalFooter className="mt-16" /></div>;
  if (!me || me.role !== "admin") {
    return (
      <div className="mx-auto max-w-lg px-4 py-24">
        <EmptyState icon={ShieldCheck} title="Admins only" description="Sign in with an admin account to see platform analytics."
          action={<Button asChild><Link href="/login?next=/admin">Sign in</Link></Button>} />
        <LegalFooter className="mt-16" />
      </div>
    );
  }
  const items = [
    { href: "/admin", label: "Overview", icon: LayoutDashboard, exact: true },
    { href: "/admin/anomalies", label: "Price review", icon: Scale, badge: overview.data?.totals.open_price_flags },
    { href: "/admin/verification", label: "Verification", icon: BadgeCheck, badge: queue.data?.length },
    { href: "/admin/moderation", label: "Moderation", icon: Flag, badge: moderation.data?.items.length },
    { href: "/admin/shops", label: "Shops", icon: Store },
    { href: "/admin/users", label: "Users", icon: Users },
    { href: "/ai", label: "AI Lab", icon: BrainCircuit },
  ];
  return <DashboardShell title="NearShop admin" subtitle={me.email} items={items}>{children}</DashboardShell>;
}

/** The shop verification queue (P3.3), shared by the nav badge and the Verification page. Polled every 30 seconds,
 * so a new submission shows within a minute and the 5-minute signed evidence links stay fresh. */
export function useVerificationQueue(enabled = true) {
  return useQuery({
    queryKey: ["admin", "verification"],
    queryFn: () => api.get<VerificationQueueItem[]>("/admin/verification"),
    enabled,
    refetchInterval: 30_000,
  });
}

/** The review moderation queue (P3.6): reported reviews and replies, and reviews held out by the self-dealing rule.
 * Shared by the nav badge and the Moderation page; polled every 30 seconds, like the verification queue. */
export function useModerationQueue(enabled = true) {
  return useQuery({
    queryKey: ["admin", "moderation"],
    queryFn: () => api.get<ModerationQueue>("/admin/reviews/queue"),
    enabled,
    refetchInterval: 30_000,
  });
}

const MIN_REASON = 10; // backend/app/api/routes/admin.py, VerificationDecisionIn: the owner reads it too

/** Asks for the reason behind a rejection or a suspension, which the owner sees. */
export function ReasonDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  pending,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  confirmLabel: string;
  pending: boolean;
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  const short = reason.trim().length < MIN_REASON;
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (pending) return;
        if (!o) setReason("");
        onOpenChange(o);
      }}
    >
      <DialogContent>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!short) onConfirm(reason.trim());
          }}
          className="grid gap-4"
        >
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>{description}</DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="decision-reason">Reason the owner will see</Label>
            <Textarea id="decision-reason" rows={3} value={reason} onChange={(e) => setReason(e.target.value.slice(0, 500))} aria-describedby="decision-reason-hint" />
            <p id="decision-reason-hint" className="text-xs text-muted-foreground">At least {MIN_REASON} characters. Say what to fix.</p>
          </div>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline" disabled={pending}>Cancel</Button>
            </DialogClose>
            <Button type="submit" variant="destructive" disabled={pending || short}>
              {pending && <Loader2 className="animate-spin" />} {confirmLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
