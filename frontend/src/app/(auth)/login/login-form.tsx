"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Eye, EyeOff, Loader2, LogOut, ShieldCheck, Smartphone } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useEffectEvent, useRef, useState } from "react";

import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { homeFor, useMe, useSignOut } from "@/hooks/use-session";
import { ApiError, api, errorMessage } from "@/lib/api";
import { DEFAULT_LANGUAGE, type LanguageCode, languageInfo, richText } from "@/lib/i18n";
import type { OtpChannel, OtpSent, PhoneSignin, User } from "@/lib/types";

// Build-time flag, off by default (next.config.ts gives it the value "false"). Only a demo
// deployment built with NEXT_PUBLIC_NEARSHOP_DEMO_MODE=true shows the demo-account shortcuts;
// in any other build this condition is the constant false and the demo module is left out.
const DemoAccounts =
  process.env.NEXT_PUBLIC_NEARSHOP_DEMO_MODE === "true" ? dynamic(() => import("./demo-accounts")) : null;

const linkClass = "font-medium text-foreground underline underline-offset-4";

export function safeNext(next: string | null) {
  // Only allow same-site relative paths to avoid open redirects.
  return next && next.startsWith("/") && !next.startsWith("//") ? next : null;
}

// ---------------------------------------------------------------- phone codes (P4.2), shared by every sign-in screen

/** Whether phone sign-in is available here (P4.1), the code's rules and the email sign-in sunset. The phone screens
 * show only while it says available; if it cannot be loaded, the email screens show. */
export function usePhoneSignin() {
  return useQuery({
    queryKey: ["phone-signin"],
    queryFn: () => api.get<PhoneSignin>("/auth/phone-signin"),
    staleTime: 5 * 60_000,
  });
}

/** The 10 digits of an Indian mobile number typed with or without +91 (or a leading 0), spaces and dashes. */
export function normalisePhone(value: string) {
  const digits = value.replace(/\D/g, "");
  if (digits.length === 12 && digits.startsWith("91")) return digits.slice(2);
  if (digits.length === 11 && digits.startsWith("0")) return digits.slice(1);
  return digits;
}

/** The message key of the inline format error for a phone field, or null when the number is a valid Indian mobile. */
export function phoneProblem(value: string) {
  const phone = normalisePhone(value);
  if (!phone) return "auth.phone.empty" as const;
  if (!/^[6-9]\d{9}$/.test(phone)) return "auth.phone.invalid" as const;
  return null;
}

export function formatPhone(phone: string) {
  return `+91 ${phone.slice(0, 5)} ${phone.slice(5)}`;
}

/** An IST day (YYYY-MM-DD) as "10 April 2027", with the month named in the interface language. */
export function formatDay(day: string, language: LanguageCode = DEFAULT_LANGUAGE) {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString(languageInfo(language).htmlLang, {
    day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
  });
}

/** A phone number field: 10 digits, with or without +91. The format error shows inline once the field is left, or
 * after a submit (showError). */
export function PhoneField({
  id,
  value,
  onChange,
  showError = false,
  label,
  hint,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  showError?: boolean;
  label?: React.ReactNode;
  hint?: string;
}) {
  const { t } = useI18n();
  const [left, setLeft] = useState(false);
  const problem = (left && value !== "") || showError ? phoneProblem(value) : null;
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label ?? t("auth.phone.label")}</Label>
      <Input
        id={id}
        type="tel"
        inputMode="tel"
        autoComplete="tel"
        maxLength={16}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={() => setLeft(true)}
        aria-invalid={!!problem}
        aria-describedby={`${id}-hint`}
        className="h-11"
      />
      <p id={`${id}-hint`} className={problem ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
        {problem ? t(problem) : (hint ?? t("auth.phone.hint"))}
      </p>
    </div>
  );
}

/** The 6-digit code field: the one-time-code autocomplete hint and the numeric keypad. Where the browser has WebOTP
 * (Android Chrome), the code read from the SMS (its last line is "@<domain> #<code>", P4.1) arrives as autoCode
 * (useSendCode listens from the moment the code is asked for): it fills the field and calls onAutoFill. Typing the
 * code always works too. */
export function CodeField({
  id,
  value,
  onChange,
  onAutoFill,
  autoCode,
  describedBy,
  length = 6,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  onAutoFill?: (code: string) => void;
  autoCode?: string | null;
  describedBy?: string;
  length?: number;
}) {
  const { t } = useI18n();
  const filled = useEffectEvent((code: string) => {
    onChange(code);
    onAutoFill?.(code);
  });
  useEffect(() => {
    if (autoCode) filled(autoCode.slice(0, length));
  }, [autoCode, length]);
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{t("auth.code.label", { length })}</Label>
      <Input
        id={id}
        inputMode="numeric"
        autoComplete="one-time-code"
        pattern={`[0-9]{${length}}`}
        maxLength={length}
        required
        // The field appears in answer to the user's own "Send code", so focus moves to it (P2.10, docs/accessibility.md).
        // eslint-disable-next-line jsx-a11y/no-autofocus
        autoFocus
        value={value}
        onChange={(e) => onChange(e.target.value.replace(/\D/g, "").slice(0, length))}
        aria-describedby={describedBy}
        className="h-11 font-mono text-lg tracking-[0.4em]"
      />
    </div>
  );
}

type Sent = { phone: string; channel: OtpChannel; at: number; expiresIn: number };

/** Sending codes for one purpose: the last send (phone, channel, time) and the seconds before a resend is allowed.
 * Where the browser has WebOTP (Android Chrome), it listens for the SMS from the moment a code is asked for, not from
 * when the code field appears, so a code that arrives before the request has answered is still read: autoCode. */
export function useSendCode(purpose: "login" | "register" | "verify") {
  const [sent, setSent] = useState<Sent | null>(null);
  const [resendIn, setResendIn] = useState(0);
  const [autoCode, setAutoCode] = useState<string | null>(null);
  const listening = useRef<AbortController | null>(null);
  useEffect(() => {
    if (resendIn <= 0) return;
    const timer = setTimeout(() => setResendIn((s) => s - 1), 1000);
    return () => clearTimeout(timer);
  }, [resendIn]);
  const stopListening = () => {
    listening.current?.abort();
    listening.current = null;
  };
  useEffect(() => () => listening.current?.abort(), []); // leaving the screen stops listening
  const listen = () => {
    stopListening();
    setAutoCode(null);
    if (!("OTPCredential" in window) || !navigator.credentials) return;
    const abort = new AbortController();
    listening.current = abort;
    navigator.credentials
      .get({ otp: { transport: ["sms"] }, signal: abort.signal } as CredentialRequestOptions)
      .then((credential) => {
        const code = (credential as { code?: string } | null)?.code;
        if (!abort.signal.aborted && code && /^\d+$/.test(code)) setAutoCode(code);
      })
      .catch(() => undefined); // dismissed, aborted, or no SMS: the code is typed instead
  };
  const send = useMutation({
    mutationFn: ({ phone, channel }: { phone: string; channel?: OtpChannel }) =>
      api.post<OtpSent>("/auth/otp/request", { phone, purpose, ...(channel ? { channel } : {}) }),
    meta: { silent: true },
    onMutate: listen,
    onSuccess: (res, { phone }) => {
      setSent({ phone, channel: res.channel, at: Date.now(), expiresIn: res.expires_in });
      setResendIn(res.resend_after);
    },
    onError: stopListening,
  });
  /** Whether a code sent to this phone is still valid (with a minute to spare), so a step back and forward need
   * not send another. */
  const fresh = (phone: string) => !!sent && sent.phone === phone && Date.now() - sent.at < (sent.expiresIn - 60) * 1000;
  const reset = () => {
    stopListening();
    setAutoCode(null);
    setSent(null);
    send.reset();
  };
  return { send, sent, resendIn, fresh, reset, autoCode };
}

type SendCode = ReturnType<typeof useSendCode>;

/** Where the code went, and the resend controls: a new code after 60 s, by SMS instead of WhatsApp when both exist. */
export function CodeSentNote({ otp, id, channels }: { otp: SendCode; id: string; channels: OtpChannel[] }) {
  const { t } = useI18n();
  if (!otp.sent) return null;
  const { phone, channel } = otp.sent;
  return (
    <div className="space-y-2 text-sm">
      <p id={id} role="status" className="text-muted-foreground">
        {richText(t(`auth.code.sent.${channel}`), {
          phone: <span className="font-medium whitespace-nowrap text-foreground">{formatPhone(phone)}</span>,
        })}
      </p>
      {otp.resendIn > 0 ? (
        <p className="text-muted-foreground">{t("auth.code.resendIn", { seconds: otp.resendIn })}</p>
      ) : (
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <button type="button" className={linkClass} disabled={otp.send.isPending} onClick={() => otp.send.mutate({ phone })}>
            {t("auth.code.resend")}
          </button>
          {channel === "whatsapp" && channels.includes("sms") && (
            <button type="button" className={linkClass} disabled={otp.send.isPending} onClick={() => otp.send.mutate({ phone, channel: "sms" })}>
              {t("auth.code.bySms")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function FormError({ children }: { children: React.ReactNode }) {
  return <div role="alert" className="rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive">{children}</div>;
}

function Checking({ label }: { label: string }) {
  return (
    <div role="status" className="flex items-center gap-2 py-16 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" aria-hidden /> {label}
    </div>
  );
}

/** After a sign-in: the new user in the cache, then the page asked for (when the role may see it), else home. */
function useSignedIn() {
  const router = useRouter();
  const params = useSearchParams();
  const qc = useQueryClient();
  return (user: User) => {
    qc.setQueryData(["me"], user);
    qc.invalidateQueries({ refetchType: "none" });
    const next = safeNext(params.get("next"));
    const allowed = next && !(next.startsWith("/shop") && user.role !== "owner" && user.role !== "staff") && !(next.startsWith("/admin") && user.role !== "admin");
    router.replace(allowed ? next : homeFor(user));
  };
}

function SignUpLinks() {
  const { t } = useI18n();
  const params = useSearchParams();
  return (
    <p className="mt-6 text-sm text-muted-foreground">
      {richText(t("auth.login.newHere"), {
        create: (
          <Link href={`/register${params.get("next") ? `?next=${encodeURIComponent(params.get("next")!)}` : ""}`} className={linkClass}>
            {t("auth.login.createAccount")}
          </Link>
        ),
        listShop: <Link href="/register/shop" className={linkClass}>{t("auth.login.listShop")}</Link>,
      })}
    </p>
  );
}

// ---------------------------------------------------------------- the sign-in screen

/** Phone first, with a code, while phone sign-in is available (P4.2); otherwise today's email and password form. The
 * "Use email and password" link stays until the sunset; from then on it is for admins (password and authenticator
 * code) and for owners whose phone is verified. */
export function LoginForm() {
  const { t } = useI18n();
  const { data: status, isPending } = usePhoneSignin();
  const [method, setMethod] = useState<"phone" | "email" | null>(null);
  if (isPending) return <Checking label={t("auth.login.loading")} />;
  const phoneOn = status?.available === true;
  if (phoneOn && (method ?? "phone") === "phone") return <PhoneLogin status={status} onUseEmail={() => setMethod("email")} />;
  return <EmailLogin phoneStatus={phoneOn ? status : undefined} onUsePhone={() => setMethod("phone")} />;
}

function PhoneLogin({ status, onUseEmail }: { status: PhoneSignin; onUseEmail: () => void }) {
  const { t, language } = useI18n();
  const done = useSignedIn();
  const otp = useSendCode("login");
  const [phone, setPhone] = useState("");
  const [tried, setTried] = useState(false);
  const [code, setCode] = useState("");
  const verify = useMutation({
    mutationFn: (value: string) => api.post<User>("/auth/otp/verify", { phone: otp.sent!.phone, purpose: "login", code: value }),
    meta: { silent: true },
    onSuccess: done,
  });
  const noAccount = verify.error instanceof ApiError && verify.error.code === "no_account";

  return (
    <div>
      <h1 className="text-3xl font-semibold">{t("auth.login.welcome")}</h1>
      <p className="mt-2 text-sm text-muted-foreground">{t("auth.login.phoneIntro")}</p>

      {!otp.sent ? (
        <form
          className="mt-8 space-y-4"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            setTried(true);
            if (!phoneProblem(phone)) otp.send.mutate({ phone: normalisePhone(phone) });
          }}
        >
          <PhoneField id="phone" value={phone} onChange={setPhone} showError={tried} />
          {otp.send.isError && <FormError>{errorMessage(otp.send.error)}</FormError>}
          <Button type="submit" size="lg" className="h-11 w-full" disabled={otp.send.isPending}>
            {otp.send.isPending ? <Loader2 className="animate-spin" /> : <Smartphone />} {t("auth.code.send")}
          </Button>
        </form>
      ) : (
        <form
          className="mt-8 space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (code.length === 6) verify.mutate(code);
          }}
        >
          <CodeSentNote otp={otp} id="code-sent" channels={status.channels} />
          <CodeField id="code" value={code} onChange={setCode} onAutoFill={(c) => verify.mutate(c)} autoCode={otp.autoCode} describedBy="code-sent" />
          {(verify.isError || otp.send.isError) && (
            <FormError>
              {errorMessage(verify.error ?? otp.send.error)}
              {noAccount && (
                <>
                  {" "}
                  <Link href="/register" className="font-medium underline underline-offset-4">{t("auth.login.createAccount")}</Link>
                </>
              )}
            </FormError>
          )}
          <Button type="submit" size="lg" className="h-11 w-full" disabled={verify.isPending || code.length !== 6}>
            {verify.isPending && <Loader2 className="animate-spin" />} {t("auth.common.signIn")}
          </Button>
          <button
            type="button"
            className="text-sm text-muted-foreground underline underline-offset-4 hover:text-foreground"
            onClick={() => {
              otp.reset();
              verify.reset();
              setCode("");
            }}
          >
            {t("auth.code.differentNumber")}
          </button>
        </form>
      )}

      <p className="mt-6 text-sm text-muted-foreground">
        {status.email_login_ended ? (
          richText(t("auth.login.passwordPrompt"), {
            link: <button type="button" className={linkClass} onClick={onUseEmail}>{t("auth.login.signInWithPassword")}</button>,
          })
        ) : (
          <>
            <button type="button" className={linkClass} onClick={onUseEmail}>{t("auth.login.useEmail")}</button>{" "}
            <span>{t("auth.login.until", { date: formatDay(status.email_login_sunset, language) })}</span>
          </>
        )}
      </p>
      <SignUpLinks />
    </div>
  );
}

function EmailLogin({ phoneStatus, onUsePhone }: { phoneStatus?: PhoneSignin; onUsePhone: () => void }) {
  const { t } = useI18n();
  const done = useSignedIn();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  // Admins confirm the password with a second factor: the API answers totp_required, and the form then
  // asks for an authenticator code (or a recovery code) and sends it with the same email and password.
  const [needsCode, setNeedsCode] = useState(false);
  const [code, setCode] = useState("");

  const changeEmail = (value: string) => {
    setEmail(value);
    setNeedsCode(false);
    setCode("");
  };

  const login = useMutation({
    mutationFn: () => api.post<User>("/auth/login", needsCode ? { email, password, code: code.trim() } : { email, password }),
    meta: { silent: true },
    onError: (error) => {
      if (error instanceof ApiError && error.code === "totp_required") setNeedsCode(true);
    },
    onSuccess: done,
  });
  // After the sunset (P4.2) a customer or owner without a verified phone is told to sign in with the phone.
  const phoneRequired = login.error instanceof ApiError && login.error.code === "phone_signin_required";

  return (
    <div>
      <h1 className="text-3xl font-semibold">{t("auth.login.welcome")}</h1>
      <p className="mt-2 text-sm text-muted-foreground">{t("auth.login.emailIntro")}</p>

      <form
        className="mt-8 space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          login.mutate();
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor="email">{t("auth.common.email")}</Label>
          <Input id="email" type="email" autoComplete="email" required value={email} onChange={(e) => changeEmail(e.target.value)} className="h-11" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="password">{t("auth.common.password")}</Label>
          <div className="relative">
            <Input
              id="password"
              type={show ? "text" : "password"}
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
                setNeedsCode(false);
                setCode("");
              }}
              className="h-11 pr-11"
            />
            <button
              type="button"
              onClick={() => setShow((v) => !v)}
              className="absolute top-1/2 right-2 grid size-8 -translate-y-1/2 place-items-center rounded-md text-muted-foreground hover:bg-muted"
              aria-label={show ? t("auth.login.hidePassword") : t("auth.login.showPassword")}
            >
              {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
            </button>
          </div>
        </div>
        {needsCode && (
          <div className="space-y-1.5 rounded-lg border p-3">
            <Label htmlFor="code" className="flex items-center gap-1.5">
              <ShieldCheck className="size-4" aria-hidden /> {t("auth.login.authCode")}
            </Label>
            <p id="code-hint" className="text-xs text-muted-foreground">
              {t("auth.login.authCodeHint")}
            </p>
            <Input
              id="code"
              autoComplete="one-time-code"
              autoCapitalize="off"
              spellCheck={false}
              // The field appears in answer to the user's own submit, so focus moves to it (P2.10, docs/accessibility.md).
              // eslint-disable-next-line jsx-a11y/no-autofocus
              autoFocus
              required
              maxLength={32}
              aria-describedby="code-hint"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className="h-11 font-mono tracking-widest"
            />
          </div>
        )}
        {login.isError && !(login.error instanceof ApiError && login.error.code === "totp_required") && (
          <FormError>
            {errorMessage(login.error)}
            {phoneRequired && phoneStatus && (
              <>
                {" "}
                <button type="button" className="font-medium underline underline-offset-4" onClick={onUsePhone}>{t("auth.login.signInWithPhone")}</button>
              </>
            )}
          </FormError>
        )}
        <Button type="submit" size="lg" className="h-11 w-full" disabled={login.isPending}>
          {login.isPending && <Loader2 className="animate-spin" />} {needsCode ? t("auth.login.verifyAndSignIn") : t("auth.common.signIn")}
        </Button>
      </form>

      {phoneStatus && (
        <p className="mt-6 text-sm text-muted-foreground">
          <button type="button" className={linkClass} onClick={onUsePhone}>{t("auth.login.useCode")}</button>
        </p>
      )}
      <SignUpLinks />

      {DemoAccounts && <DemoAccounts onPick={changeEmail} />}
    </div>
  );
}

// ---------------------------------------------------------------- /verify-phone

/** The /verify-phone screen (P4.2): a signed-in customer or owner verifies a phone number with a code (purpose verify).
 * The banner links here before the sunset, and every page sends such a user here from it. A number already verified
 * on another account is refused with a support message: accounts are never merged automatically. */
export function VerifyPhoneForm() {
  const { t, language } = useI18n();
  const { data: me, isPending } = useMe();
  const { data: status, isPending: statusPending } = usePhoneSignin();
  const params = useSearchParams();
  const router = useRouter();
  const qc = useQueryClient();
  const signOut = useSignOut();
  const otp = useSendCode("verify");
  const [phone, setPhone] = useState<string | null>(null); // null: the account's own number, if it has one
  const [tried, setTried] = useState(false);
  const [code, setCode] = useState("");
  const next = safeNext(params.get("next"));
  const verify = useMutation({
    mutationFn: (value: string) => api.post<User>("/auth/otp/verify", { phone: otp.sent!.phone, purpose: "verify", code: value }),
    meta: { silent: true },
    onSuccess: (user) => {
      qc.setQueryData(["me"], user);
      qc.invalidateQueries({ refetchType: "none" });
      router.replace(next && !next.startsWith("/verify-phone") ? next : homeFor(user));
    },
  });

  if (isPending || statusPending) return <Checking label={t("auth.verify.loading")} />;
  if (!me) {
    return (
      <div>
        <h1 className="text-3xl font-semibold">{t("auth.verify.title")}</h1>
        <p className="mt-3 text-sm text-muted-foreground">{t("auth.verify.signInFirst")}</p>
        <Button asChild size="lg" className="mt-8 h-11">
          <Link href="/login?next=/verify-phone">{t("auth.common.signIn")}</Link>
        </Button>
      </div>
    );
  }

  const value = phone ?? me.phone ?? "";
  const sunset = formatDay(me.email_login_sunset, language);
  const required = me.phone_migration === "required";
  const inUse = verify.error instanceof ApiError && verify.error.code === "phone_taken";
  const signOutLink = (
    <p className="mt-6 text-sm text-muted-foreground">
      {richText(t("auth.verify.notYours"), {
        signOut: (
          <button type="button" className={`${linkClass} inline-flex items-center gap-1`} onClick={signOut}>
            <LogOut className="size-3.5" aria-hidden /> {t("auth.verify.signOut")}
          </button>
        ),
      })}
    </p>
  );

  if (me.phone_verified || me.role === "admin") {
    return (
      <div>
        <div className="mb-5 grid size-12 place-items-center rounded-2xl bg-success-soft text-success-ink">
          <CheckCircle2 className="size-6" aria-hidden />
        </div>
        <h1 className="text-3xl font-semibold">{me.role === "admin" ? t("auth.verify.nothingTitle") : t("auth.verify.verifiedTitle")}</h1>
        <p className="mt-3 text-sm text-muted-foreground">
          {me.role === "admin"
            ? t("auth.verify.adminBody")
            : me.phone
              ? t("auth.verify.verifiedBody", { phone: formatPhone(me.phone) })
              : t("auth.verify.verifiedBodyNoPhone")}
        </p>
        <Button asChild size="lg" className="mt-8 h-11">
          <Link href={next && !next.startsWith("/verify-phone") ? next : homeFor(me)}>{t("auth.verify.continue")}</Link>
        </Button>
      </div>
    );
  }

  if (!status?.available) {
    return (
      <div>
        <h1 className="text-3xl font-semibold">{t("auth.verify.title")}</h1>
        <p className="mt-3 text-sm text-muted-foreground">{t("auth.verify.unavailable")}</p>
        <Button asChild size="lg" className="mt-8 h-11">
          <Link href={homeFor(me)}>{t("auth.verify.continue")}</Link>
        </Button>
        {signOutLink}
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-3xl font-semibold">{required ? t("auth.verify.requiredTitle") : t("auth.verify.title")}</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        {required ? t("auth.verify.requiredBody", { date: sunset }) : t("auth.verify.warnBody", { date: sunset })}
      </p>

      {!otp.sent ? (
        <form
          className="mt-8 space-y-4"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            setTried(true);
            if (!phoneProblem(value)) otp.send.mutate({ phone: normalisePhone(value) });
          }}
        >
          <PhoneField id="phone" value={value} onChange={setPhone} showError={tried} />
          {otp.send.isError && <FormError>{errorMessage(otp.send.error)}</FormError>}
          <Button type="submit" size="lg" className="h-11 w-full" disabled={otp.send.isPending}>
            {otp.send.isPending ? <Loader2 className="animate-spin" /> : <Smartphone />} {t("auth.code.send")}
          </Button>
        </form>
      ) : (
        <form
          className="mt-8 space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (code.length === 6) verify.mutate(code);
          }}
        >
          <CodeSentNote otp={otp} id="code-sent" channels={status.channels} />
          <CodeField id="code" value={code} onChange={setCode} onAutoFill={(c) => verify.mutate(c)} autoCode={otp.autoCode} describedBy="code-sent" />
          {(verify.isError || otp.send.isError) && (
            <FormError>
              {errorMessage(verify.error ?? otp.send.error)}
              {inUse && (
                <>
                  {" "}
                  <Link href="/grievance" className="font-medium underline underline-offset-4">{t("auth.verify.contactSupport")}</Link>
                </>
              )}
            </FormError>
          )}
          <Button type="submit" size="lg" className="h-11 w-full" disabled={verify.isPending || code.length !== 6}>
            {verify.isPending && <Loader2 className="animate-spin" />} {t("auth.verify.verify")}
          </Button>
          <button
            type="button"
            className="text-sm text-muted-foreground underline underline-offset-4 hover:text-foreground"
            onClick={() => {
              otp.reset();
              verify.reset();
              setCode("");
            }}
          >
            {t("auth.code.differentNumber")}
          </button>
        </form>
      )}
      {signOutLink}
    </div>
  );
}
