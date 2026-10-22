"use client";

import { useQuery } from "@tanstack/react-query";
import { ClipboardList } from "lucide-react";
import { AnimatePresence } from "motion/react";
import { useState } from "react";

import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { StatusBadge } from "@/components/common/status";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { OwnerReservationCard } from "@/components/owner/queue-cards";
import { useI18n } from "@/components/providers";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { formatDateTime, formatNumber, formatPrice } from "@/lib/format";
import type { Reservation } from "@/lib/types";

// The active reservations, one column per state (owner.reservations.columns in src/messages).
const COLUMNS = ["REQUESTED", "CONFIRMED", "READY_FOR_PICKUP"] as const;

export default function OwnerReservationsPage() {
  const { t, language } = useI18n();
  const [scope, setScope] = useState<"active" | "history">("active");
  const q = useQuery({
    queryKey: ["owner", "reservations", scope],
    queryFn: () => api.get<Reservation[]>("/owner/reservations", { scope }),
    refetchInterval: scope === "active" ? 10_000 : false,
  });

  return (
    <>
      <PageTitle
        title={t("owner.reservations.title")}
        description={t("owner.reservations.description")}
        action={
          <Tabs value={scope} onValueChange={(v) => setScope(v as "active" | "history")}>
            <TabsList>
              <TabsTrigger value="active">{t("owner.common.active")}</TabsTrigger>
              <TabsTrigger value="history">{t("owner.common.history")}</TabsTrigger>
            </TabsList>
          </Tabs>
        }
      />
      {q.isError ? (
        <ErrorState error={q.error} onRetry={() => q.refetch()} />
      ) : !q.data ? (
        <ListSkeleton />
      ) : scope === "active" ? (
        q.data.length === 0 ? (
          <EmptyState icon={ClipboardList} title={t("owner.reservations.emptyTitle")} description={t("owner.reservations.emptyBody")} />
        ) : (
          <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
            {COLUMNS.map((c) => {
              const rows = q.data.filter((r) => r.status === c);
              return (
                <section key={c}>
                  <h2 className="flex items-center gap-2 font-semibold">
                    {t(`owner.reservations.columns.${c}.title`)} <span className="rounded-full bg-muted px-2 text-xs tabular">{rows.length}</span>
                  </h2>
                  <p className="mb-3 text-xs text-muted-foreground">{t(`owner.reservations.columns.${c}.hint`)}</p>
                  <ul className="space-y-3">
                    <AnimatePresence>{rows.map((r) => <OwnerReservationCard key={r.id} r={r} />)}</AnimatePresence>
                    {!rows.length && <li className="rounded-2xl border border-dashed p-6 text-center text-sm text-muted-foreground">{t("owner.common.nothingHere")}</li>}
                  </ul>
                </section>
              );
            })}
          </div>
        )
      ) : (
        <div className="overflow-hidden rounded-2xl border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("owner.common.table.code")}</TableHead>
                <TableHead>{t("owner.reservations.table.item")}</TableHead>
                <TableHead className="hidden md:table-cell">{t("owner.common.table.customer")}</TableHead>
                <TableHead>{t("owner.common.table.status")}</TableHead>
                <TableHead className="text-right">{t("owner.common.table.total")}</TableHead>
                <TableHead className="hidden lg:table-cell">{t("owner.reservations.table.updated")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {q.data.map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="font-mono text-xs">{r.code}</TableCell>
                  <TableCell className="max-w-56 truncate">{t("owner.common.quantityItem", { count: formatNumber(r.quantity), name: r.product.name })}</TableCell>
                  <TableCell className="hidden md:table-cell">{r.customer?.name}</TableCell>
                  <TableCell><StatusBadge status={r.status} kind="reservation" /></TableCell>
                  <TableCell className="text-right tabular">{formatPrice(r.total)}</TableCell>
                  <TableCell className="hidden text-muted-foreground lg:table-cell">{formatDateTime(r.closed_at ?? r.completed_at ?? r.created_at, language)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </>
  );
}
