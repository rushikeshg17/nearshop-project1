"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, PauseCircle, Smartphone } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import {
  CodeField,
  CodeSentNote,
  FormError,
  normalisePhone,
  PhoneField,
  phoneProblem,
  safeNext,
  usePhoneSignin,
  useSendCode,
} from "@/app/(auth)/login/login-form";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useMeta } from "@/hooks/use-session";
import { api, ApiError, errorMessage } from "@/lib/api";
import type { PhoneSignin, User } from "@/lib/types";

/** True when the API refused a sign-up because sign-ups are closed (403 signups_closed). */
export function isSignupsClosed(error: unknown) {
  return error instanceof ApiError && error.code === "signups_closed";
}

/** Shown on both sign-up screens while /api/meta is loading, so no form appears before we know. */
export function SignupsChecking() {
  return (
    <div role="status" className="flex items-center gap-2 py-16 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" aria-hidden /> Loading sign-up…
    </div>
  );
}

/** Shown on both sign-up screens while /api/meta reports signups_open: false, or when the API
 * refuses a sign-up with signups_closed (for example if /api/meta could not be loaded). */
export function SignupsPaused({ signInHref = "/login" }: { signInHref?: string }) {
  return (
    <div role="status">
      <div className="mb-5 grid size-12 place-items-center rounded-2xl bg-warning-soft text-warning-ink">
        <PauseCircle className="size-6" aria-hidden />
      </div>
      <h1 className="text-3xl font-semibold">Sign-ups are paused while we prepare for launch</h1>
      <p className="mt-3 text-sm text-muted-foreground">
        New accounts cannot be created right now. You can still search what nearby shops have in stock.
      </p>
      <div className="mt-8 flex flex-wrap gap-3">
        <Button asChild size="lg" className="h-11">
          <Link href="/search">Search nearby</Link>
        </Button>
        <Button asChild variant="outline" size="lg" className="h-11">
          <Link href={signInHref}>Sign in</Link>
        </Button>
      </div>
    </div>
  );
}

/** What the user agreed to at sign-up (P3.1). The API records each purpose, and the age declaration, as a
 * consent row against the current privacy notice version. Optional purposes are off until turned on. */
export type Consent = {
  account_and_orders: boolean;
  age_18_plus: boolean;
  whatsapp: boolean;
  personalised_suggestions: boolean;
  search_measurement: boolean;
};

export const NO_CONSENT: Consent = {
  account_and_orders: false,
  age_18_plus: false,
  whatsapp: false,
  personalised_suggestions: false,
  search_measurement: false,
};

/** True when both required boxes are ticked: the required purposes and the 18-or-older declaration. */
export function consentComplete(c: Consent) {
  return c.account_and_orders && c.age_18_plus;
}

const OPTIONAL_PURPOSES: { key: keyof Consent; label: string; hint: string }[] = [
  { key: "whatsapp", label: "WhatsApp messages", hint: "Reservation and order updates on WhatsApp." },
  { key: "personalised_suggestions", label: "Personalised suggestions", hint: "Suggestions based on your search history." },
  { key: "search_measurement", label: "Help improve search", hint: "Use my searches to measure and improve search results." },
];

/** The consent block on both sign-up screens: two required boxes, then optional toggles, off by default. */
export function ConsentFields({ value, onChange, idPrefix }: { value: Consent; onChange: (c: Consent) => void; idPrefix: string }) {
  const set = (key: keyof Consent) => (checked: boolean) => onChange({ ...value, [key]: checked });
  const link = "font-medium text-foreground underline underline-offset-4";
  return (
    <fieldset className="space-y-4">
      <legend className="sr-only">Privacy and consent</legend>
      <div className="flex items-start gap-3">
        <Checkbox id={`${idPrefix}-consent`} checked={value.account_and_orders} onCheckedChange={(c) => set("account_and_orders")(c === true)} className="mt-0.5" aria-required />
        <Label htmlFor={`${idPrefix}-consent`} className="block text-sm leading-snug font-normal">
          I agree that NearShop uses my details to run my account and fulfil my reservations and orders, as described in the{" "}
          <Link href="/privacy" target="_blank" className={link}>privacy notice</Link> and{" "}
          <Link href="/terms" target="_blank" className={link}>terms</Link>.
        </Label>
      </div>
      <div className="flex items-start gap-3">
        <Checkbox id={`${idPrefix}-age`} checked={value.age_18_plus} onCheckedChange={(c) => set("age_18_plus")(c === true)} className="mt-0.5" aria-required aria-describedby={`${idPrefix}-age-hint`} />
        <div>
          <Label htmlFor={`${idPrefix}-age`} className="block text-sm leading-snug font-normal">I am 18 or older.</Label>
          <p id={`${idPrefix}-age-hint`} className="mt-1 text-xs text-muted-foreground">
            NearShop is for adults. We do not create accounts for children, because that needs a parent&apos;s verified consent.
          </p>
        </div>
      </div>
      <div className="space-y-3 rounded-lg border p-3">
        <p className="text-xs font-medium text-muted-foreground">Optional. You can leave these off.</p>
        {OPTIONAL_PURPOSES.map(({ key, label, hint }) => (
          <div key={key} className="flex items-start justify-between gap-3">
            <div>
              <Label htmlFor={`${idPrefix}-${key}`} className="text-sm">{label}</Label>
              <p id={`${idPrefix}-${key}-hint`} className="text-xs text-muted-foreground">{hint}</p>
            </div>
            <Switch id={`${idPrefix}-${key}`} checked={value[key]} onCheckedChange={set(key)} aria-describedby={`${idPrefix}-${key}-hint`} className="mt-0.5" />
          </div>
        ))}
      </div>
    </fieldset>
  );
}

/** The message for a sign-up the API refused for missing consent or a missing age declaration. */
export function consentMessage(consent: Consent) {
  if (!consent.account_and_orders) return "Please agree to the privacy notice and terms to create an account.";
  if (!consent.age_18_plus) return "NearShop is for adults. You need to be 18 or older to create an account.";
  return null;
}

export function RegisterForm() {
  const { data: meta, isPending } = useMeta();
  const { data: phone, isPending: phonePending } = usePhoneSignin();
  if (meta?.signups_open === false) return <SignupsPaused />;
  if (isPending || phonePending) return <SignupsChecking />;
  // If /api/meta failed, show the form: the API still refuses a closed sign-up, and the form then
  // switches to the paused state. Phone first while phone sign-in is available (P4.2); otherwise email.
  return phone?.available ? <PhoneRegisterForm status={phone} /> : <OpenRegisterForm />;
}

/** Registration by phone (P4.2): name, mobile number, an optional email and the consent, then the 6-digit code sent to
 * the phone, which creates the account (POST /api/auth/otp/verify, purpose register) and signs it in. */
function PhoneRegisterForm({ status }: { status: PhoneSignin }) {
  const router = useRouter();
  const params = useSearchParams();
  const qc = useQueryClient();
  const otp = useSendCode("register");
  const [form, setForm] = useState({ name: "", phone: "", email: "" });
  const [consent, setConsent] = useState<Consent>(NO_CONSENT);
  const [problem, setProblem] = useState<string | null>(null);
  const [tried, setTried] = useState(false);
  const [step, setStep] = useState<"details" | "code">("details");
  const [code, setCode] = useState("");
  const phone = normalisePhone(form.phone);

  const register = useMutation({
    mutationFn: (value: string) =>
      api.post<User>("/auth/otp/verify", {
        phone: otp.sent!.phone, purpose: "register", code: value, name: form.name.trim(), consent,
        ...(form.email.trim() ? { email: form.email.trim() } : {}),
      }),
    meta: { silent: true },
    onSuccess: (user) => {
      qc.setQueryData(["me"], user);
      router.replace(safeNext(params.get("next")) ?? "/search");
    },
  });
  const phoneTaken = register.error instanceof ApiError && register.error.code === "phone_taken";

  if (isSignupsClosed(register.error) || isSignupsClosed(otp.send.error)) return <SignupsPaused />;

  const details = () => {
    setTried(true);
    // A wrong phone number shows inline under its field; the other problems show above the button.
    const missing = form.name.trim().length < 2 ? "Please enter your full name." : consentMessage(consent);
    setProblem(missing);
    if (missing || phoneProblem(form.phone)) return;
    register.reset();
    if (otp.fresh(phone)) setStep("code"); // back from the code step with the same number: that code still works
    else otp.send.mutate({ phone }, { onSuccess: () => setStep("code") });
  };

  return (
    <div>
      <h1 className="text-3xl font-semibold">Create your account</h1>
      <p className="mt-2 text-sm text-muted-foreground">Reserve items at nearby shops and track pickups in one place. You sign in with a code sent to your phone.</p>
      {step === "details" ? (
        <form
          className="mt-8 space-y-4"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            details();
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor="name">Full name</Label>
            <Input id="name" autoComplete="name" required minLength={2} value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} className="h-11" />
          </div>
          <PhoneField id="phone" value={form.phone} onChange={(v) => setForm((f) => ({ ...f, phone: v }))} showError={tried} />
          <div className="space-y-1.5">
            <Label htmlFor="email">
              Email <span className="font-normal text-muted-foreground">(optional)</span>
            </Label>
            <Input id="email" type="email" autoComplete="email" value={form.email} onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))} className="h-11" />
          </div>
          <ConsentFields
            idPrefix="reg"
            value={consent}
            onChange={(c) => {
              setConsent(c);
              if (problem) setProblem(null);
            }}
          />
          {(problem || otp.send.isError) && <FormError>{problem ?? errorMessage(otp.send.error)}</FormError>}
          <Button type="submit" size="lg" className="h-11 w-full" disabled={otp.send.isPending}>
            {otp.send.isPending ? <Loader2 className="animate-spin" /> : <Smartphone />} Send code
          </Button>
        </form>
      ) : (
        <form
          className="mt-8 space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (code.length === 6) register.mutate(code);
          }}
        >
          <CodeSentNote otp={otp} id="code-sent" channels={status.channels} />
          <CodeField id="code" value={code} onChange={setCode} onAutoFill={(c) => register.mutate(c)} autoCode={otp.autoCode} describedBy="code-sent" />
          {(register.isError || otp.send.isError) && (
            <FormError>
              {errorMessage(register.error ?? otp.send.error)}
              {phoneTaken && (
                <>
                  {" "}
                  <Link href="/login" className="font-medium underline underline-offset-4">Sign in</Link>
                </>
              )}
            </FormError>
          )}
          <Button type="submit" size="lg" className="h-11 w-full" disabled={register.isPending || code.length !== 6}>
            {register.isPending && <Loader2 className="animate-spin" />} Create account
          </Button>
          <button
            type="button"
            className="text-sm text-muted-foreground underline underline-offset-4 hover:text-foreground"
            onClick={() => {
              setStep("details");
              setCode("");
            }}
          >
            Change your details
          </button>
        </form>
      )}
      <p className="mt-6 text-sm text-muted-foreground">
        Already have an account? <Link href="/login" className="font-medium text-foreground underline underline-offset-4">Sign in</Link>
      </p>
      <p className="mt-2 text-sm text-muted-foreground">
        Own a shop? <Link href="/register/shop" className="font-medium text-foreground underline underline-offset-4">List it on NearShop</Link>
      </p>
    </div>
  );
}

function OpenRegisterForm() {
  const router = useRouter();
  const params = useSearchParams();
  const qc = useQueryClient();
  const [form, setForm] = useState({ name: "", email: "", phone: "", password: "" });
  const [consent, setConsent] = useState<Consent>(NO_CONSENT);
  const [consentError, setConsentError] = useState<string | null>(null);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const register = useMutation({
    mutationFn: () => api.post<User>("/auth/register", { ...form, phone: form.phone || null, consent }),
    meta: { silent: true },
    onSuccess: (user) => {
      qc.setQueryData(["me"], user);
      router.replace(safeNext(params.get("next")) ?? "/search");
    },
  });
  const fieldError = (f: string) => register.error instanceof ApiError && register.error.field === f;
  const strong = form.password.length >= 8;

  if (isSignupsClosed(register.error)) return <SignupsPaused />;

  return (
    <div>
      <h1 className="text-3xl font-semibold">Create your account</h1>
      <p className="mt-2 text-sm text-muted-foreground">Reserve items at nearby shops and track pickups in one place.</p>
      <form
        className="mt-8 space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          const missing = consentMessage(consent);
          setConsentError(missing);
          if (!missing) register.mutate();
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor="name">Full name</Label>
          <Input id="name" autoComplete="name" required minLength={2} value={form.name} onChange={set("name")} className="h-11" aria-invalid={fieldError("name")} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="email">Email</Label>
          <Input id="email" type="email" autoComplete="email" required value={form.email} onChange={set("email")} className="h-11" aria-invalid={fieldError("email")} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="phone">
            Mobile number <span className="font-normal text-muted-foreground">(optional, for delivery)</span>
          </Label>
          <Input
            id="phone"
            inputMode="numeric"
            autoComplete="tel-national"
            value={form.phone}
            onChange={(e) => setForm((f) => ({ ...f, phone: e.target.value.replace(/\D/g, "").slice(0, 10) }))}
            className="h-11"
            aria-invalid={fieldError("phone")}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="password">Password</Label>
          <Input id="password" type="password" autoComplete="new-password" required minLength={8} value={form.password} onChange={set("password")} className="h-11" aria-invalid={fieldError("password")} aria-describedby="pw-hint" />
          <p id="pw-hint" className={strong ? "text-xs text-success-ink" : "text-xs text-muted-foreground"}>At least 8 characters</p>
        </div>
        <ConsentFields
          idPrefix="reg"
          value={consent}
          onChange={(c) => {
            setConsent(c);
            if (consentError) setConsentError(consentMessage(c));
          }}
        />
        {(consentError || register.isError) && (
          <p role="alert" className="rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive">{consentError ?? errorMessage(register.error)}</p>
        )}
        <Button type="submit" size="lg" className="h-11 w-full" disabled={register.isPending}>
          {register.isPending && <Loader2 className="animate-spin" />} Create account
        </Button>
      </form>
      <p className="mt-6 text-sm text-muted-foreground">
        Already have an account? <Link href="/login" className="font-medium text-foreground underline underline-offset-4">Sign in</Link>
      </p>
      <p className="mt-2 text-sm text-muted-foreground">
        Own a shop? <Link href="/register/shop" className="font-medium text-foreground underline underline-offset-4">List it on NearShop</Link>
      </p>
    </div>
  );
}
