"use client";

import { MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Smartphone, X } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ThemeProvider } from "next-themes";
import { createContext, Suspense, use, useEffect, useOptimistic, useState, useTransition } from "react";

import { useMe } from "@/hooks/use-session";
import { ApiError, errorMessage } from "@/lib/api";
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

// ---------------------------------------------------------------- the interface language (P7.5, src/lib/i18n.ts)

export type I18n = {
  /** The language the page is rendered in. */
  language: LanguageCode;
  /** Its registry entry: name, html lang (also the Intl locale) and font. */
  info: Language;
  /** The language the visitor last chose: shown by the switches while the page re-renders in it. */
  selected: LanguageCode;
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

/** The script font of the active language (src/app/layout.tsx), kept off the page's load entirely. Until it is
 * used, the script's characters show in the phone's own font for it (every Android phone has a Kannada one, itself
 * Noto Sans Kannada), so text is never blocked or missing. The visitor's first tap or key press downloads it
 * without using it, and the first one after it has arrived adds it to the body font's family list, in a task of its
 * own so the tap's own response paints first. It is removed when the language changes.
 *
 * Why not on load (P2.9 budgets, which must pass in every language, plan section 8.16): the lab counts every
 * request that finishes before the largest contentful paint, so the font named in the first paint added 150 ms
 * (28 KB) to 450 ms (the 91 KB variable file) to the lab LCP of the budget pages. And any web font that finishes
 * loading after that paint makes Chrome lay all text out again: on the search page the result titles re-wrap and
 * the lab takes that as a new, late largest paint (seconds later). After an input, the largest paint is final, and
 * a layout shift within 0.5 s of it does not count. */
function useScriptFont(fontStack: string | null, sample: string) {
  useEffect(() => {
    if (!fontStack) return;
    const root = document.documentElement;
    let state: "idle" | "loading" | "loaded" = "idle";
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const stop = () => {
      window.removeEventListener("pointerdown", onInput, true);
      window.removeEventListener("keydown", onInput, true);
    };
    function onInput() {
      if (state === "loaded") {
        stop();
        timer = setTimeout(() => root.style.setProperty("--font-geist-sans", fontStack), 0);
      } else if (state === "idle") {
        state = "loading";
        document.fonts.load(`1em ${fontStack}`, sample).then(
          () => {
            if (!cancelled) state = "loaded";
          },
          () => {
            if (!cancelled) state = "idle"; // not downloaded (offline): the phone's own font stays; try again later
          },
        );
      }
    }
    window.addEventListener("pointerdown", onInput, true);
    window.addEventListener("keydown", onInput, true);
    return () => {
      cancelled = true;
      stop();
      clearTimeout(timer);
      root.style.removeProperty("--font-geist-sans");
    };
  }, [fontStack, sample]);
}

function I18nProvider({
  language,
  messages,
  fontStack,
  children,
}: {
  language: LanguageCode;
  messages: Messages;
  fontStack: string | null;
  children: React.ReactNode;
}) {
  const info = languageInfo(language);
  // The language's own name is in its script: enough text to fetch that script's part of the font.
  useScriptFont(fontStack, info.name);
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [selected, setSelected] = useOptimistic(language);
  const setLanguage = (code: LanguageCode) => {
    if (code === selected) return;
    writeLanguageCookie(code);
    // The refresh re-renders the root layout and every server component in the chosen language (the html lang,
    // the font and these messages included) and keeps client state: no full reload.
    startTransition(() => {
      setSelected(code);
      router.refresh();
    });
  };
  const value: I18n = { language, info, selected, t: createTranslator(messages), setLanguage };
  return <I18nContext value={value}>{children}</I18nContext>;
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
  const pathname = usePathname();
  const router = useRouter();
  const [dismissed, setDismissed] = useState(dismissedToday);
  const state = me?.phone_migration ?? null;
  const open = OPEN_AFTER_SUNSET.some((p) => pathname === p || pathname.startsWith(`${p}/`));

  useEffect(() => {
    if (state === "required" && !open) router.replace(`/verify-phone?next=${encodeURIComponent(pathname)}`);
  }, [state, open, pathname, router]);

  if (state !== "warn" || dismissed || pathname.startsWith("/verify-phone") || !me) return null;
  const sunset = new Date(`${me.email_login_sunset}T00:00:00Z`).toLocaleDateString("en-IN", {
    day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
  });
  return (
    <div role="region" aria-label="Verify your phone number" className="border-b border-info/30 bg-info-soft text-info-ink">
      <div className="mx-auto flex max-w-7xl items-center gap-3 px-4 py-2 text-sm md:px-6">
        <Smartphone className="size-4 shrink-0" aria-hidden />
        <p className="flex-1">
          From {sunset} you sign in with a code sent to your phone.{" "}
          <Link href="/verify-phone" className="font-medium underline underline-offset-4">Verify your phone number</Link>
        </p>
        <button
          type="button"
          className="grid size-8 shrink-0 place-items-center rounded-md hover:bg-info/10"
          aria-label="Dismiss for today"
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

export function Providers({
  language,
  messages,
  fontStack,
  children,
}: {
  language: LanguageCode;
  messages: Messages;
  /** The body font's family list with the active language's script font, or null when the body font covers it. */
  fontStack: string | null;
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
      <I18nProvider language={language} messages={messages} fontStack={fontStack}>
        <QueryClientProvider client={client}>
          {/* No MotionConfig and no TooltipProvider on every page (P2.9): shared screens animate with CSS, which
              globals.css stops for the reduced-motion setting. That rule does not reach motion's JavaScript
              animations, so each screen that still uses motion wraps itself in its own MotionConfig with
              reducedMotion "user"; a screen that uses a Radix tooltip wraps it in its own TooltipProvider. */}
          {/* usePathname inside, in a Suspense boundary (it kept pages prerendered before P7.5 made the root
              layout render per request, for the language). */}
          <Suspense fallback={null}>
            <PhoneMigration />
          </Suspense>
          <LocationProvider>{children}</LocationProvider>
          <IdleToaster />
        </QueryClientProvider>
      </I18nProvider>
    </ThemeProvider>
  );
}
