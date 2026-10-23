"use client";

import { useQuery } from "@tanstack/react-query";
import { Bike } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { StatusBadge } from "@/components/common/status";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { OwnerOrderCard } from "@/components/owner/queue-cards";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { formatDateTime, formatPrice } from "@/lib/format";
import type { Order, WorkOverview } from "@/lib/types";

export default function OwnerOrdersPage() {
  const { t, language } = useI18n();
  const [scope, setScope] = useState<"active" | "history">("active");
  const overview = useQuery({ queryKey: ["owner", "overview"], queryFn: () => api.get<WorkOverview>("/owner/overview") });
  const q = useQuery({
    queryKey: ["owner", "orders", scope],
    queryFn: () => api.get<Order[]>("/owner/orders", { scope }),
    refetchInterval: scope === "active" ? 10_000 : false,
  });

  if (overview.data && !overview.data.shop.offers_delivery && scope === "active" && !q.data?.length) {
    return (
      <>
        <PageTitle title={t("owner.orders.title")} />
        <EmptyState icon={Bike} title={t("owner.orders.pickupOnlyTitle")} description={t("owner.orders.pickupOnlyBody")}
          action={<Button asChild><Link href="/shop/settings">{t("owner.orders.setUpDelivery")}</Link></Button>} />
      </>
    );
  }

  return (
    <>
      <PageTitle
        title={t("owner.orders.title")}
        description={t("owner.orders.description")}
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
        q.data.length ? (
          <ul className="grid grid-cols-1 gap-4 lg:grid-cols-2">{q.data.map((o) => <OwnerOrderCard key={o.id} o={o} />)}</ul>
        ) : (
          <EmptyState icon={Bike} title={t("owner.orders.emptyTitle")} description={t("owner.orders.emptyBody")} />
        )
      ) : (
        <div className="overflow-hidden rounded-2xl border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("owner.common.table.code")}</TableHead>
                <TableHead>{t("owner.orders.table.items")}</TableHead>
                <TableHead className="hidden md:table-cell">{t("owner.common.table.customer")}</TableHead>
                <TableHead>{t("owner.common.table.status")}</TableHead>
                <TableHead className="text-right">{t("owner.common.table.total")}</TableHead>
                <TableHead className="hidden lg:table-cell">{t("owner.orders.table.when")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {q.data.map((o) => (
                <TableRow key={o.id}>
                  <TableCell className="font-mono text-xs">{o.code}</TableCell>
                  <TableCell className="max-w-56 truncate">{o.items.map((i) => i.name).join(", ")}</TableCell>
                  <TableCell className="hidden md:table-cell">{o.customer?.name}</TableCell>
                  <TableCell><StatusBadge status={o.status} kind="order" /></TableCell>
                  <TableCell className="text-right tabular">{formatPrice(o.total)}</TableCell>
                  <TableCell className="hidden text-muted-foreground lg:table-cell">{formatDateTime(o.delivered_at ?? o.closed_at ?? o.created_at, language)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </>
  );
}
