"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BadgeCheck, Camera, Loader2, Save, ShieldCheck } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";

import { ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { useI18n } from "@/components/providers";
import { ShopAvatar } from "@/components/shop/shop-card";
import { formatDay, verificationNotice } from "@/app/shop/owner-shell";
import {
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
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useMeta } from "@/hooks/use-session";
import { api, errorMessage } from "@/lib/api";
import type { LanguageCode, Translate } from "@/lib/i18n";
import type { OwnerShopSettings, ShopDetail, ShopVerification } from "@/lib/types";

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
  const { t } = useI18n();
  const qc = useQueryClient();
  const { data: meta } = useMeta();
  const [form, setForm] = useState<ShopFormState>(() => toForm(initial));
  const [active, setActive] = useState(initial.is_active);
  const [confirming, setConfirming] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const save = useMutation({
    mutationFn: () => api.patch<OwnerShopSettings>("/owner/shop", { ...shopPayload(form), offers_pickup: form.offers_pickup, is_active: active }),
    onSuccess: (s) => {
      setConfirming(false);
      qc.setQueryData(["owner", "shop"], s);
      qc.setQueryData(["owner", "verification"], s.verification);
      qc.invalidateQueries({ queryKey: ["me"] });
      if (s.review_started) toast.warning(t("owner.settings.toast.savedReview"));
      else toast.success(t("owner.settings.toast.saved"));
    },
    onError: (e) => {
      setConfirming(false);
      toast.error(errorMessage(e));
    },
  });
  // A verified shop that changes its address or phone goes back to review and is hidden meanwhile (P3.3).
  const needsReview = verification.changes_need_review && reviewFieldsChanged(initial, form);
  const upload = useMutation({
    mutationFn: (f: File) => api.upload<{ image_url: string }>("/owner/shop/image", f),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["owner", "shop"] });
      toast.success(t("owner.settings.toast.photoUpdated"));
    },
  });

  const set = <K extends keyof ShopFormState>(k: K, v: ShopFormState[K]) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (needsReview) setConfirming(true);
        else save.mutate();
      }}
    >
      <PageTitle title={t("owner.settings.title")} description={t("owner.settings.description")} />
      <div className="grid gap-6 lg:grid-cols-2">
        <section className="space-y-5 rounded-2xl border bg-card p-5">
          <div className="flex items-center gap-4">
            <ShopAvatar shop={initial} categories={meta?.categories} className="size-16" />
            <div>
              <Button type="button" variant="outline" size="sm" onClick={() => fileRef.current?.click()} disabled={upload.isPending}>
                {upload.isPending ? <Loader2 className="animate-spin" /> : <Camera />} {t("owner.settings.shopPhoto")}
              </Button>
              <p className="mt-1 text-xs text-muted-foreground">{t("owner.settings.photoHint")}</p>
              <input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" className="hidden" onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])} />
            </div>
          </div>
          <ShopBasicsFields s={form} set={set} meta={meta} />
          <div className="space-y-1.5">
            <Label htmlFor="desc">{t("owner.settings.about")}</Label>
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
              <span>{t("owner.settings.pickupLabel")}</span>
              <span className="text-xs font-normal text-muted-foreground">{t("owner.settings.pickupHint")}</span>
            </Label>
            <Switch id="pickup" checked={form.offers_pickup} onCheckedChange={(v) => set("offers_pickup", v)} />
          </div>
          <div className="flex items-center justify-between gap-4">
            <Label htmlFor="active" className="flex flex-col items-start gap-0.5">
              <span>{t("owner.settings.activeLabel")}</span>
              <span className="text-xs font-normal text-muted-foreground">{t("owner.settings.activeHint")}</span>
            </Label>
            <Switch id="active" checked={active} onCheckedChange={setActive} />
          </div>
        </section>
      </div>
      <div className="fixed inset-x-0 bottom-0 z-40 border-t bg-background/95 p-3 backdrop-blur lg:left-[248px]">
        <div className="mx-auto flex max-w-6xl justify-end px-1 md:px-3">
          <Button type="submit" disabled={save.isPending}>{save.isPending ? <Loader2 className="animate-spin" /> : <Save />} {t("owner.settings.save")}</Button>
        </div>
      </div>
      <Dialog open={confirming} onOpenChange={(o) => !save.isPending && setConfirming(o)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("owner.settings.confirm.title")}</DialogTitle>
            <DialogDescription>
              {t("owner.settings.confirm.body")}
              {form.phone !== (initial.phone ?? "") && ` ${t("owner.settings.confirm.phoneRecheck")}`}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline" disabled={save.isPending}>{t("owner.settings.confirm.keepEditing")}</Button>
            </DialogClose>
            <Button type="button" onClick={() => save.mutate()} disabled={save.isPending}>
              {save.isPending && <Loader2 className="animate-spin" />} {t("owner.settings.confirm.saveAndSend")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </form>
  );
}

const EMPTY_KYC: KycFormState = { kyc_type: "gstin", kyc_number: "", document_photo: null, storefront_photo: null };

/** "Verify your shop" (P3.4): evidence type and number, a document photo and a storefront photo. */
function VerifySection({ v }: { v: ShopVerification }) {
  const { t, language } = useI18n();
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
      toast.success(t("owner.settings.toast.sentForVerification"));
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const renewing = v.status === "verified";
  return (
    <section id="verify" aria-labelledby="verify-title" className="mt-6 mb-24 scroll-mt-20 space-y-5 rounded-2xl border bg-card p-5 lg:max-w-[calc(50%-0.75rem)]">
      <div className="flex items-start gap-3">
        <ShieldCheck className="mt-0.5 size-5 shrink-0 text-primary" />
        <div>
          <h2 id="verify-title" className="text-lg font-semibold">{renewing ? t("owner.settings.verify.titleRenew") : t("owner.settings.verify.title")}</h2>
          <p className="text-sm text-muted-foreground">
            {t("owner.settings.verify.intro")}
          </p>
        </div>
      </div>
      <VerificationSummary v={v} t={t} language={language} />
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
            {submit.isPending ? <Loader2 className="animate-spin" /> : <BadgeCheck />} {renewing ? t("owner.settings.verify.submitRenewal") : t("owner.settings.verify.submit")}
          </Button>
        </form>
      ) : null}
    </section>
  );
}

const KYC_TYPE_KEYS = ["gstin", "udyam", "trade_licence"] as const;

function VerificationSummary({ v, t, language }: { v: ShopVerification; t: Translate; language: LanguageCode }) {
  const kycType = KYC_TYPE_KEYS.find((k) => k === v.kyc_type);
  const typeLabel = kycType ? t(`owner.settings.verify.kycTypes.${kycType}`) : t("owner.settings.verify.number");
  const evidence = v.kyc_number ? t("owner.settings.verify.evidence", { type: typeLabel, number: v.kyc_number }) : null;
  const day = (iso: string | null | undefined) => formatDay(iso, language);
  let line: string;
  if (v.status === "verified" && v.renewal_pending) line = t("owner.settings.verify.summary.renewalPending");
  else if (v.status === "verified" && verificationNotice(v)?.kind === "expired") {
    line = t("owner.settings.verify.summary.expired", { date: day(v.verified_until) });
  } else if (v.status === "verified") {
    line = t("owner.settings.verify.summary.verifiedUntil", { date: day(v.verified_until) });
    if (!v.can_submit && v.renewal_opens_at) line += ` ${t("owner.settings.verify.summary.renewFrom", { date: day(v.renewal_opens_at) })}`;
    if (!v.visible_to_customers) line += ` ${t("owner.settings.verify.summary.switchedOff")}`;
  }
  else if (v.status === "submitted") line = t("owner.settings.verify.summary.submitted");
  else if (v.status === "rejected") line = t("owner.settings.verify.summary.rejected");
  else if (v.status === "suspended") line = t("owner.settings.verify.summary.suspended");
  else line = t("owner.settings.verify.summary.draft");
  return (
    <div className="space-y-1 rounded-xl bg-muted/50 p-3 text-sm" data-testid="verification-summary">
      <p>{line}</p>
      {v.rejection_reason && <p><span className="font-medium">{t("owner.settings.verify.reason")}</span> {v.rejection_reason}</p>}
      {evidence && <p className="text-muted-foreground">{evidence}</p>}
      {!v.phone_verified && v.status !== "draft" && (
        <p className="text-muted-foreground">{t("owner.settings.verify.callBack")}</p>
      )}
    </div>
  );
}
