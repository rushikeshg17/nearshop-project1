"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Copy, Loader2, Mail, MapPin, MessageSquareWarning, Phone, Search, TriangleAlert, UserRound } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";

import { ListSkeleton } from "@/components/common/states";
import { useI18n } from "@/components/providers";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useMe } from "@/hooks/use-session";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { type LanguageCode, languageInfo } from "@/lib/i18n";
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

// English labels for the admin console, which stays in English (plan section 8.16). This page shows
// support.categories and support.statuses from the message files.
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

const CATEGORIES = Object.keys(CATEGORY_LABEL) as GrievanceCategory[];

function formatDate(iso: string | null, language: LanguageCode) {
  return iso
    ? new Date(iso).toLocaleDateString(languageInfo(language).htmlLang, { day: "numeric", month: "short", year: "numeric" })
    : "-";
}

function OfficerCard() {
  const { t } = useI18n();
  const officer = useQuery({ queryKey: ["grievance-officer"], queryFn: () => api.get<Officer>("/grievances/officer"), staleTime: 10 * 60_000 });
  const o = officer.data;
  return (
    <section aria-labelledby="officer-title" className="rounded-2xl border bg-card p-5">
      <h2 id="officer-title" className="font-semibold">{t("support.officer.title")}</h2>
      {officer.isError ? (
        <p className="mt-2 text-sm text-destructive">{t("support.officer.loadError")}</p>
      ) : !o ? (
        <ListSkeleton rows={2} />
      ) : (
        <>
          {o.placeholder && (
            <p className="mt-3 flex items-start gap-2 rounded-xl bg-warning-soft px-3 py-2 text-sm text-warning-ink">
              <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
              {t("support.officer.placeholder")}
            </p>
          )}
          <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
            <div className="flex gap-2">
              <dt><UserRound className="size-4 text-muted-foreground" aria-label={t("support.officer.name")} /></dt>
              <dd><span className="font-medium">{o.name}</span><br /><span className="text-muted-foreground">{o.designation}</span></dd>
            </div>
            <div className="flex gap-2">
              <dt><Mail className="size-4 text-muted-foreground" aria-label={t("support.officer.email")} /></dt>
              <dd className="break-all">{o.placeholder ? o.email : <a href={`mailto:${o.email}`} className="underline-offset-4 hover:underline">{o.email}</a>}</dd>
            </div>
            <div className="flex gap-2">
              <dt><Phone className="size-4 text-muted-foreground" aria-label={t("support.officer.phone")} /></dt>
              <dd>{o.placeholder ? o.phone : <a href={`tel:${o.phone.replace(/[^\d+]/g, "")}`} className="underline-offset-4 hover:underline">{o.phone}</a>}</dd>
            </div>
            <div className="flex gap-2">
              <dt><MapPin className="size-4 text-muted-foreground" aria-label={t("support.officer.address")} /></dt>
              <dd className="whitespace-pre-line">{o.address}</dd>
            </div>
          </dl>
        </>
      )}
    </section>
  );
}

function Acknowledgement({ filed, message, onAnother }: { filed: Grievance; message: string; onAnother: () => void }) {
  const { t, language } = useI18n();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(filed.ticket);
      toast.success(t("support.acknowledgement.copied"));
    } catch {
      toast.error(t("support.acknowledgement.copyFailed"));
    }
  };
  return (
    <div role="status" className="rounded-2xl border border-success/30 bg-success-soft/40 p-5">
      <p className="flex items-center gap-2 font-semibold"><CheckCircle2 className="size-5 text-success" aria-hidden /> {t("support.acknowledgement.title")}</p>
      <p className="mt-3 text-sm text-muted-foreground">{t("support.acknowledgement.ticket")}</p>
      <div className="mt-1 flex flex-wrap items-center gap-3">
        <span className="font-mono text-2xl font-semibold tracking-wider">{filed.ticket}</span>
        <Button size="sm" variant="outline" onClick={copy}><Copy /> {t("support.acknowledgement.copy")}</Button>
      </div>
      <p className="mt-3 text-sm">{message}</p>
      <p className="mt-2 text-sm text-muted-foreground">
        {t("support.acknowledgement.dates", {
          received: formatDateTime(filed.received_at, language),
          due: formatDate(filed.due_at, language),
        })}
      </p>
      <Button className="mt-4" variant="ghost" size="sm" onClick={onAnother}>{t("support.acknowledgement.another")}</Button>
    </div>
  );
}

function FileForm() {
  const id = useId();
  const { t } = useI18n();
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
        <h2 id={`${id}-title`} className="font-semibold">{t("support.file.title")}</h2>
        <p className="text-sm text-muted-foreground">{t(me ? "support.file.signedIn" : "support.file.signedOut")}</p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${id}-category`}>{t("support.file.category")}</Label>
        <Select value={category} onValueChange={(v) => setCategory(v as GrievanceCategory)}>
          <SelectTrigger id={`${id}-category`} className="w-full"><SelectValue placeholder={t("support.file.categoryPlaceholder")} /></SelectTrigger>
          <SelectContent>
            {CATEGORIES.map((c) => <SelectItem key={c} value={c}>{t(`support.categories.${c}`)}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-contact`}>{t("support.file.contact")}</Label>
          <Input id={`${id}-contact`} value={contactValue} onChange={(e) => setContact(e.target.value.slice(0, 254))} autoComplete="email"
            placeholder={t("support.file.contactPlaceholder")} required aria-describedby={`${id}-contact-hint`} />
          <p id={`${id}-contact-hint`} className="text-xs text-muted-foreground">{t("support.file.contactHint")}</p>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-code`}>
            {t("support.file.code")} <span className="font-normal text-muted-foreground">{t("support.file.optional")}</span>
          </Label>
          <Input id={`${id}-code`} value={orderCode} onChange={(e) => setOrderCode(e.target.value.slice(0, 20))} placeholder="D-7KQ2MX" autoCapitalize="characters" />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${id}-description`}>{t("support.file.description")}</Label>
        <Textarea id={`${id}-description`} rows={5} value={description} onChange={(e) => setDescription(e.target.value.slice(0, DESCRIPTION_MAX))}
          placeholder={t("support.file.descriptionPlaceholder")} required aria-describedby={`${id}-description-count`} />
        <p id={`${id}-description-count`} className="text-right text-xs text-muted-foreground tabular">{description.length} / {DESCRIPTION_MAX}</p>
      </div>
      {submit.isError && <p role="alert" className="text-sm text-destructive">{errorMessage(submit.error)}</p>}
      <Button type="submit" disabled={!ready || submit.isPending}>
        {submit.isPending ? <Loader2 className="animate-spin" /> : <MessageSquareWarning />} {t("support.file.submit")}
      </Button>
    </form>
  );
}

export function GrievanceStatusCard({ g }: { g: Grievance }) {
  const { t, language } = useI18n();
  const steps: [string, string | null][] = [
    [t("support.status.received"), g.acknowledged_at ?? g.received_at],
    [t("support.status.inProgress"), g.in_progress_at],
    [t("support.status.resolved"), g.resolved_at],
  ];
  return (
    <div className="rounded-2xl border bg-card p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono font-semibold">{g.ticket}</span>
        <Badge variant={g.status === "resolved" ? "secondary" : "outline"}>{t(`support.statuses.${g.status}`)}</Badge>
        <span className="ml-auto text-xs text-muted-foreground">{t(`support.categories.${g.category}`)}</span>
      </div>
      <p className="mt-2 line-clamp-3 text-sm text-muted-foreground">{g.description}</p>
      <ol className="mt-3 space-y-1 text-sm">
        {steps.map(([label, at]) => (
          <li key={label} className={cn("flex justify-between gap-3", !at && "text-muted-foreground")}>
            <span>{label}</span>
            <span className="tabular">{at ? formatDateTime(at, language) : "-"}</span>
          </li>
        ))}
      </ol>
      {g.status !== "resolved" && <p className="mt-2 text-xs text-muted-foreground">{t("support.status.due", { date: formatDate(g.due_at, language) })}</p>}
      {g.resolution && (
        <p className="mt-3 rounded-xl border-l-2 border-brand/50 bg-muted/50 px-3 py-2 text-sm">
          <span className="block text-xs font-medium">{t("support.status.answer")}</span>
          {g.resolution}
        </p>
      )}
    </div>
  );
}

function LookupForm({ initialTicket }: { initialTicket: string }) {
  const id = useId();
  const { t } = useI18n();
  const [ticket, setTicket] = useState(initialTicket);
  const [contact, setContact] = useState("");
  const lookup = useMutation({
    mutationFn: () => api.post<Grievance>("/grievances/lookup", { ticket: ticket.trim(), contact: contact.trim() }),
  });
  return (
    <section aria-labelledby={`${id}-title`} className="space-y-4 rounded-2xl border bg-card p-5">
      <div>
        <h2 id={`${id}-title`} className="font-semibold">{t("support.lookup.title")}</h2>
        <p className="text-sm text-muted-foreground">{t("support.lookup.intro")}</p>
      </div>
      <form
        className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          if (ticket.trim() && contact.trim()) lookup.mutate();
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-ticket`}>{t("support.lookup.ticket")}</Label>
          <Input id={`${id}-ticket`} value={ticket} onChange={(e) => setTicket(e.target.value.slice(0, 20))} placeholder="G-7KQ2MX" autoCapitalize="characters" required />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-contact`}>{t("support.lookup.contact")}</Label>
          <Input id={`${id}-contact`} value={contact} onChange={(e) => setContact(e.target.value.slice(0, 254))} required />
        </div>
        <Button type="submit" variant="outline" disabled={lookup.isPending || !ticket.trim() || !contact.trim()}>
          {lookup.isPending ? <Loader2 className="animate-spin" /> : <Search />} {t("support.lookup.submit")}
        </Button>
      </form>
      {lookup.isError && <p role="alert" className="text-sm text-destructive">{errorMessage(lookup.error)}</p>}
      {lookup.data && <div role="status"><GrievanceStatusCard g={lookup.data} /></div>}
    </section>
  );
}

function MyGrievances() {
  const { t } = useI18n();
  const { data: me } = useMe();
  const mine = useQuery({ queryKey: ["grievances", "mine"], queryFn: () => api.get<Grievance[]>("/grievances/mine"), enabled: !!me });
  if (!me || !mine.data?.length) return null;
  return (
    <section aria-labelledby="mine-title" className="space-y-3">
      <h2 id="mine-title" className="font-semibold">{t("support.mine")}</h2>
      <ul className="space-y-3">
        {mine.data.map((g) => <li key={g.ticket}><GrievanceStatusCard g={g} /></li>)}
      </ul>
    </section>
  );
}

export function GrievanceView({ initialTicket }: { initialTicket: string }) {
  const { t } = useI18n();
  return (
    <div className="mx-auto max-w-3xl space-y-6 px-4 pt-8 md:px-6 md:pt-12">
      <div>
        <p className="text-sm text-muted-foreground">{t("support.eyebrow")}</p>
        <h1 className="text-3xl font-semibold">{t("support.title")}</h1>
        <p className="mt-2 text-muted-foreground">{t("support.intro")}</p>
      </div>
      <OfficerCard />
      <MyGrievances />
      <FileForm />
      <LookupForm initialTicket={initialTicket} />
    </div>
  );
}
