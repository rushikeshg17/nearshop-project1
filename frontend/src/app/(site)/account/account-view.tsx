"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, History, MonitorSmartphone, Search, ShieldCheck, Sparkles, Star, Trash2, UserRound } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { ProductThumb } from "@/components/common/product-thumb";
import { EmptyState, ErrorState, ListSkeleton } from "@/components/common/states";
import { ActivityCard } from "@/components/fulfillment/activity-card";
import { ReviewDialog } from "@/components/fulfillment/review-dialog";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useNow } from "@/hooks/use-debounce";
import { clearOfflineReservations, useMe } from "@/hooks/use-session";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime, formatDistance, formatNumber, formatPrice, timeAgo } from "@/lib/format";
import { type MessageKey, plural } from "@/lib/i18n";
import { useLocation } from "@/lib/location";
import type { CustomerDashboard, Order, Reservation, User } from "@/lib/types";

const href = (i: Reservation | Order) => (i.kind === "reservation" ? `/account/reservations/${i.id}` : `/account/orders/${i.id}`);

export function AccountView() {
  const { t, language } = useI18n();
  const { data: me } = useMe();
  const [tab, setTab] = useState("all");
  const [reviewing, setReviewing] = useState<Reservation | Order | null>(null);
  const dash = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api.get<CustomerDashboard>("/me/dashboard"),
    enabled: me?.role === "customer",
    refetchInterval: 20_000,
  });

  if (me && me.role !== "customer") {
    return (
      <div className="mx-auto max-w-2xl px-4 py-16">
        <EmptyState icon={History} title={t("transactions.account.ownerTitle")} description={t("transactions.account.ownerBody")}
          action={<Button asChild><Link href={me.role === "owner" ? "/shop" : "/admin"}>{t("transactions.account.goToDashboard")}</Link></Button>} />
        <AccountSettings me={me} />
      </div>
    );
  }

  const d = dash.data;
  const active = d ? [...d.active_reservations, ...d.active_orders] : [];
  const history = (d?.history ?? []).filter((h) => tab === "all" || h.kind === tab);

  return (
    <div className="mx-auto max-w-5xl px-4 pt-8 md:px-6 md:pt-12">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-sm text-muted-foreground">{t("transactions.account.yourActivity")}</p>
          <h1 className="text-3xl font-semibold">
            {me ? t("transactions.account.hi", { name: me.name.split(" ")[0] }) : t("transactions.account.hiThere")}
          </h1>
        </div>
        {d && (
          <dl className="flex gap-6 text-sm">
            {(
              [
                [d.stats.completed_pickups, "pickups"],
                [d.stats.delivered_orders, "deliveries"],
                [d.stats.shops_visited, "shops"],
              ] as const
            ).map(([n, l]) => (
              <div key={l}>
                <dt className="font-heading text-2xl font-semibold tabular">{formatNumber(n)}</dt>
                <dd className="text-muted-foreground">{t(`transactions.account.stats.${l}.${plural(language, n)}`)}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>

      {dash.isError ? (
        <ErrorState className="mt-8" error={dash.error} onRetry={() => dash.refetch()} />
      ) : !d ? (
        <ListSkeleton className="mt-8" rows={3} />
      ) : (
        <div className="mt-8 grid gap-10 lg:grid-cols-[1fr_320px]">
          <div className="min-w-0 space-y-10">
            <section>
              <h2 className="mb-3 text-lg font-semibold">{t("transactions.account.inProgress")}</h2>
              {active.length ? (
                <div className="space-y-3">{active.map((i) => <ActivityCard key={`${i.kind}-${i.id}`} item={i} href={href(i)} />)}</div>
              ) : (
                <EmptyState icon={Search} title={t("transactions.account.nothingHeld")} description={t("transactions.account.nothingHeldBody")}
                  action={<Button asChild><Link href="/search">{t("transactions.account.searchNearby")}</Link></Button>} />
              )}
            </section>

            {d.to_review.length > 0 && (
              <section className="rounded-2xl border bg-brand-soft/40 p-4">
                <h2 className="flex items-center gap-2 font-semibold"><Star className="size-4 text-brand" /> {t("transactions.account.rateRecent")}</h2>
                <ul className="mt-3 space-y-2">
                  {d.to_review.map((i) => (
                    <li key={`${i.kind}-${i.id}`} className="flex items-center justify-between gap-3 rounded-xl bg-card p-3">
                      <span className="min-w-0 truncate text-sm">
                        <span className="font-medium">{i.shop.name}</span> ·{" "}
                        {i.kind === "reservation"
                          ? i.product.name
                          : t(`transactions.account.items.${plural(language, i.items.length)}`, { count: formatNumber(i.items.length) })}
                      </span>
                      <Button size="sm" onClick={() => setReviewing(i)}>{t("transactions.account.rate")}</Button>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section>
              <div className="mb-3 flex items-center justify-between gap-3">
                <h2 className="text-lg font-semibold">{t("transactions.account.history")}</h2>
                <Tabs value={tab} onValueChange={setTab}>
                  <TabsList>
                    <TabsTrigger value="all">{t("transactions.account.tabs.all")}</TabsTrigger>
                    <TabsTrigger value="reservation">{t("transactions.account.tabs.pickups")}</TabsTrigger>
                    <TabsTrigger value="order">{t("transactions.account.tabs.deliveries")}</TabsTrigger>
                  </TabsList>
                </Tabs>
              </div>
              {history.length ? (
                <div className="space-y-3">{history.map((i) => <ActivityCard key={`${i.kind}-${i.id}`} item={i} href={href(i)} />)}</div>
              ) : (
                <p className="rounded-2xl border border-dashed p-6 text-center text-sm text-muted-foreground">{tab === "order"
                    ? t("transactions.account.noPast.deliveries")
                    : tab === "reservation"
                      ? t("transactions.account.noPast.pickups")
                      : t("transactions.account.noPast.activity")}</p>
              )}
            </section>
          </div>

          <aside className="min-w-0 space-y-8">
            {d.recommendations.length > 0 && (
              <section>
                <h2 className="flex items-center gap-2 font-semibold"><Sparkles className="size-4 text-brand" /> {t("transactions.account.nextTitle")}</h2>
                <p className="mt-1 text-xs text-muted-foreground">{t("transactions.account.nextBody")}</p>
                <ul className="mt-3 space-y-2">
                  {d.recommendations.map((r) => (
                    <li key={r.offer.id}>
                      <Link href={`/product/${r.offer.id}`} className="flex items-center gap-3 rounded-xl border bg-card p-3 hover:bg-accent/50">
                        <ProductThumb icon={r.offer.icon} imageUrl={r.offer.image_url} thumbnailUrl={r.offer.thumbnail_url} category={r.offer.category} alt="" size="sm" />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-medium">{r.offer.name}</span>
                          <span className="block truncate text-xs text-muted-foreground">{r.offer.shop.name} · {formatDistance(r.offer.shop.distance_km)}</span>
                        </span>
                        <span className="text-sm font-semibold tabular">{formatPrice(r.offer.price)}</span>
                      </Link>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {d.recent_searches.length > 0 && (
              <section>
                <h2 className="font-semibold">{t("transactions.account.recentSearches")}</h2>
                <div className="mt-3 flex flex-wrap gap-2">
                  {d.recent_searches.map((q) => (
                    <Link key={q} href={`/search?q=${encodeURIComponent(q)}`} className="rounded-full border bg-card px-3 py-1.5 text-sm hover:bg-accent">{q}</Link>
                  ))}
                </div>
              </section>
            )}
          </aside>
        </div>
      )}
      {me && <AccountSettings me={me} />}
      {reviewing && <ReviewDialog item={reviewing} open onOpenChange={(o) => !o && setReviewing(null)} />}
    </div>
  );
}

// ---------------------------------------------------------------- account and privacy settings (P3.2)
// Correction of name and home location, consent toggles, the nominee, signed-in devices, data export and
// account deletion. Rules and what enforces them: docs/legal/retention.md.

type Purpose = "whatsapp" | "personalised_suggestions" | "search_measurement";

interface OpenItem {
  kind: "reservation" | "order";
  id: number;
  code: string;
  status: string;
  shop: string | null;
  summary: string | null;
}

interface DeletionPlan {
  outcome: "immediate" | "cancel_first" | "scheduled" | "refused";
  then: "immediate" | "scheduled" | null;
  message: string;
  open: OpenItem[];
  in_flight: OpenItem[];
  scheduled_for: string | null;
  shops: string[];
}

interface Nominee {
  name: string;
  phone: string | null;
  email: string | null;
  added_at: string | null;
}

interface PrivacySettings {
  consents: Record<string, boolean>;
  optional_purposes: Purpose[];
  nominee: Nominee | null;
  deletion: DeletionPlan;
}

interface Device {
  id: string;
  device: string;
  created_at: string;
  last_seen_at: string;
  current: boolean;
}

// The message keys of each optional purpose's title and description (transactions.account.purposes).
const PURPOSES: Record<Purpose, "whatsapp" | "personalised" | "measurement"> = {
  whatsapp: "whatsapp",
  personalised_suggestions: "personalised",
  search_measurement: "measurement",
};

// Open and in-flight reservation and order states the deletion plan can list (transactions.account.itemStatus).
const ITEM_STATUSES = [
  "REQUESTED", "CONFIRMED", "READY_FOR_PICKUP", "PENDING", "SHOP_CONFIRMED", "PREPARING", "OUT_FOR_DELIVERY",
] as const;

function itemStatusKey(status: string): MessageKey | null {
  return (ITEM_STATUSES as readonly string[]).includes(status)
    ? (`transactions.account.itemStatus.${status as (typeof ITEM_STATUSES)[number]}` as const)
    : null;
}

function AccountSettings({ me }: { me: User }) {
  const { t } = useI18n();
  const privacy = useQuery({ queryKey: ["privacy"], queryFn: () => api.get<PrivacySettings>("/auth/privacy") });
  return (
    <section aria-labelledby="account-settings" className="mt-14 border-t pt-8 pb-12">
      <h2 id="account-settings" className="text-2xl font-semibold">{t("transactions.account.settingsTitle")}</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        {t("transactions.account.settingsBody")} <Link href="/privacy" className="underline">{t("transactions.account.privacyNotice")}</Link>
      </p>
      {privacy.isError ? (
        <ErrorState className="mt-6" error={privacy.error} onRetry={() => privacy.refetch()} />
      ) : !privacy.data ? (
        <ListSkeleton className="mt-6" rows={3} />
      ) : (
        <div className="mt-6 grid gap-6 lg:grid-cols-2">
          <DetailsCard me={me} />
          <ConsentCard settings={privacy.data} />
          <NomineeCard nominee={privacy.data.nominee} />
          <DevicesCard />
          <ExportCard />
          <DeleteCard plan={privacy.data.deletion} />
        </div>
      )}
    </section>
  );
}

function Card({ title, icon: Icon, children }: { title: string; icon: typeof UserRound; children: React.ReactNode }) {
  return (
    <div className="rounded-2xl border bg-card p-5">
      <h3 className="flex items-center gap-2 font-semibold"><Icon className="size-4 text-brand" aria-hidden /> {title}</h3>
      <div className="mt-4">{children}</div>
    </div>
  );
}

function DetailsCard({ me }: { me: User }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const { location } = useLocation();
  const [name, setName] = useState(me.name);
  const [homeLabel, setHomeLabel] = useState(me.home_label ?? "");
  const [home, setHome] = useState<{ lat: number; lng: number } | null>(
    me.home_lat != null && me.home_lng != null ? { lat: me.home_lat, lng: me.home_lng } : null,
  );
  const save = useMutation({
    mutationFn: () => api.patch<User>("/auth/me", {
      name: name.trim(), home_label: homeLabel.trim() || null, home_lat: home?.lat ?? null, home_lng: home?.lng ?? null,
    }),
    onSuccess: (user) => {
      qc.setQueryData(["me"], user);
      toast.success(t("transactions.account.details.saved"));
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Card title={t("transactions.account.details.title")} icon={UserRound}>
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <div className="space-y-1.5">
          <Label htmlFor="acct-name">{t("transactions.account.details.name")}</Label>
          <Input id="acct-name" value={name} onChange={(e) => setName(e.target.value)} minLength={2} maxLength={120} required autoComplete="name" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="acct-home">{t("transactions.account.details.home")}</Label>
          <Input id="acct-home" value={homeLabel} onChange={(e) => setHomeLabel(e.target.value)} maxLength={160}
            placeholder={t("transactions.account.details.homePlaceholder")} />
          <p className="text-xs text-muted-foreground">
            {home
              ? t("transactions.account.details.savedPoint", { lat: home.lat.toFixed(3), lng: home.lng.toFixed(3) })
              : t("transactions.account.details.noPoint")}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" variant="outline" disabled={!location}
              onClick={() => { if (location) { setHome({ lat: location.lat, lng: location.lng }); if (!homeLabel) setHomeLabel(location.label); } }}>
              {t("transactions.account.details.useLocation")}
            </Button>
            {home && <Button type="button" size="sm" variant="ghost" onClick={() => { setHome(null); setHomeLabel(""); }}>{t("transactions.account.details.clearHome")}</Button>}
          </div>
        </div>
        <Button type="submit" size="sm" disabled={save.isPending || name.trim().length < 2}>{t("transactions.account.details.save")}</Button>
      </form>
    </Card>
  );
}

function ConsentCard({ settings }: { settings: PrivacySettings }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const toggle = useMutation({
    mutationFn: ({ purpose, on }: { purpose: Purpose; on: boolean }) =>
      on ? api.put<{ consents: Record<string, boolean> }>(`/auth/consents/${purpose}`)
        : api.delete<{ consents: Record<string, boolean> }>(`/auth/consents/${purpose}`),
    onSuccess: ({ consents }) => qc.setQueryData<PrivacySettings>(["privacy"], (old) => (old ? { ...old, consents } : old)),
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Card title={t("transactions.account.choices.title")} icon={ShieldCheck}>
      <ul className="space-y-4">
        {settings.optional_purposes.map((purpose) => (
          <li key={purpose} className="flex items-start justify-between gap-4">
            <Label htmlFor={`consent-${purpose}`} className="flex flex-col items-start gap-0.5">
              <span>{PURPOSES[purpose] ? t(`transactions.account.purposes.${PURPOSES[purpose]}.title`) : purpose}</span>
              <span className="text-xs font-normal text-muted-foreground">
                {PURPOSES[purpose] ? t(`transactions.account.purposes.${PURPOSES[purpose]}.description`) : null}
              </span>
            </Label>
            <Switch id={`consent-${purpose}`} checked={!!settings.consents[purpose]} disabled={toggle.isPending}
              onCheckedChange={(on) => toggle.mutate({ purpose, on })} />
          </li>
        ))}
      </ul>
      <p className="mt-4 text-xs text-muted-foreground">{t("transactions.account.choices.required")}</p>
    </Card>
  );
}

function NomineeCard({ nominee }: { nominee: Nominee | null }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const [name, setName] = useState(nominee?.name ?? "");
  const [phone, setPhone] = useState(nominee?.phone ?? "");
  const [email, setEmail] = useState(nominee?.email ?? "");
  const refresh = (n: Nominee | null) => qc.setQueryData<PrivacySettings>(["privacy"], (old) => (old ? { ...old, nominee: n } : old));
  const save = useMutation({
    mutationFn: () => api.put<{ nominee: Nominee }>("/auth/nominee", {
      name: name.trim(), phone: phone.trim() || null, email: email.trim() || null,
    }),
    onSuccess: ({ nominee: n }) => { refresh(n); toast.success(t("transactions.account.nominee.saved")); },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: () => api.delete("/auth/nominee"),
    onSuccess: () => { refresh(null); setName(""); setPhone(""); setEmail(""); toast.success(t("transactions.account.nominee.removed")); },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Card title={t("transactions.account.nominee.title")} icon={UserRound}>
      <p className="text-sm text-muted-foreground">{t("transactions.account.nominee.body")}</p>
      <form className="mt-3 space-y-3" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <div className="space-y-1.5">
          <Label htmlFor="nominee-name">{t("transactions.account.nominee.name")}</Label>
          <Input id="nominee-name" value={name} onChange={(e) => setName(e.target.value)} minLength={2} maxLength={120} required />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="nominee-phone">{t("transactions.account.nominee.phone")}</Label>
            <Input id="nominee-phone" value={phone} onChange={(e) => setPhone(e.target.value)} inputMode="numeric" pattern="[6-9][0-9]{9}"
              placeholder={t("transactions.account.nominee.phonePlaceholder")} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="nominee-email">{t("transactions.account.nominee.email")}</Label>
            <Input id="nominee-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
        </div>
        <p className="text-xs text-muted-foreground">{t("transactions.account.nominee.contactHint")}</p>
        <div className="flex flex-wrap gap-2">
          <Button type="submit" size="sm" disabled={save.isPending || name.trim().length < 2 || (!phone.trim() && !email.trim())}>
            {nominee ? t("transactions.account.nominee.replace") : t("transactions.account.nominee.save")}
          </Button>
          {nominee && <Button type="button" size="sm" variant="ghost" disabled={remove.isPending} onClick={() => remove.mutate()}>{t("transactions.account.nominee.remove")}</Button>}
        </div>
      </form>
    </Card>
  );
}

function DevicesCard() {
  const { t, language } = useI18n();
  const now = useNow(60_000);
  const qc = useQueryClient();
  const router = useRouter();
  const devices = useQuery({ queryKey: ["sessions"], queryFn: () => api.get<Device[]>("/auth/sessions") });
  // Signed out of this browser: the stored reservation codes and saved /account pages go too (P7.12), as on sign-out.
  const signedOut = async () => {
    await qc.cancelQueries();
    qc.clear();
    qc.setQueryData(["me"], null);
    await clearOfflineReservations();
    router.push("/");
    router.refresh();
  };
  const revoke = useMutation({
    mutationFn: (d: Device) => api.delete(`/auth/sessions/${encodeURIComponent(d.id)}`).then(() => d),
    onSuccess: (d) => {
      if (d.current) return signedOut();
      qc.setQueryData<Device[]>(["sessions"], (old) => old?.filter((x) => x.id !== d.id));
      toast.success(t("transactions.account.devices.signedOut", { device: d.device }));
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const everywhere = useMutation({ mutationFn: () => api.post("/auth/logout-everywhere"), onSuccess: signedOut,
    onError: (e) => toast.error(errorMessage(e)) });
  return (
    <Card title={t("transactions.account.devices.title")} icon={MonitorSmartphone}>
      {devices.isError ? (
        <ErrorState error={devices.error} onRetry={() => devices.refetch()} />
      ) : !devices.data ? (
        <ListSkeleton rows={2} />
      ) : (
        <ul className="divide-y">
          {devices.data.map((d) => (
            <li key={d.id} className="flex items-center justify-between gap-3 py-2.5">
              <span className="min-w-0">
                <span className="block truncate text-sm font-medium">
                  {d.current ? t("transactions.account.devices.thisDevice", { device: d.device }) : d.device}
                </span>
                <span className="block text-xs text-muted-foreground">
                  {t("transactions.account.devices.lastUsed", { time: timeAgo(d.last_seen_at, now, language) })}
                </span>
              </span>
              <Button size="sm" variant="outline" disabled={revoke.isPending} onClick={() => revoke.mutate(d)}
                aria-label={t("transactions.account.devices.revokeLabel", {
                  device: d.current ? t("transactions.account.devices.thisDevice", { device: d.device }) : d.device,
                })}>
                {t("transactions.account.devices.revoke")}
              </Button>
            </li>
          ))}
        </ul>
      )}
      <Button className="mt-3" size="sm" variant="ghost" disabled={everywhere.isPending} onClick={() => everywhere.mutate()}>
        {t("transactions.account.devices.everywhere")}
      </Button>
    </Card>
  );
}

function ExportCard() {
  const { t } = useI18n();
  const download = useMutation({
    mutationFn: () => api.get<unknown>("/me/export"),
    onSuccess: (data) => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = "nearshop-data.json";
      a.click();
      URL.revokeObjectURL(url);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Card title={t("transactions.account.export.title")} icon={Download}>
      <p className="text-sm text-muted-foreground">{t("transactions.account.export.body")}</p>
      <Button className="mt-3" size="sm" variant="outline" disabled={download.isPending} onClick={() => download.mutate()}>
        <Download className="size-4" aria-hidden /> {download.isPending ? t("transactions.account.export.preparing") : t("transactions.account.export.download")}
      </Button>
    </Card>
  );
}

function ItemList({ items }: { items: OpenItem[] }) {
  const { t } = useI18n();
  return (
    <ul className="mt-2 space-y-1 text-sm">
      {items.map((i) => {
        const status = itemStatusKey(i.status);
        return (
          <li key={`${i.kind}-${i.id}`} className="rounded-lg bg-muted/60 px-3 py-1.5">
            {t(i.kind === "reservation" ? "transactions.account.deletion.pickup" : "transactions.account.deletion.delivery", { code: i.code })}
            {" · "}{i.shop ?? t("transactions.account.deletion.shop")}
            {" · "}{status ? t(status) : i.status.replaceAll("_", " ").toLowerCase()}
            {i.summary && <span className="text-muted-foreground"> · {i.summary}</span>}
          </li>
        );
      })}
    </ul>
  );
}

function DeleteCard({ plan }: { plan: DeletionPlan }) {
  const { t, language } = useI18n();
  const qc = useQueryClient();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const cancelFirst = plan.outcome === "cancel_first";
  const remove = useMutation({
    mutationFn: () => api.post<{ status: "deleted" | "scheduled"; scheduled_for?: string }>("/auth/delete", { password, cancel_open: cancelFirst }),
    onSuccess: async (res) => {
      setOpen(false);
      setPassword("");
      if (res.status === "deleted") {
        // Signed out with the account gone: the stored reservation codes and saved /account pages go too (P7.12).
        await qc.cancelQueries();
        qc.clear();
        qc.setQueryData(["me"], null);
        await clearOfflineReservations();
        toast.success(t("transactions.account.deletion.deleted"));
        router.push("/");
        router.refresh();
      } else {
        toast.success(t("transactions.account.deletion.willDelete", { date: formatDateTime(res.scheduled_for, language) }));
        qc.invalidateQueries({ queryKey: ["privacy"] });
      }
    },
    onError: (e) => {
      toast.error(errorMessage(e));
      qc.invalidateQueries({ queryKey: ["privacy"] });
    },
  });
  const date = formatDateTime(plan.scheduled_for, language);
  const outcome =
    plan.outcome === "refused" ? plan.message
    : plan.outcome === "scheduled" && plan.open.length === 0 && plan.in_flight.length === 0 && plan.scheduled_for
      ? t("transactions.account.deletion.scheduled", { date })
    : plan.outcome === "scheduled" ? t("transactions.account.deletion.scheduledInFlight", { date })
    : cancelFirst
      ? `${t("transactions.account.deletion.cancelFirst")} ${plan.then === "scheduled"
        ? t("transactions.account.deletion.thenScheduled", { date })
        : t("transactions.account.deletion.thenImmediate")}`
    : t("transactions.account.deletion.immediate");
  return (
    <Card title={t("transactions.account.deletion.title")} icon={Trash2}>
      <p className="text-sm text-muted-foreground">
        {t("transactions.account.deletion.body")}
        {plan.shops.length > 0 && ` ${t("transactions.account.deletion.shopHidden", { shops: plan.shops.join(", ") })}`}
      </p>
      <p className="mt-3 text-sm font-medium" role="status">{outcome}</p>
      {plan.open.length > 0 && <ItemList items={plan.open} />}
      {plan.in_flight.length > 0 && <><p className="mt-3 text-sm">{t("transactions.account.deletion.onTheirWay")}</p><ItemList items={plan.in_flight} /></>}
      {plan.outcome !== "refused" && (
        <Button className="mt-4" size="sm" variant="destructive" onClick={() => setOpen(true)}>
          {cancelFirst ? t("transactions.account.deletion.cancelAndDelete") : t("transactions.account.deletion.delete")}
        </Button>
      )}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("transactions.account.deletion.confirmTitle")}</DialogTitle>
            <DialogDescription>{outcome} {t("transactions.account.deletion.cannotUndo")}</DialogDescription>
          </DialogHeader>
          <form id="delete-account" className="space-y-1.5" onSubmit={(e) => { e.preventDefault(); remove.mutate(); }}>
            <Label htmlFor="delete-password">{t("transactions.account.deletion.password")}</Label>
            <Input id="delete-password" type="password" autoComplete="current-password" value={password}
              onChange={(e) => setPassword(e.target.value)} required />
          </form>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>{t("transactions.account.deletion.keep")}</Button>
            <Button type="submit" form="delete-account" variant="destructive" disabled={!password || remove.isPending}>
              {cancelFirst ? t("transactions.account.deletion.cancelAndDelete") : t("transactions.account.deletion.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
