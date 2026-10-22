import type englishMessages from "@/messages/en.json";

// The interface language layer (P7.5, plan section 8.16).
//
// One URL per page, in every language: there is no locale segment in the path. The language is the visitor's
// choice in the nearshop_lang cookie when it names a registered language; otherwise the browser's first
// Accept-Language entry, when that is a registered language; otherwise English. Crawlers send neither, so they
// see English (plan section 8.14).
//
// Strings live in src/messages/<code>.json, keyed by area ("shell", then one area per group of screens). English
// is complete; another language's file has the same keys, and a key that is absent or empty there falls back to
// the English text (logged as a warning in development).
//
// This module is shared by server and client code, so it imports neither next/headers nor React hooks:
// - server components and the root layout call requestI18n with the cookies and headers functions from
//   next/headers, and get the language, its messages and a translator;
// - client components call useI18n (src/components/providers.tsx), whose messages the root layout passes down
//   once per request. No message file is ever part of the browser's JavaScript.
//
// Adding a language (Telugu or Hindi, P8.4) needs only its message file, a font entry in src/app/layout.tsx when
// its script needs one, and a line in LANGUAGES below.

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
  /** Its message file, loaded on the server only. */
  load: () => Promise<MessageModule>;
};

export const LANGUAGES = [
  { code: "en", name: "English", htmlLang: "en-IN", font: null, load: () => import("@/messages/en.json") },
  { code: "kn", name: "ಕನ್ನಡ", htmlLang: "kn-IN", font: "kannada", load: () => import("@/messages/kn.json") },
] as const satisfies readonly Language[];

export type LanguageCode = (typeof LANGUAGES)[number]["code"];

export const DEFAULT_LANGUAGE: LanguageCode = "en";
export const LANGUAGE_COOKIE = "nearshop_lang";
const COOKIE_MAX_AGE_SECONDS = 365 * 24 * 60 * 60;

export function isLanguage(value: string | null | undefined): value is LanguageCode {
  return LANGUAGES.some((l) => l.code === value);
}

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

/** The language for a request: the nearshop_lang cookie when it names a registered language, else the browser's
 * first Accept-Language entry when that is registered (Kannada only if listed first), else English. */
export function resolveLanguage(cookieValue: string | null | undefined, acceptLanguage: string | null | undefined): LanguageCode {
  if (isLanguage(cookieValue)) return cookieValue;
  const accepted = firstAcceptedLanguage(acceptLanguage);
  return isLanguage(accepted) ? accepted : DEFAULT_LANGUAGE;
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

const loaded = new Map<LanguageCode, Promise<Messages>>();

async function build(code: LanguageCode): Promise<Messages> {
  const english = (await languageInfo(DEFAULT_LANGUAGE).load()).default as Tree;
  if (code === DEFAULT_LANGUAGE) return english as Messages;
  const missing: string[] = [];
  const merged = withFallback(english, (await languageInfo(code).load()).default, "", missing);
  if (missing.length && process.env.NODE_ENV === "development") {
    console.warn(
      `[i18n] ${code}: ${missing.length} message keys have no ${code} text and show English: ${missing.join(", ")}`,
    );
  }
  return merged as Messages;
}

/** A language's messages, complete: every key English has, in that language where its file has the text. */
export function loadMessages(code: LanguageCode): Promise<Messages> {
  let messages = loaded.get(code);
  if (!messages) {
    messages = build(code);
    loaded.set(code, messages);
  }
  return messages;
}

/** t(key, values): the message, with each {name} replaced by values.name. A key that is not in the messages (a
 * typo the types did not catch) shows the key itself, with a warning in development. */
export function createTranslator(messages: Messages): Translate {
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

// ------------------------------------------------------------------------------------------------ requests

type CookieStore = { get(name: string): { value: string } | undefined };
type HeaderStore = { get(name: string): string | null };

/** For server components and the root layout: pass cookies and headers from next/headers.
 *   const { t } = await requestI18n({ cookies, headers });
 * Reading them makes the page render per request, which the language needs. */
export async function requestI18n(request: {
  cookies: () => Promise<CookieStore>;
  headers: () => Promise<HeaderStore>;
}): Promise<{ language: LanguageCode; messages: Messages; t: Translate }> {
  const [cookieStore, headerStore] = await Promise.all([request.cookies(), request.headers()]);
  const language = resolveLanguage(cookieStore.get(LANGUAGE_COOKIE)?.value, headerStore.get("accept-language"));
  const messages = await loadMessages(language);
  return { language, messages, t: createTranslator(messages) };
}

/** In the browser: remember the visitor's choice for a year, on every path. */
export function writeLanguageCookie(code: LanguageCode) {
  const secure = window.location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `${LANGUAGE_COOKIE}=${code}; Path=/; Max-Age=${COOKIE_MAX_AGE_SECONDS}; SameSite=Lax${secure}`;
}
