"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Hand, Loader2, MessageSquareWarning } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";

import type { Grievance } from "@/app/(site)/grievance/grievance-view";
import { CATEGORY_LABEL, STATUS_LABEL } from "@/app/(site)/grievance/grievance-view";
import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

// GET /api/admin/grievances (backend/app/api/routes/admin.py, P3.8).
interface AdminGrievance extends Grievance {
  id: number;
  contact: string;
  contact_kind: "email" | "phone";
  user_id: number | null;
  order_id: number | null;
  reservation_id: number | null;
  days_open: number;
  needs_attention: boolean;
  past_due: boolean;
}

interface GrievanceQueue {
  items: AdminGrievance[];
  total: number;
  resolve_days: number;
  warn_days: number;
}

type QueueState = "unresolved" | "resolved" | "all";

function formatDate(iso: string | null) {
  return iso ? new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "-";
}

function GrievanceItem({ g, warnDays }: { g: AdminGrievance; warnDays: number }) {
  const id = useId();
  const qc = useQueryClient();
  const [resolution, setResolution] = useState("");
  const act = useMutation({
    mutationFn: (status: "in_progress" | "resolved") =>
      api.post(`/admin/grievances/${g.id}/status`, status === "resolved" ? { status, resolution: resolution.trim() } : { status }),
    onSuccess: (_d, status) => {
      toast.success(status === "resolved" ? `${g.ticket} resolved.` : `${g.ticket} is in progress.`);
      qc.invalidateQueries({ queryKey: ["admin", "grievances"] });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const open = g.status !== "resolved";
  return (
    <li
      aria-label={`Grievance ${g.ticket}`}
      className={cn("rounded-2xl border bg-card p-4", open && g.needs_attention && "border-destructive/60 bg-destructive/5")}
    >
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-mono text-sm font-semibold">{g.ticket}</span>
        <Badge variant={open ? "outline" : "secondary"}>{STATUS_LABEL[g.status]}</Badge>
        <Badge variant="secondary">{CATEGORY_LABEL[g.category]}</Badge>
        {g.order_code && <Badge variant="outline">{g.order_code}</Badge>}
        <span className={cn("ml-auto tabular", open && g.needs_attention ? "font-semibold text-destructive" : "text-muted-foreground")}>
          {open ? (
            <>
              Due {formatDate(g.due_at)} · day {g.days_open}
              {g.past_due ? " (past due)" : g.needs_attention ? ` (over ${warnDays} days)` : ""}
            </>
          ) : (
            <>Resolved {formatDateTime(g.resolved_at)} · took {g.days_open} d</>
          )}
        </span>
      </div>
      <p className="mt-2 whitespace-pre-line text-sm">{g.description}</p>
      <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <div><dt className="inline">Contact ({g.contact_kind}): </dt><dd className="inline break-all text-foreground">{g.contact}</dd></div>
        <div><dt className="inline">Received: </dt><dd className="inline">{formatDateTime(g.received_at)}</dd></div>
        <div><dt className="inline">Account: </dt><dd className="inline">{g.user_id ? `user ${g.user_id}` : "not signed in"}</dd></div>
        {g.in_progress_at && <div><dt className="inline">Picked up: </dt><dd className="inline">{formatDateTime(g.in_progress_at)}</dd></div>}
      </dl>
      {g.resolution && (
        <p className="mt-2 rounded-xl border-l-2 border-brand/50 bg-muted/50 px-3 py-2 text-sm">
          <span className="block text-xs font-medium">Resolution</span>
          {g.resolution}
        </p>
      )}
      {open && (
        <div className="mt-3 space-y-2 border-t pt-3">
          <Label htmlFor={`${id}-resolution`} className="text-xs">Resolution (the complainant reads this with their ticket)</Label>
          <Textarea id={`${id}-resolution`} rows={2} value={resolution} onChange={(e) => setResolution(e.target.value.slice(0, 2000))} />
          <div className="flex flex-wrap gap-2">
            {g.status !== "in_progress" && (
              <Button size="sm" variant="ghost" onClick={() => act.mutate("in_progress")} disabled={act.isPending}>
                {act.isPending && act.variables === "in_progress" ? <Loader2 className="animate-spin" /> : <Hand />} Pick up
              </Button>
            )}
            <Button size="sm" onClick={() => act.mutate("resolved")} disabled={act.isPending || resolution.trim().length < 10}>
              {act.isPending && act.variables === "resolved" ? <Loader2 className="animate-spin" /> : <CheckCircle2 />} Resolve
            </Button>
          </div>
        </div>
      )}
    </li>
  );
}

export default function AdminGrievancesPage() {
  const [state, setState] = useState<QueueState>("unresolved");
  const queue = useQuery({
    queryKey: ["admin", "grievances", state],
    queryFn: () => api.get<GrievanceQueue>("/admin/grievances", { state }),
  });
  const d = queue.data;
  return (
    <>
      <PageTitle
        title="Grievances"
        description="Every grievance is acknowledged when it is filed and must be resolved within 30 days of receipt. Unresolved grievances are oldest first; those open for more than 25 days are shown in red."
      />
      <Tabs value={state} onValueChange={(v) => setState(v as QueueState)} className="mb-4">
        <TabsList>
          <TabsTrigger value="unresolved">Unresolved</TabsTrigger>
          <TabsTrigger value="resolved">Resolved</TabsTrigger>
          <TabsTrigger value="all">All</TabsTrigger>
        </TabsList>
      </Tabs>
      {queue.isError ? (
        <ErrorState error={queue.error} onRetry={() => queue.refetch()} />
      ) : !d ? (
        <ListSkeleton />
      ) : !d.items.length ? (
        <EmptyState icon={MessageSquareWarning} title="No grievances here" description="Grievances filed on the public grievance page appear here." />
      ) : (
        <>
          {d.total > d.items.length && (
            <p className="mb-3 text-sm text-muted-foreground">Showing {d.items.length} of {d.total}.</p>
          )}
          <ul className="space-y-3">
            {d.items.map((g) => <GrievanceItem key={g.id} g={g} warnDays={d.warn_days} />)}
          </ul>
        </>
      )}
    </>
  );
}
