"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Search, ShieldOff } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { ReasonDialog } from "@/app/admin/admin-shell";
import { Freshness, Rating } from "@/components/common/indicators";
import { ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useDebounce } from "@/hooks/use-debounce";
import { api, errorMessage } from "@/lib/api";
import type { AdminShopRow } from "@/lib/types";

function day(iso: string | null) {
  return iso ? new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" }) : "";
}

/** The shop's verification as an admin needs it: the status, and whether customers can see the shop. */
function VerificationStatusCell({ s, now }: { s: AdminShopRow; now: number }) {
  const pastDate = s.verification_status === "verified" && !!s.verified_until && new Date(s.verified_until).getTime() <= now;
  // A shop whose renewal was submitted in time stays visible past verified_until while it is checked (P3.3), so
  // a past date only means "Expired" when customers cannot see the shop.
  const renewing = pastDate && s.visible_to_customers;
  const expired = pastDate && !renewing;
  const label = expired
    ? "Expired"
    : { draft: "Draft", submitted: "Submitted", verified: "Verified", rejected: "Rejected", suspended: "Suspended" }[s.verification_status];
  const variant = s.verification_status === "verified" && !expired ? "default" : s.verification_status === "rejected" || s.verification_status === "suspended" || expired ? "destructive" : "secondary";
  return (
    <div className="space-y-0.5">
      <Badge variant={variant}>{label}</Badge>
      {s.verification_status === "verified" && s.verified_until && (
        <p className="text-xs text-muted-foreground">
          {pastDate ? "ended" : "until"} {day(s.verified_until)}{renewing && ", renewal being checked"}
        </p>
      )}
      <p className="text-xs text-muted-foreground">{s.visible_to_customers ? "Visible to customers" : "Hidden from customers"}</p>
    </div>
  );
}

export default function AdminShopsPage() {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const q = useDebounce(text, 250);
  const [suspending, setSuspending] = useState<AdminShopRow | null>(null);
  const [now] = useState(() => Date.now());
  const shops = useQuery({ queryKey: ["admin", "shops", q], queryFn: () => api.get<AdminShopRow[]>("/admin/shops", { q }), placeholderData: keepPreviousData });
  const update = useMutation({
    mutationFn: ({ id, body }: { id: number; body: { is_active?: boolean } }) => api.patch(`/admin/shops/${id}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin"] });
      toast.success("Shop updated");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const suspend = useMutation({
    mutationFn: ({ id, reason }: { id: number; reason: string }) => api.post(`/admin/shops/${id}/verification/suspend`, { reason }),
    onSuccess: () => {
      setSuspending(null);
      qc.invalidateQueries({ queryKey: ["admin"] });
      toast.success("Shop suspended and hidden from customers");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  return (
    <>
      <PageTitle
        title="Shops"
        description={
          <>
            Shops are verified from their evidence in <Link href="/admin/verification" className="underline underline-offset-3">Verification</Link>.
            Suspending hides a verified shop until it is verified again; deactivating hides a shop without deleting anything.
          </>
        }
      />
      <div className="relative mb-4 max-w-sm">
        <Search className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="Search shops or areas" className="h-9 pl-9" />
      </div>
      {shops.isError ? (
        <ErrorState error={shops.error} onRetry={() => shops.refetch()} />
      ) : !shops.data ? (
        <ListSkeleton />
      ) : (
        <div className="overflow-x-auto rounded-2xl border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Shop</TableHead>
                <TableHead className="hidden md:table-cell">Owner</TableHead>
                <TableHead className="text-right">Listings</TableHead>
                <TableHead className="hidden lg:table-cell">Rating</TableHead>
                <TableHead className="hidden lg:table-cell">Stock updated</TableHead>
                <TableHead>Verification</TableHead>
                <TableHead>Active</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {shops.data.map((s) => (
                <TableRow key={s.id}>
                  <TableCell>
                    <Link href={`/shops/${s.slug}`} className="font-medium hover:underline">{s.name}</Link>
                    <p className="text-xs text-muted-foreground">{s.locality}</p>
                  </TableCell>
                  <TableCell className="hidden md:table-cell">
                    <p>{s.owner}</p>
                    <p className="text-xs text-muted-foreground">{s.owner_email}</p>
                  </TableCell>
                  <TableCell className="text-right tabular">{s.listings}</TableCell>
                  <TableCell className="hidden lg:table-cell"><Rating value={s.rating_avg} count={s.rating_count} /></TableCell>
                  <TableCell className="hidden lg:table-cell"><Freshness at={s.inventory_updated_at} prefix="" /></TableCell>
                  <TableCell>
                    <div className="flex items-start gap-2">
                      <VerificationStatusCell s={s} now={now} />
                      {s.verification_status === "verified" && (
                        <Button size="icon-sm" variant="ghost" onClick={() => setSuspending(s)} aria-label={`Suspend ${s.name}`} title="Suspend">
                          <ShieldOff />
                        </Button>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Switch checked={s.is_active} onCheckedChange={(v) => update.mutate({ id: s.id, body: { is_active: v } })} aria-label={`Active: ${s.name}`} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <ReasonDialog
        key={suspending?.id ?? "none"}
        open={!!suspending}
        onOpenChange={(o) => !o && setSuspending(null)}
        title={`Suspend ${suspending?.name ?? "shop"}?`}
        description="Customers stop seeing the shop at once. The owner sees your reason and must submit fresh evidence to be verified again."
        confirmLabel="Suspend"
        pending={suspend.isPending}
        onConfirm={(reason) => suspending && suspend.mutate({ id: suspending.id, reason })}
      />
    </>
  );
}
