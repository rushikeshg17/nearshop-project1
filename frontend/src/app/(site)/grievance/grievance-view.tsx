"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Copy, Loader2, Mail, MapPin, MessageSquareWarning, Phone, Search, TriangleAlert, UserRound } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";

import { ListSkeleton } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useMe } from "@/hooks/use-session";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

// Shapes from backend/app/api/routes/grievances.py (P3.8).
interface Officer {
  name: string;
  designation: string;
  email: string;
  phone: string;
  address: string;
  placeholder: boolean;
  resolve_days: number;
}

type GrievanceCategory = "order" | "shop_conduct" | "privacy" | "account" | "other";
type GrievanceStatus = "open" | "acknowledged" | "in_progress" | "resolved";

export interface Grievance {
  ticket: string;
  category: GrievanceCategory;
  description: string;
  order_code: string | null;
  status: GrievanceStatus;
  received_at: string;
  acknowledged_at: string | null;
  due_at: string;
  in_progress_at: string | null;
  resolved_at: string | null;
  resolution: string | null;
  updated_at: string | null;
}

export const CATEGORY_LABEL: Record<GrievanceCategory, string> = {
  order: "An order or reservation",
  shop_conduct: "A shop's conduct",
  privacy: "Privacy or my data",
  account: "My account",
  other: "Something else",
};

export const STATUS_LABEL: Record<GrievanceStatus, string> = {
  open: "Open",
  acknowledged: "Acknowledged",
  in_progress: "In progress",
  resolved: "Resolved",
};

const DESCRIPTION_MAX = 2000;

function formatDate(iso: string | null) {
  return iso ? new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "-";
}

function OfficerCard() {
  const officer = useQuery({ queryKey: ["grievance-officer"], queryFn: () => api.get<Officer>("/grievances/officer"), staleTime: 10 * 60_000 });
  const o = officer.data;
  return (
    <section aria-labelledby="officer-title" className="rounded-2xl border bg-card p-5">
      <h2 id="officer-title" className="font-semibold">Grievance officer</h2>
      {officer.isError ? (
        <p className="mt-2 text-sm text-destructive">Could not load the grievance officer&apos;s details. You can still file a grievance below.</p>
      ) : !o ? (
        <ListSkeleton rows={2} />
      ) : (
        <>
          {o.placeholder && (
            <p className="mt-3 flex items-start gap-2 rounded-xl bg-warning-soft px-3 py-2 text-sm text-warning-ink">
              <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
              Placeholder details: the grievance officer has not been published yet. Grievances filed here are still recorded and answered.
            </p>
          )}
          <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
            <div className="flex gap-2">
              <dt><UserRound className="size-4 text-muted-foreground" aria-label="Name" /></dt>
              <dd><span className="font-medium">{o.name}</span><br /><span className="text-muted-foreground">{o.designation}</span></dd>
            </div>
            <div className="flex gap-2">
              <dt><Mail className="size-4 text-muted-foreground" aria-label="Email" /></dt>
              <dd className="break-all">{o.placeholder ? o.email : <a href={`mailto:${o.email}`} className="underline-offset-4 hover:underline">{o.email}</a>}</dd>
            </div>
            <div className="flex gap-2">
              <dt><Phone className="size-4 text-muted-foreground" aria-label="Phone" /></dt>
              <dd>{o.placeholder ? o.phone : <a href={`tel:${o.phone.replace(/[^\d+]/g, "")}`} className="underline-offset-4 hover:underline">{o.phone}</a>}</dd>
            </div>
            <div className="flex gap-2">
              <dt><MapPin className="size-4 text-muted-foreground" aria-label="Postal address" /></dt>
              <dd className="whitespace-pre-line">{o.address}</dd>
            </div>
          </dl>
        </>
      )}
    </section>
  );
}

function Acknowledgement({ filed, message, onAnother }: { filed: Grievance; message: string; onAnother: () => void }) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(filed.ticket);
      toast.success("Ticket number copied");
    } catch {
      toast.error("Could not copy. Write the ticket number down instead.");
    }
  };
  return (
    <div role="status" className="rounded-2xl border border-success/30 bg-success-soft/40 p-5">
      <p className="flex items-center gap-2 font-semibold"><CheckCircle2 className="size-5 text-success" aria-hidden /> Grievance received and acknowledged</p>
      <p className="mt-3 text-sm text-muted-foreground">Your ticket number</p>
      <div className="mt-1 flex flex-wrap items-center gap-3">
        <span className="font-mono text-2xl font-semibold tracking-wider">{filed.ticket}</span>
        <Button size="sm" variant="outline" onClick={copy}><Copy /> Copy</Button>
      </div>
      <p className="mt-3 text-sm">{message}</p>
      <p className="mt-2 text-sm text-muted-foreground">Received {formatDateTime(filed.received_at)}. Resolution due by {formatDate(filed.due_at)}.</p>
      <Button className="mt-4" variant="ghost" size="sm" onClick={onAnother}>File another grievance</Button>
    </div>
  );
}

function FileForm() {
  const id = useId();
  const { data: me } = useMe();
  const [category, setCategory] = useState<GrievanceCategory | "">("");
  // null: the signed-in account's phone or email, until the visitor types their own.
  const [contact, setContact] = useState<string | null>(null);
  const [orderCode, setOrderCode] = useState("");
  const [description, setDescription] = useState("");
  const contactValue = contact ?? me?.phone ?? me?.email ?? "";
  const queryClient = useQueryClient();
  const submit = useMutation({
    mutationFn: () =>
      api.post<Grievance & { message: string }>("/grievances", {
        category, contact: contactValue.trim(), order_code: orderCode.trim() || null, description: description.trim(),
      }),
    // A signed-in visitor sees the new ticket under "Your grievances" without reloading.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["grievances", "mine"] }),
  });

  if (submit.data) {
    return (
      <Acknowledgement filed={submit.data} message={submit.data.message} onAnother={() => {
        submit.reset();
        setCategory("");
        setOrderCode("");
        setDescription("");
      }} />
    );
  }

  const ready = category !== "" && contactValue.trim().length >= 3 && description.trim().length >= 10;
  return (
    <form
      aria-labelledby={`${id}-title`}
      className="space-y-4 rounded-2xl border bg-card p-5"
      onSubmit={(e) => {
        e.preventDefault();
        if (ready) submit.mutate();
      }}
    >
      <div>
        <h2 id={`${id}-title`} className="font-semibold">File a grievance</h2>
        <p className="text-sm text-muted-foreground">
          {me ? "It is linked to your account, and you will see the acknowledgement in the app too." : "You do not need an account. Give a phone number or email so we can reach you and you can track the grievance."}
        </p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${id}-category`}>What is it about?</Label>
        <Select value={category} onValueChange={(v) => setCategory(v as GrievanceCategory)}>
          <SelectTrigger id={`${id}-category`} className="w-full"><SelectValue placeholder="Choose a category" /></SelectTrigger>
          <SelectContent>
            {(Object.keys(CATEGORY_LABEL) as GrievanceCategory[]).map((c) => <SelectItem key={c} value={c}>{CATEGORY_LABEL[c]}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-contact`}>Phone or email</Label>
          <Input id={`${id}-contact`} value={contactValue} onChange={(e) => setContact(e.target.value.slice(0, 254))} autoComplete="email"
            placeholder="98765 43210 or you@example.com" required aria-describedby={`${id}-contact-hint`} />
          <p id={`${id}-contact-hint`} className="text-xs text-muted-foreground">You need it with the ticket number to check the status.</p>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-code`}>Order or reservation code <span className="font-normal text-muted-foreground">(optional)</span></Label>
          <Input id={`${id}-code`} value={orderCode} onChange={(e) => setOrderCode(e.target.value.slice(0, 20))} placeholder="D-7KQ2MX" autoCapitalize="characters" />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${id}-description`}>What happened?</Label>
        <Textarea id={`${id}-description`} rows={5} value={description} onChange={(e) => setDescription(e.target.value.slice(0, DESCRIPTION_MAX))}
          placeholder="Tell us what went wrong, when, and what you would like us to do." required aria-describedby={`${id}-description-count`} />
        <p id={`${id}-description-count`} className="text-right text-xs text-muted-foreground tabular">{description.length} / {DESCRIPTION_MAX}</p>
      </div>
      {submit.isError && <p role="alert" className="text-sm text-destructive">{errorMessage(submit.error)}</p>}
      <Button type="submit" disabled={!ready || submit.isPending}>
        {submit.isPending ? <Loader2 className="animate-spin" /> : <MessageSquareWarning />} Submit grievance
      </Button>
    </form>
  );
}

export function GrievanceStatusCard({ g }: { g: Grievance }) {
  const steps: [string, string | null][] = [
    ["Received and acknowledged", g.acknowledged_at ?? g.received_at],
    ["In progress", g.in_progress_at],
    ["Resolved", g.resolved_at],
  ];
  return (
    <div className="rounded-2xl border bg-card p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono font-semibold">{g.ticket}</span>
        <Badge variant={g.status === "resolved" ? "secondary" : "outline"}>{STATUS_LABEL[g.status]}</Badge>
        <span className="ml-auto text-xs text-muted-foreground">{CATEGORY_LABEL[g.category]}</span>
      </div>
      <p className="mt-2 line-clamp-3 text-sm text-muted-foreground">{g.description}</p>
      <ol className="mt-3 space-y-1 text-sm">
        {steps.map(([label, at]) => (
          <li key={label} className={cn("flex justify-between gap-3", !at && "text-muted-foreground")}>
            <span>{label}</span>
            <span className="tabular">{at ? formatDateTime(at) : "-"}</span>
          </li>
        ))}
      </ol>
      {g.status !== "resolved" && <p className="mt-2 text-xs text-muted-foreground">Resolution due by {formatDate(g.due_at)}.</p>}
      {g.resolution && (
        <p className="mt-3 rounded-xl border-l-2 border-brand/50 bg-muted/50 px-3 py-2 text-sm">
          <span className="block text-xs font-medium">The grievance officer&apos;s answer</span>
          {g.resolution}
        </p>
      )}
    </div>
  );
}

function LookupForm({ initialTicket }: { initialTicket: string }) {
  const id = useId();
  const [ticket, setTicket] = useState(initialTicket);
  const [contact, setContact] = useState("");
  const lookup = useMutation({
    mutationFn: () => api.post<Grievance>("/grievances/lookup", { ticket: ticket.trim(), contact: contact.trim() }),
  });
  return (
    <section aria-labelledby={`${id}-title`} className="space-y-4 rounded-2xl border bg-card p-5">
      <div>
        <h2 id={`${id}-title`} className="font-semibold">Check a grievance</h2>
        <p className="text-sm text-muted-foreground">Enter the ticket number and the phone or email you gave when you filed it.</p>
      </div>
      <form
        className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          if (ticket.trim() && contact.trim()) lookup.mutate();
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-ticket`}>Ticket number</Label>
          <Input id={`${id}-ticket`} value={ticket} onChange={(e) => setTicket(e.target.value.slice(0, 20))} placeholder="G-7KQ2MX" autoCapitalize="characters" required />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-contact`}>Phone or email</Label>
          <Input id={`${id}-contact`} value={contact} onChange={(e) => setContact(e.target.value.slice(0, 254))} required />
        </div>
        <Button type="submit" variant="outline" disabled={lookup.isPending || !ticket.trim() || !contact.trim()}>
          {lookup.isPending ? <Loader2 className="animate-spin" /> : <Search />} Check status
        </Button>
      </form>
      {lookup.isError && <p role="alert" className="text-sm text-destructive">{errorMessage(lookup.error)}</p>}
      {lookup.data && <div role="status"><GrievanceStatusCard g={lookup.data} /></div>}
    </section>
  );
}

function MyGrievances() {
  const { data: me } = useMe();
  const mine = useQuery({ queryKey: ["grievances", "mine"], queryFn: () => api.get<Grievance[]>("/grievances/mine"), enabled: !!me });
  if (!me || !mine.data?.length) return null;
  return (
    <section aria-labelledby="mine-title" className="space-y-3">
      <h2 id="mine-title" className="font-semibold">Your grievances</h2>
      <ul className="space-y-3">
        {mine.data.map((g) => <li key={g.ticket}><GrievanceStatusCard g={g} /></li>)}
      </ul>
    </section>
  );
}

export function GrievanceView({ initialTicket }: { initialTicket: string }) {
  return (
    <div className="mx-auto max-w-3xl space-y-6 px-4 pt-8 md:px-6 md:pt-12">
      <div>
        <p className="text-sm text-muted-foreground">Help</p>
        <h1 className="text-3xl font-semibold">Grievances</h1>
        <p className="mt-2 text-muted-foreground">
          If something went wrong with an order, a shop, your account or your personal data, tell us here. Every grievance gets a ticket number and is acknowledged as soon as you file it, and the grievance officer resolves it within 30 days.
        </p>
      </div>
      <OfficerCard />
      <MyGrievances />
      <FileForm />
      <LookupForm initialTicket={initialTicket} />
    </div>
  );
}
