"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check, Loader2 } from "lucide-react";
import { AnimatePresence, motion, MotionConfig } from "motion/react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { CodeField, CodeSentNote, usePhoneSignin, useSendCode } from "@/app/(auth)/login/login-form";
import {
  type Consent,
  consentComplete,
  ConsentFields,
  isSignupsClosed,
  NO_CONSENT,
  SignupsChecking,
  SignupsPaused,
} from "@/app/(auth)/register/register-form";
import { useI18n } from "@/components/providers";
import {
  ShopBasicsFields,
  type ShopFormState,
  ShopLocationFields,
  ShopOperationsFields,
  shopPayload,
} from "@/components/shop/shop-form-fields";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useMeta } from "@/hooks/use-session";
import { api, errorMessage } from "@/lib/api";
import { richText } from "@/lib/i18n";
import type { User } from "@/lib/types";
import { cn } from "@/lib/utils";

const STEPS = ["account", "shop", "location", "hours"] as const;
// While phone sign-in is available (P4.2), a last step verifies the shop's phone with a code: it becomes the owner's
// verified sign-in number, and the shop's phone counts as verified, which an admin needs before the shop goes live
// (P3.3). The code comes last because it is valid for 5 minutes.
const VERIFY_STEP = "verify";

export function ShopSignup() {
  const { t } = useI18n();
  const router = useRouter();
  const qc = useQueryClient();
  const { data: meta, isPending: metaPending } = useMeta();
  const { data: phoneSignin, isPending: phonePending } = usePhoneSignin();
  const phoneOn = phoneSignin?.available === true;
  const steps: readonly (typeof STEPS[number] | typeof VERIFY_STEP)[] = phoneOn ? [...STEPS, VERIFY_STEP] : STEPS;
  const otp = useSendCode("register");
  const [code, setCode] = useState("");
  const [step, setStep] = useState(0);
  const [owner, setOwner] = useState({ name: "", email: "", password: "" });
  const [consent, setConsent] = useState<Consent>(NO_CONSENT);
  const [shop, setShop] = useState<ShopFormState>({
    name: "", tagline: "", description: "", category_slugs: [], phone: "", address_line: "", locality: "", pincode: "",
    lat: 15.148, lng: 76.923, open_time: "9:30 AM", close_time: "9:00 PM", closed_on: "",
    offers_pickup: true, offers_delivery: false, delivery_radius_km: 4, delivery_fee: 30, free_delivery_above: "", hold_minutes: 30,
  });
  const set = <K extends keyof ShopFormState>(k: K, v: ShopFormState[K]) => setShop((s) => ({ ...s, [k]: v }));

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- start the pin at the city centre once known
    if (meta) setShop((s) => (s.locality ? s : { ...s, lat: meta.city.center.lat, lng: meta.city.center.lng }));
  }, [meta]);

  const submit = useMutation({
    // value: the code sent to the shop's phone, on the verify step (the field's state may not have caught up yet
    // when the browser fills it in from the SMS).
    mutationFn: (value?: string) =>
      api.post<User>("/auth/register-shop", {
        owner: { ...owner, phone: shop.phone || null, consent, ...(phoneOn && value ? { code: value } : {}) },
        shop: shopPayload(shop),
      }),
    meta: { silent: true },
    onSuccess: (user) => {
      qc.setQueryData(["me"], user);
      router.replace("/shop/catalog?welcome=1");
    },
  });

  const valid = [
    owner.name.trim().length >= 2 && /\S+@\S+\.\S+/.test(owner.email) && owner.password.length >= 8 && consentComplete(consent),
    shop.name.trim().length >= 3 && shop.category_slugs.length > 0 && /^[6-9]\d{9}$/.test(shop.phone),
    shop.address_line.trim().length >= 5 && !!shop.locality,
    true,
    code.length === 6,
  ];

  if (meta?.signups_open === false || isSignupsClosed(submit.error)) {
    return <SignupsPaused signInHref="/login?next=/shop" />;
  }
  if (metaPending || phonePending) return <SignupsChecking />;
  const last = steps.length - 1;
  const error = submit.error ?? otp.send.error;

  return (
    <MotionConfig reducedMotion="user">
      <div>
        <p className="text-sm font-medium text-brand-ink">{t("auth.shopSignup.stepOf", { step: step + 1, total: steps.length })}</p>
        <h1 className="mt-1 text-3xl font-semibold">{t(`auth.shopSignup.steps.${steps[step]}`)}</h1>
        <div className="mt-4 flex gap-1.5" aria-hidden>
          {steps.map((s, i) => (
            <span key={s} className={cn("h-1 flex-1 rounded-full transition-colors duration-300", i <= step ? "bg-brand" : "bg-muted")} />
          ))}
        </div>

        <form
          className="mt-8"
          onSubmit={(e) => {
            e.preventDefault();
            if (!valid[step]) return;
            if (phoneOn && step === STEPS.length - 1) {
              // Into the verify step: a code to the shop's phone, unless one sent to it is still valid.
              submit.reset();
              if (otp.fresh(shop.phone)) setStep(step + 1);
              else otp.send.mutate({ phone: shop.phone }, { onSuccess: () => setStep(step + 1) });
            } else if (step < last) setStep(step + 1);
            else submit.mutate(phoneOn ? code : undefined);
          }}
        >
          <AnimatePresence mode="wait" initial={false}>
            <motion.div key={step} initial={{ opacity: 0, x: 16 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -16 }} transition={{ duration: 0.2 }}>
              {step === 0 && (
                <div className="space-y-4">
                  <div className="space-y-1.5">
                    <Label htmlFor="oname">{t("auth.shopSignup.name")}</Label>
                    <Input id="oname" autoComplete="name" value={owner.name} onChange={(e) => setOwner({ ...owner, name: e.target.value })} className="h-11" />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="oemail">{t("auth.common.email")}</Label>
                    <Input id="oemail" type="email" autoComplete="email" value={owner.email} onChange={(e) => setOwner({ ...owner, email: e.target.value })} className="h-11" />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="opw">{t("auth.common.password")}</Label>
                    <Input id="opw" type="password" autoComplete="new-password" value={owner.password} onChange={(e) => setOwner({ ...owner, password: e.target.value })} className="h-11" />
                    <p className="text-xs text-muted-foreground">{t("auth.common.passwordHint")}</p>
                  </div>
                  <ConsentFields idPrefix="owner" value={consent} onChange={setConsent} />
                </div>
              )}
              {step === 1 && <ShopBasicsFields s={shop} set={set} meta={meta} />}
              {step === 2 && <ShopLocationFields s={shop} set={set} meta={meta} />}
              {step === 3 && <ShopOperationsFields s={shop} set={set} />}
              {step === 4 && (
                <div className="space-y-4">
                  <p className="text-sm text-muted-foreground">
                    {t("auth.shopSignup.verifyNote")}
                  </p>
                  <CodeSentNote otp={otp} id="code-sent" channels={phoneSignin?.channels ?? []} />
                  <CodeField id="code" value={code} onChange={setCode} onAutoFill={(c) => submit.mutate(c)} autoCode={otp.autoCode} describedBy="code-sent" />
                </div>
              )}
            </motion.div>
          </AnimatePresence>

          {error && (
            <p role="alert" className="mt-4 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive">{errorMessage(error)}</p>
          )}

          <div className="mt-8 flex gap-3">
            {step > 0 && (
              <Button type="button" variant="outline" size="lg" className="h-11" onClick={() => setStep(step - 1)}>
                <ArrowLeft /> {t("auth.shopSignup.back")}
              </Button>
            )}
            <Button type="submit" size="lg" className="h-11 flex-1" disabled={!valid[step] || submit.isPending || otp.send.isPending}>
              {submit.isPending || otp.send.isPending ? <Loader2 className="animate-spin" /> : step === last ? <Check /> : null}
              {step === last
                ? t("auth.shopSignup.open")
                : phoneOn && step === STEPS.length - 1
                  ? t("auth.code.send")
                  : t("auth.shopSignup.continue")}
              {step < last && <ArrowRight />}
            </Button>
          </div>
        </form>
        <p className="mt-6 text-sm text-muted-foreground">
          {richText(t("auth.shopSignup.alreadyListed"), {
            link: (
              <Link href="/login?next=/shop" className="font-medium text-foreground underline underline-offset-4">
                {t("auth.common.signIn")}
              </Link>
            ),
          })}
        </p>
      </div>
    </MotionConfig>
  );
}
