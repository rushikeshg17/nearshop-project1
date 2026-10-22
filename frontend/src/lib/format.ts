import { DEFAULT_LANGUAGE, type LanguageCode, languageInfo } from "@/lib/i18n";

// Numbers and rupee amounts use Indian grouping (12,34,567) and Latin digits in every interface language (P7.5):
// they are always formatted with en-IN. The kn-IN locale would group them the Western way (1,234,567) and
// abbreviate with the Kannada word for million, which is not how prices are written in Ballari.
const NUMBER_LOCALE = "en-IN";
const inr = new Intl.NumberFormat(NUMBER_LOCALE, { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const inrPrecise = new Intl.NumberFormat(NUMBER_LOCALE, { style: "currency", currency: "INR", maximumFractionDigits: 2 });
const compact = new Intl.NumberFormat(NUMBER_LOCALE, { notation: "compact", maximumFractionDigits: 1 });
const number = new Intl.NumberFormat(NUMBER_LOCALE);

export function formatPrice(value: number | null | undefined, precise = false) {
  if (value === null || value === undefined) return "";
  return (precise && value % 1 !== 0 ? inrPrecise : inr).format(value);
}

export function formatCompactPrice(value: number) {
  return value >= 100000 ? `₹${compact.format(value)}` : inr.format(value);
}

export function formatNumber(value: number) {
  return number.format(value);
}

export function formatDistance(km: number | null | undefined) {
  if (km === null || km === undefined) return "";
  if (km < 1) return `${Math.max(50, Math.round((km * 1000) / 10) * 10)} m`;
  return `${km < 10 ? km.toFixed(1) : Math.round(km)} km`;
}

/** Rough walking or riding time for local context, for a message in the interface language
 * (common.travel.walk or common.travel.ride, with {minutes}). Null when the distance is not known. */
export function travelTime(km: number | null | undefined): { mode: "walk" | "ride"; minutes: number } | null {
  if (km === null || km === undefined) return null;
  if (km <= 1.2) return { mode: "walk", minutes: Math.max(2, Math.round(km * 13)) };
  return { mode: "ride", minutes: Math.max(4, Math.round(km * 3.2 + 2)) };
}

/** Rough walking/riding estimate for local context, in English (screens not yet translated). */
export function travelHint(km: number | null | undefined) {
  const time = travelTime(km);
  if (!time) return "";
  return `${time.minutes} min ${time.mode}`;
}

const relativeFormats = new Map<LanguageCode, Intl.RelativeTimeFormat>();

function relativeFormat(language: LanguageCode) {
  let format = relativeFormats.get(language);
  if (!format) {
    format = new Intl.RelativeTimeFormat(languageInfo(language).htmlLang, { numeric: "auto" });
    relativeFormats.set(language, format);
  }
  return format;
}

/** "4 minutes ago", "yesterday": phrased in the interface language by Intl (its grammar for each language), with
 * Latin digits. English when no language is given. */
export function timeAgo(iso: string | null | undefined, now: number = Date.now(), language: LanguageCode = DEFAULT_LANGUAGE) {
  if (!iso) return "never";
  const rtf = relativeFormat(language);
  const diff = (new Date(iso).getTime() - now) / 1000;
  const abs = Math.abs(diff);
  if (abs < 45) return language === DEFAULT_LANGUAGE ? "just now" : rtf.format(0, "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  if (abs < 86400 * 30) return rtf.format(Math.round(diff / 86400), "day");
  return new Date(iso).toLocaleDateString(languageInfo(language).htmlLang, { day: "numeric", month: "short" });
}

const WEEKDAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];

/** A weekday the owner wrote in English ("Monday", as shops store closed_on), named in the interface language.
 * Anything else is returned as written. */
export function weekdayName(day: string, language: LanguageCode = DEFAULT_LANGUAGE) {
  const index = WEEKDAYS.indexOf(day.trim().toLowerCase());
  if (index < 0 || language === DEFAULT_LANGUAGE) return day;
  // 4 Jan 1970 was a Sunday.
  return new Date(Date.UTC(1970, 0, 4 + index)).toLocaleDateString(languageInfo(language).htmlLang, {
    weekday: "long",
    timeZone: "UTC",
  });
}

/** Dates and times are written in the interface language (month names, AM and PM); English when not given. */
export function formatDateTime(iso: string | null | undefined, language: LanguageCode = DEFAULT_LANGUAGE) {
  if (!iso) return "";
  return new Date(iso).toLocaleString(languageInfo(language).htmlLang, {
    day: "numeric",
    month: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function formatTime(iso: string | null | undefined, language: LanguageCode = DEFAULT_LANGUAGE) {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString(languageInfo(language).htmlLang, { hour: "numeric", minute: "2-digit" });
}

/** "In stock, updated 2 minutes ago" style freshness text. */
export function freshness(iso: string | null | undefined) {
  if (!iso) return "Stock not yet confirmed";
  return `Updated ${timeAgo(iso)}`;
}

export function freshnessLevel(iso: string | null | undefined): "fresh" | "recent" | "stale" {
  if (!iso) return "stale";
  const hours = (Date.now() - new Date(iso).getTime()) / 3.6e6;
  if (hours <= 6) return "fresh";
  if (hours <= 48) return "recent";
  return "stale";
}

export function pluralize(n: number, one: string, many = `${one}s`) {
  return `${formatNumber(n)} ${n === 1 ? one : many}`;
}

export function initials(name: string) {
  return name
    .split(" ")
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]?.toUpperCase())
    .join("");
}
