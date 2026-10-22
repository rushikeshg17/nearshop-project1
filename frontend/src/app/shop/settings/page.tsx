"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BadgeCheck, Camera, IndianRupee, Loader2, Save, ShieldCheck } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";

import { ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { ShopAvatar } from "@/components/shop/shop-card";
import { formatDay, verificationNotice } from "@/app/shop/owner-shell";
import {
  KYC_TYPES,
  KycFields,
  type KycFormState,
  kycFormReady,
  normalizeKycNumber,
  parseHours,
  reviewFieldsChanged,
  ShopBasicsFields,
  type ShopFormState,
  ShopLocationFields,
  ShopOperationsFields,
  shopPayload,
} from "@/components/shop/shop-form-fields";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useMeta } from "@/hooks/use-session";
import { api, ApiError, errorMessage } from "@/lib/api";
import type { OwnerShopSettings, ShopDetail, ShopVerification } from "@/lib/types";
import { normalizePayeeName, normalizeUpiId, payeeNameError, upiIdError } from "@/lib/upi";

function toForm(s: ShopDetail): ShopFormState {
  return {
    name: s.name,
    tagline: s.tagline ?? "",
    description: s.description ?? "",
    category_slugs: s.categories,
    phone: s.phone ?? "",
    address_line: s.address_line,
    locality: s.locality ?? "",
    pincode: s.pincode ?? "",
    lat: s.lat,
    lng: s.lng,
    ...parseHours(s.opening_hours),
    closed_on: s.closed_on ?? "",
    offers_pickup: s.offers_pickup,
    offers_delivery: s.offers_delivery,
    delivery_radius_km: s.delivery_radius_km || 4,
    delivery_fee: s.delivery_fee,
    free_delivery_above: s.free_delivery_above ? String(s.free_delivery_above) : "",
    hold_minutes: s.hold_minutes,
  };
}

export default function ShopSettingsPage() {
  const shop = useQuery({ queryKey: ["owner", "shop"], queryFn: () => api.get<OwnerShopSettings>("/owner/shop") });
  // The same query as the owner shell's banner, so an admin's decision reaches this page when the banner sees it.
  const live = useQuery({
    queryKey: ["owner", "verification"],
    queryFn: () => api.get<ShopVerification>("/owner/verification"),
    refetchInterval: 30_000,
  });
  if (shop.isError) return <ErrorState error={shop.error} onRetry={() => shop.refetch()} />;
  if (!shop.data) return <ListSkeleton rows={4} />;
  const verification = live.data ?? shop.data.verification;
  return (
    <>
      <SettingsForm key={shop.data.id} initial={shop.data} verification={verification} />
      <VerifySection v={verification} />
    </>
  );
}

/** initial is the latest saved shop (the query data, replaced after every save); only the form state is local. */
function SettingsForm({ initial, verification }: { initial: OwnerShopSettings; verification: ShopVerification }) {
  const qc = useQueryClient();
  const { data: meta } = useMeta();
  const [form, setForm] = useState<ShopFormState>(() => toForm(initial));
  const [active, setActive] = useState(initial.is_active);
  const [confirming, setConfirming] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  // Direct UPI (P6.1, P6.2): the shop's own UPI ID, where customers pay it directly. Empty removes it.
  const [upi, setUpi] = useState<UpiFormState>(() => ({ upi_id: initial.upi_id ?? "", upi_payee_name: initial.upi_payee_name ?? "" }));
  const upiErrors = upiErrorsOf(upi);

  const save = useMutation({
    mutationFn: () =>
      api.patch<OwnerShopSettings>("/owner/shop", {
        ...shopPayload(form),
        offers_pickup: form.offers_pickup,
        is_active: active,
        upi_id: normalizeUpiId(upi.upi_id) || null,
        upi_payee_name: normalizePayeeName(upi.upi_payee_name) || null,
      }),
    onSuccess: (s) => {
      setConfirming(false);
      setUpi({ upi_id: s.upi_id ?? "", upi_payee_name: s.upi_payee_name ?? "" });
      qc.setQueryData(["owner", "shop"], s);
      qc.setQueryData(["owner", "verification"], s.verification);
      qc.invalidateQueries({ queryKey: ["me"] });
      if (s.review_started) toast.warning("Saved. Your shop is hidden until an admin approves the new details.");
      else toast.success("Shop settings saved");
    },
    onError: (e) => {
      setConfirming(false);
      // The API's own format check (the same rules) shows next to the field it names.
      if (e instanceof ApiError && (e.field === "upi_id" || e.field === "upi_payee_name")) {
        setUpi((u) => ({ ...u, server: { field: e.field!, message: e.message.replace(/^[a-z_]+: /, "") } }));
      }
      toast.error(errorMessage(e));
    },
  });
  // A verified shop that changes its address or phone goes back to review and is hidden meanwhile (P3.3).
  const needsReview = verification.changes_need_review && reviewFieldsChanged(initial, form);
  const upload = useMutation({
    mutationFn: (f: File) => api.upload<{ image_url: string }>("/owner/shop/image", f),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["owner", "shop"] });
      toast.success("Shop photo updated");
    },
  });

  const set = <K extends keyof ShopFormState>(k: K, v: ShopFormState[K]) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (upiErrors.upi_id || upiErrors.upi_payee_name) {
          setUpi((u) => ({ ...u, shown: true }));
          document.getElementById(upiErrors.upi_id ? "upi-id" : "upi-payee")?.focus();
          return;
        }
        if (needsReview) setConfirming(true);
        else save.mutate();
      }}
    >
      <PageTitle title="Shop settings" description="What customers see on your shop page and in search." />
      <div className="grid gap-6 lg:grid-cols-2">
        <section className="space-y-5 rounded-2xl border bg-card p-5">
          <div className="flex items-center gap-4">
            <ShopAvatar shop={initial} categories={meta?.categories} className="size-16" />
            <div>
              <Button type="button" variant="outline" size="sm" onClick={() => fileRef.current?.click()} disabled={upload.isPending}>
                {upload.isPending ? <Loader2 className="animate-spin" /> : <Camera />} Shop photo
              </Button>
              <p className="mt-1 text-xs text-muted-foreground">A clear photo of your shopfront helps customers find you.</p>
              <input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" className="hidden" onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])} />
            </div>
          </div>
          <ShopBasicsFields s={form} set={set} meta={meta} />
          <div className="space-y-1.5">
            <Label htmlFor="desc">About your shop</Label>
            <Textarea id="desc" rows={3} value={form.description} onChange={(e) => set("description", e.target.value.slice(0, 1000))} />
          </div>
        </section>
        <section className="space-y-5 rounded-2xl border bg-card p-5">
          <ShopLocationFields s={form} set={set} meta={meta} />
        </section>
        <section className="space-y-5 rounded-2xl border bg-card p-5">
          <ShopOperationsFields s={form} set={set} />
        </section>
        <section className="space-y-5 rounded-2xl border bg-card p-5">
          <div className="flex items-center justify-between gap-4">
            <Label htmlFor="pickup" className="flex flex-col items-start gap-0.5">
              <span>Accept pickup reservations</span>
              <span className="text-xs font-normal text-muted-foreground">Customers reserve and pay at your counter.</span>
            </Label>
            <Switch id="pickup" checked={form.offers_pickup} onCheckedChange={(v) => set("offers_pickup", v)} />
          </div>
          <div className="flex items-center justify-between gap-4">
            <Label htmlFor="active" className="flex flex-col items-start gap-0.5">
              <span>Shop visible on NearShop</span>
              <span className="text-xs font-normal text-muted-foreground">Turn off to pause while you are away. Nothing is deleted.</span>
            </Label>
            <Switch id="active" checked={active} onCheckedChange={setActive} />
          </div>
        </section>
        <UpiFields s={upi} set={setUpi} errors={upiErrors} />
      </div>
      <div className="fixed inset-x-0 bottom-0 z-40 border-t bg-background/95 p-3 backdrop-blur lg:left-[248px]">
        <div className="mx-auto flex max-w-6xl justify-end px-1 md:px-3">
          <Button type="submit" disabled={save.isPending}>{save.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save changes</Button>
        </div>
      </div>
      <Dialog open={confirming} onOpenChange={(o) => !save.isPending && setConfirming(o)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Hide your shop until it is checked again?</DialogTitle>
            <DialogDescription>
              You changed your shop&apos;s address or phone. Customers will not see your shop until an admin approves the new details,
              usually within 2 working days.{form.phone !== (initial.phone ?? "") && " A new phone number is checked again with a call-back."}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline" disabled={save.isPending}>Keep editing</Button>
            </DialogClose>
            <Button type="button" onClick={() => save.mutate()} disabled={save.isPending}>
              {save.isPending && <Loader2 className="animate-spin" />} Save and send for review
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </form>
  );
}

type UpiFormState = {
  upi_id: string;
  upi_payee_name: string;
  /** Show format errors: set once the owner leaves a field or tries to save. */
  shown?: boolean;
  /** The API's refusal of the last save, until the field changes. */
  server?: { field: string; message: string };
};

function upiErrorsOf(s: UpiFormState) {
  const server = (field: string) => (s.server?.field === field ? s.server.message : null);
  return {
    upi_id: upiIdError(s.upi_id) ?? server("upi_id"),
    upi_payee_name: payeeNameError(s.upi_payee_name) ?? server("upi_payee_name"),
  };
}

/** "Get paid by UPI" (P6.2): the shop's own UPI ID and payee name, with the format error under each field. */
function UpiFields({
  s,
  set,
  errors,
}: {
  s: UpiFormState;
  set: (f: (s: UpiFormState) => UpiFormState) => void;
  errors: { upi_id: string | null; upi_payee_name: string | null };
}) {
  const change = (k: "upi_id" | "upi_payee_name", v: string) => set((f) => ({ ...f, [k]: v, server: f.server?.field === k ? undefined : f.server }));
  const show = (k: "upi_id" | "upi_payee_name") => (s.shown || s.server?.field === k ? errors[k] : null);
  const idError = show("upi_id");
  const nameError = show("upi_payee_name");
  return (
    <section aria-labelledby="upi-title" className="space-y-5 rounded-2xl border bg-card p-5">
      <div className="flex items-start gap-3">
        <IndianRupee className="mt-0.5 size-5 shrink-0 text-primary" />
        <div>
          <h2 id="upi-title" className="font-semibold">Get paid by UPI</h2>
          <p className="text-sm text-muted-foreground">
            Customers can pay your own UPI ID straight from their order or reservation, with a link and a QR code. The money
            goes to you directly: NearShop never holds it. Leave it empty to take cash only.
          </p>
        </div>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="upi-id">Your UPI ID</Label>
        <Input
          id="upi-id"
          value={s.upi_id}
          onChange={(e) => change("upi_id", e.target.value.replace(/\s/g, "").slice(0, 50))}
          onBlur={() => s.upi_id && set((f) => ({ ...f, shown: true }))}
          inputMode="email"
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          placeholder="yourshop@okhdfcbank"
          aria-invalid={idError ? true : undefined}
          aria-describedby={idError ? "upi-id-error" : "upi-id-hint"}
        />
        {idError ? (
          <p id="upi-id-error" role="alert" className="text-sm font-medium text-destructive">{idError}</p>
        ) : (
          <p id="upi-id-hint" className="text-xs text-muted-foreground">As your UPI app shows it, for example 98xxxxxx10@ybl.</p>
        )}
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="upi-payee">Payee name (optional)</Label>
        <Input
          id="upi-payee"
          value={s.upi_payee_name}
          onChange={(e) => change("upi_payee_name", e.target.value.slice(0, 60))}
          onBlur={() => s.upi_payee_name && set((f) => ({ ...f, shown: true }))}
          autoComplete="off"
          placeholder="The name on your UPI account"
          aria-invalid={nameError ? true : undefined}
          aria-describedby={nameError ? "upi-payee-error" : "upi-payee-hint"}
        />
        {nameError ? (
          <p id="upi-payee-error" role="alert" className="text-sm font-medium text-destructive">{nameError}</p>
        ) : (
          <p id="upi-payee-hint" className="text-xs text-muted-foreground">Customers see it before paying. Empty uses your shop&apos;s name.</p>
        )}
      </div>
    </section>
  );
}

const EMPTY_KYC: KycFormState = { kyc_type: "gstin", kyc_number: "", document_photo: null, storefront_photo: null };

/** "Verify your shop" (P3.4): evidence type and number, a document photo and a storefront photo. */
function VerifySection({ v }: { v: ShopVerification }) {
  const qc = useQueryClient();
  const [kyc, setKyc] = useState<KycFormState>(EMPTY_KYC);
  const [formKey, setFormKey] = useState(0); // remounts the file inputs after a submission
  const [touched, setTouched] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const set = <K extends keyof KycFormState>(k: K, val: KycFormState[K]) => setKyc((f) => ({ ...f, [k]: val }));

  const submit = useMutation({
    mutationFn: () => {
      const body = new FormData();
      body.append("kyc_type", kyc.kyc_type);
      body.append("kyc_number", normalizeKycNumber(kyc.kyc_number));
      if (kyc.storefront_photo) body.append("storefront_photo", kyc.storefront_photo);
      if (kyc.document_photo) body.append("document_photo", kyc.document_photo);
      return api.post<ShopVerification>("/owner/verification", body);
    },
    onSuccess: (next) => {
      qc.setQueryData(["owner", "verification"], next);
      qc.setQueryData<OwnerShopSettings>(["owner", "shop"], (s) => (s ? { ...s, verification: next } : s));
      setKyc(EMPTY_KYC);
      setTouched(false);
      setAttempted(false);
      setFormKey((k) => k + 1);
      toast.success("Sent for verification. An admin usually checks it within 2 working days.");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const renewing = v.status === "verified";
  const typeLabel = KYC_TYPES.find((t) => t.value === v.kyc_type)?.label;
  return (
    <section id="verify" aria-labelledby="verify-title" className="mt-6 mb-24 scroll-mt-20 space-y-5 rounded-2xl border bg-card p-5 lg:max-w-[calc(50%-0.75rem)]">
      <div className="flex items-start gap-3">
        <ShieldCheck className="mt-0.5 size-5 shrink-0 text-primary" />
        <div>
          <h2 id="verify-title" className="text-lg font-semibold">{renewing ? "Renew your verification" : "Verify your shop"}</h2>
          <p className="text-sm text-muted-foreground">
            Customers only see shops whose registration an admin has checked. Verification lasts 12 months.
          </p>
        </div>
      </div>
      <VerificationSummary v={v} typeLabel={typeLabel} />
      {v.can_submit ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setAttempted(true);
            if (kycFormReady(kyc)) submit.mutate();
          }}
          className="space-y-5"
        >
          <KycFields key={formKey} s={kyc} set={set} numberTouched={touched} attempted={attempted} onBlurNumber={() => setTouched(true)} />
          <Button type="submit" disabled={submit.isPending}>
            {submit.isPending ? <Loader2 className="animate-spin" /> : <BadgeCheck />} {renewing ? "Submit renewal" : "Submit for verification"}
          </Button>
        </form>
      ) : null}
    </section>
  );
}

function VerificationSummary({ v, typeLabel }: { v: ShopVerification; typeLabel?: string }) {
  const evidence = v.kyc_number ? `${typeLabel ?? "Number"}: ${v.kyc_number}` : null;
  let line: string;
  if (v.status === "verified" && v.renewal_pending) line = "Your renewal is waiting for an admin. Your shop stays visible meanwhile.";
  else if (v.status === "verified" && verificationNotice(v)?.kind === "expired") {
    line = `Your verification ended on ${formatDay(v.verified_until)}. Renew it to be visible again.`;
  } else if (v.status === "verified") {
    line = `Verified until ${formatDay(v.verified_until)}.`;
    if (!v.can_submit && v.renewal_opens_at) line += ` You can renew from ${formatDay(v.renewal_opens_at)}.`;
    if (!v.visible_to_customers) line += " Your shop is switched off in settings, so customers cannot see it.";
  }
  else if (v.status === "submitted") line = "Waiting for an admin, usually within 2 working days. Your shop is hidden until it is approved.";
  else if (v.status === "rejected") line = "Not approved. Correct the evidence and submit again.";
  else if (v.status === "suspended") line = "Suspended. Submit fresh evidence to be verified again.";
  else line = "Not submitted yet. Your shop is hidden from customers until it is verified.";
  return (
    <div className="space-y-1 rounded-xl bg-muted/50 p-3 text-sm" data-testid="verification-summary">
      <p>{line}</p>
      {v.rejection_reason && <p><span className="font-medium">Reason:</span> {v.rejection_reason}</p>}
      {evidence && <p className="text-muted-foreground">{evidence}</p>}
      {!v.phone_verified && v.status !== "draft" && (
        <p className="text-muted-foreground">We will call your shop phone to confirm it before approving.</p>
      )}
    </div>
  );
}
