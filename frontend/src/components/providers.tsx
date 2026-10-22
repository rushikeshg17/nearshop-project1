"use client";

import { MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Smartphone, X } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ThemeProvider } from "next-themes";
import { createContext, Suspense, use, useEffect, useMemo, useOptimistic, useState, useTransition } from "react";

import { useMe } from "@/hooks/use-session";
import { api, ApiError, errorMessage, setErrorTranslator } from "@/lib/api";
import {
  createTranslator,
  type Language,
  type LanguageCode,
  languageInfo,
  type Messages,
  type Translate,
  writeLanguageCookie,
} from "@/lib/i18n";
import { LocationProvider } from "@/lib/location";
import type { User } from "@/lib/types";

// ---------------------------------------------------------------- the interface language (P7.5, src/lib/i18n.ts)

export type I18n = {
  /** The language the page is rendered in. */
  language: LanguageCode;
  /** Its registry entry: name, html lang (also the Intl locale) and font. */
  info: Language;
  /** The language the visitor last chose: shown by the switches while the page re-renders in it. */
  selected: LanguageCode;
  /** The languages the switches offer (src/lib/i18n.ts, servableLanguages). With one, the switches are hidden. */
  languages: readonly Language[];
  t: Translate;
  /** Remember the choice (the nearshop_lang cookie) and re-render the page in it, without a full reload. */
  setLanguage: (code: LanguageCode) => void;
};

const I18nContext = createContext<I18n | null>(null);

/** The interface language in a client component. Server components use requestI18n from src/lib/i18n.ts. */
export function useI18n(): I18n {
  const i18n = use(I18nContext);
  if (!i18n) throw new Error("useI18n needs the Providers from src/components/providers.tsx");
  return i18n;
}

function I18nProvider({
  language,
  languages,
  messages,
  queryClient,
  children,
}: {
  language: LanguageCode;
  languages: readonly LanguageCode[];
  messages: Partial<Messages>;
  /** The page's query client: a signed-in visitor's choice is also saved to their account (below). */
  queryClient: QueryClient;
  children: React.ReactNode;
}) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [selected, setSelected] = useOptimistic(language);
  // One translator per set of messages, so that t keeps its identity across renders: it is a dependency of memos,
  // callbacks and effects (the location provider's, for one), which would otherwise re-run on every render. The
  // context value is memoised for the same reason.
  const t = useMemo(() => createTranslator(messages), [messages]);
  const value = useMemo<I18n>(
    () => ({
      language,
      info: languageInfo(language),
      selected,
      languages: languages.map(languageInfo),
      t,
      setLanguage: (code: LanguageCode) => {
        if (code === selected) return;
        writeLanguageCookie(code);
        // A signed-in visitor's choice is also their account's language (PATCH /api/auth/me, P7.11), which their
        // notifications and WhatsApp messages come in. Fire and forget: the cookie already holds the choice, and a
        // failure here never blocks the switch.
        if (queryClient.getQueryData<User | null>(["me"])) {
          api.patch<User>("/auth/me", { language: code }).then(
            (user) => queryClient.setQueryData(["me"], user),
            () => undefined,
          );
        }
        // The refresh re-renders the root layout and every server component in the chosen language (the html lang,
        // the font and these messages included) and keeps client state: no full reload.
        startTransition(() => {
          setSelected(code);
          router.refresh();
        });
      },
    }),
    [language, languages, selected, t, router, setSelected, queryClient],
  );
  // API errors with a known code are shown in this language (errorMessage in src/lib/api.ts, P7.10). Their text is in
  // the common area, which these messages always include.
  useEffect(() => {
    setErrorTranslator(t, language);
    return () => setErrorTranslator(null);
  }, [t, language]);
  return (
    <I18nMessagesContext value={messages}>
      <I18nContext value={value}>{children}</I18nContext>
    </I18nMessagesContext>
  );
}

const I18nMessagesContext = createContext<Partial<Messages>>({});

/** Adds message areas for a part of the site, from that route's server layout, so the browser gets a screen's
 * strings only on that screen's pages (Providers sends only the shell, CLIENT_AREAS in src/lib/i18n.ts):
 *   const { messages } = await requestI18n({ cookies, headers });
 *   return <I18nAreas messages={pickAreas(messages, ["discovery"])}>{children}</I18nAreas>;
 */
export function I18nAreas({ messages, children }: { messages: Partial<Messages>; children: React.ReactNode }) {
  const parent = useI18n();
  const parentMessages = use(I18nMessagesContext);
  // Memoised, like the provider's translator, so consumers of t and of the context see stable values across renders.
  const merged = useMemo(() => ({ ...parentMessages, ...messages }), [parentMessages, messages]);
  const value = useMemo(() => ({ ...parent, t: createTranslator(merged) }), [parent, merged]);
  return (
    <I18nMessagesContext value={merged}>
      <I18nContext value={value}>{children}</I18nContext>
    </I18nMessagesContext>
  );
}

// The toast viewport is not first-load JavaScript (P2.9): it mounts 2 s after the page has loaded, when the
// browser is idle. A toast raised before then is not lost: sonner replays active toasts to a new Toaster.
const Toaster = dynamic(() => import("@/components/ui/sonner").then((m) => m.Toaster), { ssr: false });

function IdleToaster() {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    // 2 s after the load event, when the main thread is idle: past the page's first paint and data on a slow phone.
    let timer: ReturnType<typeof setTimeout> | undefined;
    const idle = () =>
      "requestIdleCallback" in window ? requestIdleCallback(() => setReady(true), { timeout: 3000 }) : setReady(true);
    const start = () => {
      timer = setTimeout(idle, 2000);
    };
    if (document.readyState === "complete") start();
    else window.addEventListener("load", start, { once: true });
    return () => {
      window.removeEventListener("load", start);
      clearTimeout(timer);
    };
  }, []);
  return ready ? <Toaster position="top-center" richColors closeButton /> : null;
}

// ---------------------------------------------------------------- the move to phone sign-in (P4.2)

// Pages a signed-in account without a verified phone may still open after the sunset: the verify screen itself, and
// the grievance page (support, for a number already verified on another account) with the legal pages it links to.
// Signing out is a button on every page, and on /verify-phone.
const OPEN_AFTER_SUNSET = ["/verify-phone", "/grievance", "/privacy", "/terms"];
const DISMISS_KEY = "nearshop.phone-banner-dismissed";

function localDay() {
  const d = new Date();
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
}

function dismissedToday() {
  try {
    return typeof window !== "undefined" && window.localStorage.getItem(DISMISS_KEY) === localDay();
  } catch {
    return false; // storage blocked: the banner shows, and dismissing hides it until the page is reloaded
  }
}

/** /auth/me says what to ask of the signed-in account (phone_migration): "warn", from the day phone sign-in is
 * available until the sunset, shows a banner on every page with the sunset date and a link to /verify-phone, which can
 * be dismissed for the day and blocks nothing; "required", from the sunset, sends every page to /verify-phone. Admins
 * and accounts with a verified phone get null: nothing. */
function PhoneMigration() {
  const { data: me } = useMe();
  const { t, info } = useI18n();
  const pathname = usePathname();
  const router = useRouter();
  const [dismissed, setDismissed] = useState(dismissedToday);
  const state = me?.phone_migration ?? null;
  const open = OPEN_AFTER_SUNSET.some((p) => pathname === p || pathname.startsWith(`${p}/`));

  useEffect(() => {
    if (state === "required" && !open) router.replace(`/verify-phone?next=${encodeURIComponent(pathname)}`);
  }, [state, open, pathname, router]);

  if (state !== "warn" || dismissed || pathname.startsWith("/verify-phone") || !me) return null;
  const sunset = new Date(`${me.email_login_sunset}T00:00:00Z`).toLocaleDateString(info.htmlLang, {
    day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
  });
  return (
    <div role="region" aria-label={t("shell.phoneBanner.region")} className="border-b border-info/30 bg-info-soft text-info-ink">
      <div className="mx-auto flex max-w-7xl items-center gap-3 px-4 py-2 text-sm md:px-6">
        <Smartphone className="size-4 shrink-0" aria-hidden />
        <p className="flex-1">
          {t("shell.phoneBanner.text", { date: sunset })}{" "}
          <Link href="/verify-phone" className="font-medium underline underline-offset-4">{t("shell.phoneBanner.link")}</Link>
        </p>
        <button
          type="button"
          className="grid size-8 shrink-0 place-items-center rounded-md hover:bg-info/10"
          aria-label={t("shell.phoneBanner.dismiss")}
          onClick={() => {
            setDismissed(true);
            try {
              window.localStorage.setItem(DISMISS_KEY, localDay());
            } catch {
              // storage blocked: dismissed for this page view only
            }
          }}
        >
          <X className="size-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}

/** The location provider with the shell's translator (the I18nProvider's, memoised per set of messages), so its
 * location and context value keep their identity across renders of Providers. */
function ShellLocationProvider({ children }: { children: React.ReactNode }) {
  const { t } = useI18n();
  return <LocationProvider t={t}>{children}</LocationProvider>;
}

export function Providers({
  language,
  languages,
  messages,
  children,
}: {
  language: LanguageCode;
  /** The languages the switches offer. */
  languages: readonly LanguageCode[];
  /** The areas of the active language's messages that client components need on every page (the shell). */
  messages: Partial<Messages>;
  children: React.ReactNode;
}) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            refetchOnWindowFocus: false,
            retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
          },
        },
        mutationCache: new MutationCache({
          // Mutations show their own error unless they opt out with meta.silent.
          // sonner is loaded here, not imported: it is not first-load JavaScript (P2.9).
          onError: (error, _v, _c, mutation) => {
            if (!mutation.meta?.silent) void import("sonner").then(({ toast }) => toast.error(errorMessage(error)));
          },
        }),
      }),
  );

  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
      <I18nProvider language={language} languages={languages} messages={messages} queryClient={client}>
        <QueryClientProvider client={client}>
          {/* No MotionConfig and no TooltipProvider on every page (P2.9): shared screens animate with CSS, which
              globals.css stops for the reduced-motion setting. That rule does not reach motion's JavaScript
              animations, so each screen that still uses motion wraps itself in its own MotionConfig with
              reducedMotion "user"; a screen that uses a Radix tooltip wraps it in its own TooltipProvider. */}
          {/* usePathname inside, in a Suspense boundary, so pages stay prerendered (with English as the only
              offered language the root layout does not read the request; src/lib/i18n.ts). */}
          <Suspense fallback={null}>
            <PhoneMigration />
          </Suspense>
          <ShellLocationProvider>{children}</ShellLocationProvider>
          <IdleToaster />
        </QueryClientProvider>
      </I18nProvider>
    </ThemeProvider>
  );
}
