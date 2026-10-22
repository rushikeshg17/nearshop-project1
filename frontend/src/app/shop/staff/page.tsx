"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, UserPlus, Users } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { useI18n } from "@/components/providers";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorMessage } from "@/lib/api";
import type { StaffMember, StaffOverview } from "@/lib/types";

import { formatDay } from "../owner-shell";

/**
 * Staff logins (P9.13, plan section 8.18). The owner invites counter staff by phone; each signs in with a code sent to
 * their own number and works the reservation and order queues, stock and imports. The plan's logins count the owner,
 * so the page shows how many are in use with the plan line, refuses an invitation beyond them (the API's plan_limit)
 * and lists who is invited, active or paused (beyond the plan's logins after a lapse, until renewal). Removing someone
 * signs them out at once.
 */

const PHONE = /^[6-9]\d{9}$/;
const KEY = ["owner", "staff"];

export default function StaffPage() {
  const { t } = useI18n();
  const q = useQuery({ queryKey: KEY, queryFn: () => api.get<StaffOverview>("/owner/staff") });
  const header = <PageTitle title={t("owner.staff.title")} description={t("owner.staff.description")} />;
  if (q.isLoading) return <>{header}<ListSkeleton rows={3} /></>;
  if (q.error || !q.data) return <>{header}<ErrorState error={q.error} onRetry={() => q.refetch()} /></>;
  const data = q.data;
  return (
    <>
      {header}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="space-y-6">
          <Limit data={data} />
          <InviteForm full={data.logins.available === 0} />
        </div>
        <StaffList staff={data.staff} />
      </div>
    </>
  );
}

function Limit({ data }: { data: StaffOverview }) {
  const { t } = useI18n();
  const { limit, used } = data.logins;
  return (
    <section className="space-y-2 rounded-3xl border bg-card p-5" data-testid="staff-limit" data-limit={limit} data-used={used}>
      <p className="text-lg font-semibold">{t("owner.staff.logins", { used, limit })}</p>
      <p className="text-sm text-muted-foreground">
        {limit > 1 ? t("owner.staff.planLine", { plan: data.plan.name, limit }) : t("owner.staff.planLineOne", { plan: data.plan.name })}
      </p>
      <Button asChild variant="outline" size="sm">
        <Link href="/shop/billing">{t("owner.staff.seePlans")}</Link>
      </Button>
    </section>
  );
}

function InviteForm({ full }: { full: boolean }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [agreed, setAgreed] = useState(false);
  const invite = useMutation({
    mutationFn: () => api.post<StaffOverview>("/owner/staff", { name: name.trim(), phone, agreed }),
    onSuccess: (data) => {
      qc.setQueryData(KEY, data);
      toast.success(t("owner.staff.invite.done", { name: name.trim() }));
      setName("");
      setPhone("");
      setAgreed(false);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const ready = name.trim().length >= 2 && PHONE.test(phone) && agreed && !full;
  return (
    <section aria-labelledby="invite-title" className="space-y-4 rounded-3xl border bg-card p-5">
      <h2 id="invite-title" className="flex items-center gap-2 text-base font-semibold"><UserPlus className="size-4" aria-hidden /> {t("owner.staff.invite.heading")}</h2>
      {full && <p className="text-sm text-muted-foreground" data-testid="staff-full">{t("owner.staff.invite.full")}</p>}
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready) invite.mutate();
        }}
      >
        <div className="space-y-2">
          <Label htmlFor="staff-name">{t("owner.staff.invite.name")}</Label>
          <Input id="staff-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} autoComplete="off" disabled={full} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="staff-phone">{t("owner.staff.invite.phone")}</Label>
          <Input
            id="staff-phone"
            value={phone}
            onChange={(e) => setPhone(e.target.value.replace(/\D/g, "").slice(0, 10))}
            inputMode="numeric"
            autoComplete="off"
            aria-describedby="staff-phone-hint"
            disabled={full}
          />
          <p id="staff-phone-hint" className="text-xs text-muted-foreground">{t("owner.staff.invite.phoneHint")}</p>
        </div>
        <div className="flex items-start gap-2">
          <Checkbox id="staff-agreed" checked={agreed} onCheckedChange={(v) => setAgreed(v === true)} disabled={full} />
          <Label htmlFor="staff-agreed" className="text-sm leading-snug font-normal">{t("owner.staff.invite.agreed")}</Label>
        </div>
        <Button type="submit" disabled={!ready || invite.isPending}>
          {invite.isPending && <Loader2 className="animate-spin" aria-hidden />}
          {t("owner.staff.invite.submit")}
        </Button>
      </form>
    </section>
  );
}

function StaffList({ staff }: { staff: StaffMember[] }) {
  const { t } = useI18n();
  const [removing, setRemoving] = useState<StaffMember | null>(null);
  return (
    <section aria-labelledby="staff-list-title" className="space-y-4 rounded-3xl border bg-card p-5">
      <h2 id="staff-list-title" className="text-base font-semibold">{t("owner.staff.list.heading")}</h2>
      {staff.length === 0 ? (
        <EmptyState icon={Users} title={t("owner.staff.list.empty")} description={t("owner.staff.list.emptyBody")} />
      ) : (
        <ul className="divide-y" data-testid="staff-list">
          {staff.map((m) => <StaffRow key={m.id} member={m} onRemove={() => setRemoving(m)} />)}
        </ul>
      )}
      <RemoveDialog member={removing} onClose={() => setRemoving(null)} />
    </section>
  );
}

function StaffRow({ member: m, onRemove }: { member: StaffMember; onRemove: () => void }) {
  const { t, language } = useI18n();
  const since =
    m.status === "suspended" && m.suspended_at
      ? t("owner.staff.list.suspendedOn", { date: formatDay(m.suspended_at, language) })
      : m.status === "active" && m.activated_at
        ? t("owner.staff.list.activeSince", { date: formatDay(m.activated_at, language) })
        : t("owner.staff.list.invitedOn", { date: formatDay(m.invited_at, language) });
  const note = m.status === "invited" && (m.invitation === "whatsapp_off" || m.invitation === "failed")
    ? t(m.invitation === "failed" ? "owner.staff.list.failed" : "owner.staff.list.whatsappOff")
    : null;
  return (
    <li className="flex flex-wrap items-start justify-between gap-3 py-3" data-testid="staff-row" data-status={m.status}>
      <div className="min-w-0 space-y-1">
        <p className="flex flex-wrap items-center gap-2 font-medium">
          <span className="truncate">{m.name}</span>
          <Badge variant={m.status === "active" ? "default" : "secondary"}>{t(`owner.staff.status.${m.status}`)}</Badge>
        </p>
        <p className="text-sm text-muted-foreground tabular-nums">{m.phone}</p>
        <p className="text-xs text-muted-foreground">{since}</p>
        {note && <p className="text-xs text-muted-foreground">{note}</p>}
      </div>
      <Button variant="outline" size="sm" onClick={onRemove}>
        {m.status === "invited" ? t("owner.staff.list.withdraw") : t("owner.staff.list.remove")}
      </Button>
    </li>
  );
}

function RemoveDialog({ member, onClose }: { member: StaffMember | null; onClose: () => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const remove = useMutation({
    mutationFn: (id: number) => api.delete<StaffOverview>(`/owner/staff/${id}`),
    onSuccess: (data) => {
      qc.setQueryData(KEY, data);
      if (member) toast.success(t("owner.staff.removed", { name: member.name }));
      onClose();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Dialog open={member !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("owner.staff.confirm.title", { name: member?.name ?? "" })}</DialogTitle>
          <DialogDescription>{t("owner.staff.confirm.body")}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">{t("owner.staff.confirm.cancel")}</Button>
          </DialogClose>
          <Button variant="destructive" disabled={remove.isPending || !member} onClick={() => member && remove.mutate(member.id)}>
            {remove.isPending && <Loader2 className="animate-spin" aria-hidden />}
            {t("owner.staff.confirm.action")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
