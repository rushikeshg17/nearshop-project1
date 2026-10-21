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
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useMe } from "@/hooks/use-session";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime, formatDistance, formatPrice, timeAgo } from "@/lib/format";
import { useLocation } from "@/lib/location";
import type { CustomerDashboard, Order, Reservation, User } from "@/lib/types";

const href = (i: Reservation | Order) => (i.kind === "reservation" ? `/account/reservations/${i.id}` : `/account/orders/${i.id}`);

export function AccountView() {
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
        <EmptyState icon={History} title="Your activity is on your dashboard" description="Shop owners manage reservations from the shop dashboard. Your account and privacy settings are below."
          action={<Button asChild><Link href={me.role === "owner" ? "/shop" : "/admin"}>Go to dashboard</Link></Button>} />
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
          <p className="text-sm text-muted-foreground">Your activity</p>
          <h1 className="text-3xl font-semibold">Hi {me?.name.split(" ")[0] ?? "there"}</h1>
        </div>
        {d && (
          <dl className="flex gap-6 text-sm">
            {[
              [d.stats.completed_pickups, "pickups"],
              [d.stats.delivered_orders, "deliveries"],
              [d.stats.shops_visited, "shops"],
            ].map(([n, l]) => (
              <div key={l as string}>
                <dt className="font-heading text-2xl font-semibold tabular">{n}</dt>
                <dd className="text-muted-foreground">{l}</dd>
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
              <h2 className="mb-3 text-lg font-semibold">In progress</h2>
              {active.length ? (
                <div className="space-y-3">{active.map((i) => <ActivityCard key={`${i.kind}-${i.id}`} item={i} href={href(i)} />)}</div>
              ) : (
                <EmptyState icon={Search} title="Nothing on hold right now" description="Find something nearby and reserve it. The shop sets it aside for you."
                  action={<Button asChild><Link href="/search">Search nearby</Link></Button>} />
              )}
            </section>

            {d.to_review.length > 0 && (
              <section className="rounded-2xl border bg-brand-soft/40 p-4">
                <h2 className="flex items-center gap-2 font-semibold"><Star className="size-4 text-brand" /> Rate your recent visits</h2>
                <ul className="mt-3 space-y-2">
                  {d.to_review.map((i) => (
                    <li key={`${i.kind}-${i.id}`} className="flex items-center justify-between gap-3 rounded-xl bg-card p-3">
                      <span className="min-w-0 truncate text-sm">
                        <span className="font-medium">{i.shop.name}</span> · {i.kind === "reservation" ? i.product.name : `${i.items.length} items`}
                      </span>
                      <Button size="sm" onClick={() => setReviewing(i)}>Rate</Button>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section>
              <div className="mb-3 flex items-center justify-between gap-3">
                <h2 className="text-lg font-semibold">History</h2>
                <Tabs value={tab} onValueChange={setTab}>
                  <TabsList>
                    <TabsTrigger value="all">All</TabsTrigger>
                    <TabsTrigger value="reservation">Pickups</TabsTrigger>
                    <TabsTrigger value="order">Deliveries</TabsTrigger>
                  </TabsList>
                </Tabs>
              </div>
              {history.length ? (
                <div className="space-y-3">{history.map((i) => <ActivityCard key={`${i.kind}-${i.id}`} item={i} href={href(i)} />)}</div>
              ) : (
                <p className="rounded-2xl border border-dashed p-6 text-center text-sm text-muted-foreground">No past {tab === "order" ? "deliveries" : tab === "reservation" ? "pickups" : "activity"} yet.</p>
              )}
            </section>
          </div>

          <aside className="space-y-8">
            {d.recommendations.length > 0 && (
              <section>
                <h2 className="flex items-center gap-2 font-semibold"><Sparkles className="size-4 text-brand" /> You might need next</h2>
                <p className="mt-1 text-xs text-muted-foreground">Based on what people buy along with your recent purchases.</p>
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
                <h2 className="font-semibold">Recent searches</h2>
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

const PURPOSES: Record<Purpose, { title: string; description: string }> = {
  whatsapp: { title: "WhatsApp messages", description: "Updates about your reservations and orders on WhatsApp." },
  personalised_suggestions: {
    title: "Personalised suggestions",
    description: "Use your search history for suggestions. Off means new searches are not linked to you, and past ones are unlinked.",
  },
  search_measurement: { title: "Help improve search", description: "Count which results you open, without linking them to your account." },
};

function AccountSettings({ me }: { me: User }) {
  const privacy = useQuery({ queryKey: ["privacy"], queryFn: () => api.get<PrivacySettings>("/auth/privacy") });
  return (
    <section aria-labelledby="account-settings" className="mt-14 border-t pt-8 pb-12">
      <h2 id="account-settings" className="text-2xl font-semibold">Account and privacy</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Correct your details, choose what we may use, and download or delete your data. <Link href="/privacy" className="underline">Privacy notice</Link>
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
      toast.success("Your details are saved");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Card title="Your details" icon={UserRound}>
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <div className="space-y-1.5">
          <Label htmlFor="acct-name">Name</Label>
          <Input id="acct-name" value={name} onChange={(e) => setName(e.target.value)} minLength={2} maxLength={120} required autoComplete="name" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="acct-home">Home location name</Label>
          <Input id="acct-home" value={homeLabel} onChange={(e) => setHomeLabel(e.target.value)} maxLength={160} placeholder="For example, Agrahara" />
          <p className="text-xs text-muted-foreground">
            {home ? `Saved point: ${home.lat.toFixed(3)}, ${home.lng.toFixed(3)}` : "No home point saved."}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" variant="outline" disabled={!location}
              onClick={() => { if (location) { setHome({ lat: location.lat, lng: location.lng }); if (!homeLabel) setHomeLabel(location.label); } }}>
              Use my current location
            </Button>
            {home && <Button type="button" size="sm" variant="ghost" onClick={() => { setHome(null); setHomeLabel(""); }}>Clear home</Button>}
          </div>
        </div>
        <Button type="submit" size="sm" disabled={save.isPending || name.trim().length < 2}>Save details</Button>
      </form>
    </Card>
  );
}

function ConsentCard({ settings }: { settings: PrivacySettings }) {
  const qc = useQueryClient();
  const toggle = useMutation({
    mutationFn: ({ purpose, on }: { purpose: Purpose; on: boolean }) =>
      on ? api.put<{ consents: Record<string, boolean> }>(`/auth/consents/${purpose}`)
        : api.delete<{ consents: Record<string, boolean> }>(`/auth/consents/${purpose}`),
    onSuccess: ({ consents }) => qc.setQueryData<PrivacySettings>(["privacy"], (old) => (old ? { ...old, consents } : old)),
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Card title="Your choices" icon={ShieldCheck}>
      <ul className="space-y-4">
        {settings.optional_purposes.map((purpose) => (
          <li key={purpose} className="flex items-start justify-between gap-4">
            <Label htmlFor={`consent-${purpose}`} className="flex flex-col items-start gap-0.5">
              <span>{PURPOSES[purpose]?.title ?? purpose}</span>
              <span className="text-xs font-normal text-muted-foreground">{PURPOSES[purpose]?.description}</span>
            </Label>
            <Switch id={`consent-${purpose}`} checked={!!settings.consents[purpose]} disabled={toggle.isPending}
              onCheckedChange={(on) => toggle.mutate({ purpose, on })} />
          </li>
        ))}
      </ul>
      <p className="mt-4 text-xs text-muted-foreground">Account and order messages are needed to run your account. To stop them, delete the account.</p>
    </Card>
  );
}

function NomineeCard({ nominee }: { nominee: Nominee | null }) {
  const qc = useQueryClient();
  const [name, setName] = useState(nominee?.name ?? "");
  const [phone, setPhone] = useState(nominee?.phone ?? "");
  const [email, setEmail] = useState(nominee?.email ?? "");
  const refresh = (n: Nominee | null) => qc.setQueryData<PrivacySettings>(["privacy"], (old) => (old ? { ...old, nominee: n } : old));
  const save = useMutation({
    mutationFn: () => api.put<{ nominee: Nominee }>("/auth/nominee", {
      name: name.trim(), phone: phone.trim() || null, email: email.trim() || null,
    }),
    onSuccess: ({ nominee: n }) => { refresh(n); toast.success("Nominee saved"); },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: () => api.delete("/auth/nominee"),
    onSuccess: () => { refresh(null); setName(""); setPhone(""); setEmail(""); toast.success("Nominee removed"); },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Card title="Nominee" icon={UserRound}>
      <p className="text-sm text-muted-foreground">
        One person who may use your data rights if you die or cannot act yourself. Support acts on it only with documentary proof.
      </p>
      <form className="mt-3 space-y-3" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <div className="space-y-1.5">
          <Label htmlFor="nominee-name">Nominee&apos;s name</Label>
          <Input id="nominee-name" value={name} onChange={(e) => setName(e.target.value)} minLength={2} maxLength={120} required />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="nominee-phone">Phone</Label>
            <Input id="nominee-phone" value={phone} onChange={(e) => setPhone(e.target.value)} inputMode="numeric" pattern="[6-9][0-9]{9}" placeholder="10-digit mobile" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="nominee-email">Email</Label>
            <Input id="nominee-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
        </div>
        <p className="text-xs text-muted-foreground">Give a phone number, an email address or both.</p>
        <div className="flex flex-wrap gap-2">
          <Button type="submit" size="sm" disabled={save.isPending || name.trim().length < 2 || (!phone.trim() && !email.trim())}>
            {nominee ? "Replace nominee" : "Save nominee"}
          </Button>
          {nominee && <Button type="button" size="sm" variant="ghost" disabled={remove.isPending} onClick={() => remove.mutate()}>Remove</Button>}
        </div>
      </form>
    </Card>
  );
}

function DevicesCard() {
  const qc = useQueryClient();
  const router = useRouter();
  const devices = useQuery({ queryKey: ["sessions"], queryFn: () => api.get<Device[]>("/auth/sessions") });
  const signedOut = () => {
    qc.clear();
    qc.setQueryData(["me"], null);
    router.push("/");
    router.refresh();
  };
  const revoke = useMutation({
    mutationFn: (d: Device) => api.delete(`/auth/sessions/${encodeURIComponent(d.id)}`).then(() => d),
    onSuccess: (d) => {
      if (d.current) return signedOut();
      qc.setQueryData<Device[]>(["sessions"], (old) => old?.filter((x) => x.id !== d.id));
      toast.success(`${d.device} is signed out`);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const everywhere = useMutation({ mutationFn: () => api.post("/auth/logout-everywhere"), onSuccess: signedOut,
    onError: (e) => toast.error(errorMessage(e)) });
  return (
    <Card title="Signed-in devices" icon={MonitorSmartphone}>
      {devices.isError ? (
        <ErrorState error={devices.error} onRetry={() => devices.refetch()} />
      ) : !devices.data ? (
        <ListSkeleton rows={2} />
      ) : (
        <ul className="divide-y">
          {devices.data.map((d) => (
            <li key={d.id} className="flex items-center justify-between gap-3 py-2.5">
              <span className="min-w-0">
                <span className="block truncate text-sm font-medium">{d.device}{d.current && " (this device)"}</span>
                <span className="block text-xs text-muted-foreground">Last used {timeAgo(d.last_seen_at)}</span>
              </span>
              <Button size="sm" variant="outline" disabled={revoke.isPending} onClick={() => revoke.mutate(d)}
                aria-label={`Revoke ${d.device}${d.current ? " (this device)" : ""}`}>Revoke</Button>
            </li>
          ))}
        </ul>
      )}
      <Button className="mt-3" size="sm" variant="ghost" disabled={everywhere.isPending} onClick={() => everywhere.mutate()}>
        Sign out everywhere
      </Button>
    </Card>
  );
}

function ExportCard() {
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
    <Card title="Your data" icon={Download}>
      <p className="text-sm text-muted-foreground">
        Download your profile, choices, nominee, reservations, orders, reviews, notifications and search history as a JSON file. Up to 3 times a day.
      </p>
      <Button className="mt-3" size="sm" variant="outline" disabled={download.isPending} onClick={() => download.mutate()}>
        <Download className="size-4" aria-hidden /> {download.isPending ? "Preparing..." : "Download my data"}
      </Button>
    </Card>
  );
}

function ItemList({ items }: { items: OpenItem[] }) {
  return (
    <ul className="mt-2 space-y-1 text-sm">
      {items.map((i) => (
        <li key={`${i.kind}-${i.id}`} className="rounded-lg bg-muted/60 px-3 py-1.5">
          {i.kind === "reservation" ? "Pickup" : "Delivery"} {i.code} · {i.shop ?? "Shop"} · {i.status.replaceAll("_", " ").toLowerCase()}
          {i.summary && <span className="text-muted-foreground"> · {i.summary}</span>}
        </li>
      ))}
    </ul>
  );
}

function DeleteCard({ plan }: { plan: DeletionPlan }) {
  const qc = useQueryClient();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const cancelFirst = plan.outcome === "cancel_first";
  const remove = useMutation({
    mutationFn: () => api.post<{ status: "deleted" | "scheduled"; scheduled_for?: string }>("/auth/delete", { password, cancel_open: cancelFirst }),
    onSuccess: (res) => {
      setOpen(false);
      setPassword("");
      if (res.status === "deleted") {
        qc.clear();
        qc.setQueryData(["me"], null);
        toast.success("Your account has been deleted");
        router.push("/");
        router.refresh();
      } else {
        toast.success(`Your account will be deleted by ${formatDateTime(res.scheduled_for)}`);
        qc.invalidateQueries({ queryKey: ["privacy"] });
      }
    },
    onError: (e) => {
      toast.error(errorMessage(e));
      qc.invalidateQueries({ queryKey: ["privacy"] });
    },
  });
  const outcome =
    plan.outcome === "refused" ? plan.message
    : plan.outcome === "scheduled" && plan.open.length === 0 && plan.in_flight.length === 0 && plan.scheduled_for
      ? `Deletion is scheduled for ${formatDateTime(plan.scheduled_for)}.`
    : plan.outcome === "scheduled" ? `Some orders are on their way. Your account will be deleted when they close, at the latest by ${formatDateTime(plan.scheduled_for)}.`
    : cancelFirst ? `These will be cancelled first, and the items released back to the shop. ${plan.then === "scheduled"
        ? `Orders already on their way then keep your account until they close, at the latest ${formatDateTime(plan.scheduled_for)}.`
        : "Then your account is deleted at once."}`
    : "Your account is deleted at once.";
  return (
    <Card title="Delete account" icon={Trash2}>
      <p className="text-sm text-muted-foreground">
        Your name, email, phone, password, home, nominee and choices are removed, and your notifications and searches deleted.
        Shops keep the reservations and orders they handled, without your phone or address. Reviews keep their rating and show as &ldquo;Former customer&rdquo;.
        {plan.shops.length > 0 && ` Your shop (${plan.shops.join(", ")}) is hidden and its listings switched off.`}
      </p>
      <p className="mt-3 text-sm font-medium" role="status">{outcome}</p>
      {plan.open.length > 0 && <ItemList items={plan.open} />}
      {plan.in_flight.length > 0 && <><p className="mt-3 text-sm">On their way:</p><ItemList items={plan.in_flight} /></>}
      {plan.outcome !== "refused" && (
        <Button className="mt-4" size="sm" variant="destructive" onClick={() => setOpen(true)}>
          {cancelFirst ? "Cancel all and delete" : "Delete my account"}
        </Button>
      )}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete your NearShop account?</DialogTitle>
            <DialogDescription>{outcome} This cannot be undone.</DialogDescription>
          </DialogHeader>
          <form id="delete-account" className="space-y-1.5" onSubmit={(e) => { e.preventDefault(); remove.mutate(); }}>
            <Label htmlFor="delete-password">Your password</Label>
            <Input id="delete-password" type="password" autoComplete="current-password" value={password}
              onChange={(e) => setPassword(e.target.value)} required />
          </form>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>Keep my account</Button>
            <Button type="submit" form="delete-account" variant="destructive" disabled={!password || remove.isPending}>
              {cancelFirst ? "Cancel all and delete" : "Delete my account"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
