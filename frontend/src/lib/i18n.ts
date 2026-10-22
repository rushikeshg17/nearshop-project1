import { createElement, Fragment, type ReactNode } from "react";

import type englishMessages from "@/messages/en.json";

// The interface language layer (P7.5, plan section 8.16).
//
// One URL per page, in every language: there is no locale segment in the path. The language is the visitor's
// choice in the nearshop_lang cookie when it names an offered language; otherwise the browser's first
// Accept-Language entry, when that is an offered language; otherwise English. Crawlers send neither, so they
// see English (plan section 8.14).
//
// Offered, not merely registered (plan section 8.16: a language appears only after its strings exist and a native
// speaker has reviewed them). A registered language is offered when its entry says reviewed, which is set only once
// its text for every screen (P7.6 to P7.10) has been reviewed, or when its code is in
// NEXT_PUBLIC_NEARSHOP_PREVIEW_LANGUAGES at build time (comma-separated, unset by default: for development, tests
// and review builds). Until then the switch does not show it, Accept-Language and the cookie never pick it, and the
// page is never labelled with a language whose text it does not show. A reviewed language with a missing key is
// not served at all: English is, with an error in the server log; only a preview language falls back per key.
//
// While English is the only language offered (the default build), the language is not read from the request at
// all, so pages stay static and CDN-cacheable exactly as before P7.5. Offering a second language makes every
// page render per request (the cookie and Accept-Language are request data, and there is no locale segment): a
// production change that needs sign-off when the first language is switched on (see next.config.ts on caching).
//
// Strings live in src/messages/<code>.json, keyed by area ("shell", then one area per group of screens). English
// is complete; another language's file has the same keys, and a key that is absent or empty there falls back to
// the English text (logged as a warning in development).
//
// This module is shared by server and client code, so it imports neither next/headers nor React hooks:
// - server components and the root layout call requestI18n with the cookies and headers functions from
//   next/headers, and get the language, its messages and a translator;
// - client components call useI18n (src/components/providers.tsx). The root layout sends the browser only the
//   areas in CLIENT_AREAS (the shell and the shared components); a route's layout that has client components of its own adds its area for
//   its pages only with I18nAreas (providers.tsx), so no page carries every screen's strings. No message file is
//   ever part of the browser's JavaScript.
//
// Adding a language (Telugu or Hindi, P8.4) needs only its message file, a font entry in src/app/layout.tsx when
// its script needs one, and a line in LANGUAGES below (reviewed: false until its text has been reviewed).

export type Messages = typeof englishMessages;

type Leaves<T, P extends string = ""> = {
  [K in keyof T & string]: T[K] extends string ? `${P}${K}` : Leaves<T[K], `${P}${K}.`>;
}[keyof T & string];

/** Every message key, as a dotted path: "shell.nav.signIn". */
export type MessageKey = Leaves<Messages>;

export type Translate = (key: MessageKey, values?: Record<string, string | number>) => string;

/** A web font the root layout loads for a script that the body font does not cover. */
export type ScriptFont = "kannada";

type MessageModule = { default: unknown };

export type Language = {
  /** The cookie value and the code the code base uses. */
  code: string;
  /** The language's name in its own script: what the language switch shows. */
  name: string;
  /** The html lang attribute, and the locale for dates and words in Intl formatting. */
  htmlLang: string;
  /** The extra font its script needs, or null when the body font covers it. */
  font: ScriptFont | null;
  /** Its text for every screen exists and a native speaker has reviewed it (plan section 8.16): only then is it
   * offered to everyone. Until then only a build that lists it in NEXT_PUBLIC_NEARSHOP_PREVIEW_LANGUAGES offers it. */
  reviewed: boolean;
  /** Its message file, loaded on the server only. */
  load: () => Promise<MessageModule>;
};

export const LANGUAGES = [
  { code: "en", name: "English", htmlLang: "en-IN", font: null, reviewed: true, load: () => import("@/messages/en.json") },
  // Not reviewed: the Kannada text (B16) has not arrived, so kn.json is empty and Kannada is a preview language.
  {
    code: "kn",
    name: "ಕನ್ನಡ",
    htmlLang: "kn-IN",
    font: "kannada",
    reviewed: false,
    load: () => import("@/messages/kn.json"),
  },
] as const satisfies readonly Language[];

export type LanguageCode = (typeof LANGUAGES)[number]["code"];

export const DEFAULT_LANGUAGE: LanguageCode = "en";
export const LANGUAGE_COOKIE = "nearshop_lang";
const COOKIE_MAX_AGE_SECONDS = 365 * 24 * 60 * 60;

export function isLanguage(value: string | null | undefined): value is LanguageCode {
  return LANGUAGES.some((l) => l.code === value);
}

// Read literally, so the build inlines it in browser code too: the server and the switches agree.
const PREVIEW_LANGUAGES = (process.env.NEXT_PUBLIC_NEARSHOP_PREVIEW_LANGUAGES ?? "")
  .split(",")
  .map((code) => code.trim().toLowerCase())
  .filter(Boolean);

function isPreview(code: LanguageCode): boolean {
  return code !== DEFAULT_LANGUAGE && PREVIEW_LANGUAGES.includes(code);
}

/** The languages this build offers: the reviewed ones and the preview ones, English first. */
export const OFFERED_LANGUAGES: readonly Language[] = LANGUAGES.filter((l) => l.reviewed || isPreview(l.code));

/** An offered language's code (see the top of this file). */
export function isOffered(value: string | null | undefined): value is LanguageCode {
  return OFFERED_LANGUAGES.some((l) => l.code === value);
}

/** True when the build offers only English: the language is then not read from the request. */
export const SINGLE_LANGUAGE = OFFERED_LANGUAGES.length < 2;

export function languageInfo(code: LanguageCode): Language {
  return LANGUAGES.find((l) => l.code === code) ?? LANGUAGES[0];
}

/** The first language in an Accept-Language header (highest q, ties in the order listed), as a lower-case
 * primary subtag: "kn-IN,kn;q=0.9,en;q=0.8" gives "kn". Null when the header is empty or names only "*". */
export function firstAcceptedLanguage(header: string | null | undefined): string | null {
  if (!header) return null;
  let best: { tag: string; q: number } | null = null;
  for (const part of header.split(",")) {
    const [range, ...params] = part.trim().split(";");
    const tag = range.trim().toLowerCase();
    if (!tag || tag === "*") continue;
    let q = 1;
    for (const p of params) {
      const [name, value] = p.trim().split("=");
      if (name?.trim().toLowerCase() === "q") q = Number(value);
    }
    if (!Number.isFinite(q) || q <= 0) continue;
    if (!best || q > best.q) best = { tag: tag.split("-")[0], q };
  }
  return best?.tag ?? null;
}

/** The language for a request: the nearshop_lang cookie when it names an offered language, else the browser's
 * first Accept-Language entry when that is offered (Kannada only if listed first), else English. */
export function resolveLanguage(cookieValue: string | null | undefined, acceptLanguage: string | null | undefined): LanguageCode {
  if (isOffered(cookieValue)) return cookieValue;
  const accepted = firstAcceptedLanguage(acceptLanguage);
  return isOffered(accepted) ? accepted : DEFAULT_LANGUAGE;
}

// ------------------------------------------------------------------------------------------------ messages

type Tree = { [key: string]: string | Tree };

function isTree(value: unknown): value is Tree {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** English's shape with the other language's text where it has some, and English where it has none. */
function withFallback(english: Tree, own: unknown, prefix: string, missing: string[]): Tree {
  const result: Tree = {};
  for (const [key, value] of Object.entries(english)) {
    const theirs = isTree(own) ? own[key] : undefined;
    if (typeof value === "string") {
      if (typeof theirs === "string" && theirs.trim()) result[key] = theirs;
      else {
        result[key] = value;
        missing.push(prefix + key);
      }
    } else {
      result[key] = withFallback(value, theirs, `${prefix}${key}.`, missing);
    }
  }
  return result;
}

type Loaded = { messages: Messages; missing: readonly string[] };

const loaded = new Map<LanguageCode, Promise<Loaded>>();

async function build(code: LanguageCode): Promise<Loaded> {
  const english = (await languageInfo(DEFAULT_LANGUAGE).load()).default as Tree;
  if (code === DEFAULT_LANGUAGE) return { messages: english as Messages, missing: [] };
  const missing: string[] = [];
  const merged = withFallback(english, (await languageInfo(code).load()).default, "", missing);
  if (missing.length && languageInfo(code).reviewed) {
    // A reviewed language must be complete: it is not served (requestI18n), and this is a defect to fix.
    console.error(`[i18n] ${code} is marked reviewed but ${missing.length} keys have no text: ${missing.join(", ")}`);
  } else if (missing.length && process.env.NODE_ENV === "development") {
    console.warn(
      `[i18n] ${code}: ${missing.length} message keys have no ${code} text and show English: ${missing.join(", ")}`,
    );
  }
  return { messages: merged as Messages, missing };
}

function loadLanguage(code: LanguageCode): Promise<Loaded> {
  let entry = loaded.get(code);
  if (!entry) {
    entry = build(code);
    loaded.set(code, entry);
  }
  return entry;
}

/** A language's messages, complete: every key English has, in that language where its file has the text. */
export async function loadMessages(code: LanguageCode): Promise<Messages> {
  return (await loadLanguage(code)).messages;
}

/** Whether a language may be served: English always; a preview language always (falling back per key); a
 * reviewed one only when its file has every key, so a page is never labelled Kannada while showing English. */
async function servable(code: LanguageCode): Promise<boolean> {
  if (code === DEFAULT_LANGUAGE || isPreview(code)) return true;
  return isOffered(code) && (await loadLanguage(code)).missing.length === 0;
}

/** The languages the switches show, for the root layout to pass to Providers: offered and servable. */
export async function servableLanguages(): Promise<LanguageCode[]> {
  const ok = await Promise.all(OFFERED_LANGUAGES.map(async (l) => ((await servable(l.code as LanguageCode)) ? l.code : null)));
  return ok.filter((code): code is LanguageCode => code !== null);
}

// ------------------------------------------------------------------------------------------------ client areas

/** The message areas every page sends to the browser: the site shell's client components, and "common", the
 * shared components used on every part of the site (the search box in the header, the empty, error and loading
 * states). Each screen area (P7.6 to P7.10) reaches the browser only on its own pages, through I18nAreas in that
 * route's layout: "discovery" from src/app/(site)/layout.tsx. */
export const CLIENT_AREAS = ["shell", "common"] as const satisfies readonly (keyof Messages)[];

/** Only the named areas of a message tree, for passing to client components. */
export function pickAreas<K extends keyof Messages>(messages: Messages, areas: readonly K[]): Pick<Messages, K> {
  const result = {} as Pick<Messages, K>;
  for (const area of areas) result[area] = messages[area];
  return result;
}

/** t(key, values): the message, with each {name} replaced by values.name. A key that is not in the messages (a
 * typo the types did not catch) shows the key itself, with a warning in development. */
export function createTranslator(messages: Partial<Messages>): Translate {
  return (key, values) => {
    let node: unknown = messages;
    for (const part of key.split(".")) node = isTree(node) ? node[part] : undefined;
    if (typeof node !== "string") {
      if (process.env.NODE_ENV === "development") console.warn(`[i18n] no message for ${key}`);
      return key;
    }
    if (!values) return node;
    return node.replace(/\{(\w+)\}/g, (match, name: string) => (name in values ? String(values[name]) : match));
  };
}

/** The plural form of a count in a language, as CLDR defines it, reduced to the two forms the message files use:
 * a counted message is a pair of keys, "one" and "other", each with {count}, and the caller picks one:
 *   t(`discovery.search.items.${plural(language, n)}`, { count: formatNumber(n) })
 * English says "1 item" but "0 items"; Kannada uses its "one" form for 0 and 1. */
export function plural(language: LanguageCode, n: number): "one" | "other" {
  return new Intl.PluralRules(languageInfo(language).htmlLang).select(n) === "one" ? "one" : "other";
}

/** A message with elements inside it, such as a link or bold text: each {name} in the text that parts names is
 * replaced by that element, wherever the language puts it ("Did you mean {suggestion}?"). Placeholders t has
 * already filled, and any not in parts, stay as they are. */
export function richText(text: string, parts: Record<string, ReactNode>): ReactNode[] {
  return text
    .split(/(\{\w+\})/)
    .filter(Boolean)
    .map((piece, i) => {
      const name = /^\{(\w+)\}$/.exec(piece)?.[1];
      return createElement(Fragment, { key: i }, name !== undefined && name in parts ? parts[name] : piece);
    });
}

// ------------------------------------------------------------------------------------------------ requests

type CookieStore = { get(name: string): { value: string } | undefined };
type HeaderStore = { get(name: string): string | null };

/** For server components and the root layout: pass cookies and headers from next/headers.
 *   const { t } = await requestI18n({ cookies, headers });
 * While the build offers only English they are not called, and the page stays static. Once a second language is
 * offered, reading them makes the page render per request, which the language then needs. */
export async function requestI18n(request: {
  cookies: () => Promise<CookieStore>;
  headers: () => Promise<HeaderStore>;
}): Promise<{ language: LanguageCode; messages: Messages; t: Translate }> {
  let language: LanguageCode = DEFAULT_LANGUAGE;
  if (!SINGLE_LANGUAGE) {
    const [cookieStore, headerStore] = await Promise.all([request.cookies(), request.headers()]);
    language = resolveLanguage(cookieStore.get(LANGUAGE_COOKIE)?.value, headerStore.get("accept-language"));
    if (!(await servable(language))) language = DEFAULT_LANGUAGE;
  }
  const messages = await loadMessages(language);
  return { language, messages, t: createTranslator(messages) };
}

/** In the browser: remember the visitor's choice for a year, on every path. */
export function writeLanguageCookie(code: LanguageCode) {
  const secure = window.location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `${LANGUAGE_COOKIE}=${code}; Path=/; Max-Age=${COOKIE_MAX_AGE_SECONDS}; SameSite=Lax${secure}`;
}
