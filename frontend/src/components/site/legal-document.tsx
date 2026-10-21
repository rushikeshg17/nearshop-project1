import fs from "node:fs";
import path from "node:path";

import type { ReactNode } from "react";

/**
 * The privacy notice and terms pages (P3.1) render the lawyer-reviewed text from docs/legal at build time.
 *
 * File names: docs/legal/<doc>-v<N>.md is the English text of version vN, and docs/legal/<doc>-v<N>.<lang>.md
 * the same version in another language, by its ISO 639-1 code (privacy-v1.kn.md for Kannada; P8.4 adds .te
 * and .hi). The newest version present is shown, with every language file of that version; no page change is
 * needed for a new language or version. Each file states its effective date on a line of its own, for example
 * "Effective date: 2027-01-17". Until the files exist the page shows a marked draft notice instead.
 * Rendered markdown: headings, paragraphs, bulleted and numbered lists, **bold** and [links](https://...).
 */

export type LegalDoc = "privacy" | "terms";

export type LegalVersion = { lang: string; text: string; effective: string | null };

export type LegalText = { version: string; languages: LegalVersion[] } | null;

const LEGAL_DIR = path.join(process.cwd(), "..", "docs", "legal");

const LANGUAGE_NAMES: Record<string, string> = {
  en: "English",
  kn: "ಕನ್ನಡ (Kannada)",
  te: "తెలుగు (Telugu)",
  hi: "हिन्दी (Hindi)",
};

const EFFECTIVE = /^\s*\**effective(?: date)?\**\s*:\s*\**\s*(.+?)\s*\**\s*$/im;

/** The newest version of `doc` in docs/legal, with each language present, or null when there is none. */
export function loadLegal(doc: LegalDoc): LegalText {
  let names: string[];
  try {
    names = fs.readdirSync(LEGAL_DIR);
  } catch {
    return null;
  }
  const pattern = new RegExp(`^${doc}-v(\\d+)(?:\\.([a-z]{2,3}))?\\.md$`);
  const files = names
    .map((name) => ({ name, match: pattern.exec(name) }))
    .filter((f): f is { name: string; match: RegExpExecArray } => f.match !== null)
    .map(({ name, match }) => ({ name, version: Number(match[1]), lang: match[2] ?? "en" }));
  if (files.length === 0) return null;
  const newest = Math.max(...files.map((f) => f.version));
  const languages = files
    .filter((f) => f.version === newest)
    .sort((a, b) => (a.lang === "en" ? -1 : b.lang === "en" ? 1 : a.lang.localeCompare(b.lang)))
    .map(({ name, lang }) => {
      const text = fs.readFileSync(path.join(LEGAL_DIR, name), "utf-8");
      return { lang, text, effective: EFFECTIVE.exec(text)?.[1] ?? null };
    });
  return { version: `v${newest}`, languages };
}

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /\*\*(.+?)\*\*|\[([^\]]+)\]\(((?:https?:\/\/|\/|mailto:)[^)\s]+)\)/g;
  let last = 0;
  let n = 0;
  for (let m = pattern.exec(text); m; m = pattern.exec(text)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] !== undefined) out.push(<strong key={`${key}-${n++}`}>{m[1]}</strong>);
    else out.push(<a key={`${key}-${n++}`} href={m[3]} className="text-foreground underline underline-offset-4">{m[2]}</a>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** A small markdown subset, enough for the legal texts, rendered without HTML passthrough. */
function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const key = `b${i}`;
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (!line.trim()) {
      i++;
    } else if (heading) {
      const level = heading[1].length;
      const cls = level <= 2 ? "mt-8 text-xl font-semibold" : "mt-6 text-lg font-semibold";
      blocks.push(level <= 1 ? <h2 key={key} className={cls}>{inline(heading[2], key)}</h2>
        : level === 2 ? <h3 key={key} className={cls}>{inline(heading[2], key)}</h3>
        : <h4 key={key} className={cls}>{inline(heading[2], key)}</h4>);
      i++;
    } else if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, ""));
      const children = items.map((item, j) => <li key={`${key}-${j}`}>{inline(item, `${key}-${j}`)}</li>);
      blocks.push(ordered ? <ol key={key} className="mt-3 list-decimal space-y-1 pl-6">{children}</ol>
        : <ul key={key} className="mt-3 list-disc space-y-1 pl-6">{children}</ul>);
    } else {
      const para: string[] = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,4})\s|^\s*([-*]|\d+\.)\s+/.test(lines[i])) para.push(lines[i++].trim());
      blocks.push(<p key={key} className="mt-3">{inline(para.join(" "), key)}</p>);
    }
  }
  return <>{blocks}</>;
}

/** The page body for /privacy or /terms: the reviewed text, or the draft notice until it is published. */
export function LegalDocument({ doc, title, legal }: { doc: LegalDoc; title: string; legal: LegalText }) {
  const id = `legal-${doc}`;
  if (!legal) {
    return (
      <article className="mx-auto max-w-3xl px-4 py-12 md:px-6">
        <h1 className="text-3xl font-semibold">{title}</h1>
        <div role="note" className="mt-6 rounded-xl border border-warning/40 bg-warning-soft p-5 text-warning-ink">
          <p className="font-semibold">Draft: the reviewed text is not yet published</p>
          <p className="mt-2 text-sm">
            The lawyer-reviewed {doc === "privacy" ? "privacy notice" : "terms of use"} in English and Kannada will
            appear here, with its version and effective date, before NearShop opens sign-ups. Until then this page
            has no binding text.
          </p>
        </div>
      </article>
    );
  }
  const multi = legal.languages.length > 1;
  const css = multi
    ? `@supports selector(:has(*)){#${id} [data-legal-lang]{display:none}` +
      legal.languages.map(({ lang }) => `#${id}:has(#${id}-${lang}:checked) [data-legal-lang="${lang}"]{display:block}`).join("") +
      "}"
    : "";
  return (
    <article id={id} className="mx-auto max-w-3xl px-4 py-12 md:px-6">
      {multi && <style>{css}</style>}
      <h1 className="text-3xl font-semibold">{title}</h1>
      {multi && (
        <div role="radiogroup" aria-label="Language" className="mt-5 flex flex-wrap gap-2">
          {legal.languages.map(({ lang }, i) => (
            <label key={lang} className="cursor-pointer rounded-full border px-3 py-1.5 text-sm has-checked:border-primary has-checked:bg-primary has-checked:text-primary-foreground has-focus-visible:ring-3 has-focus-visible:ring-ring/50">
              <input type="radio" name={`${id}-lang`} id={`${id}-${lang}`} defaultChecked={i === 0} className="sr-only" />
              {LANGUAGE_NAMES[lang] ?? lang}
            </label>
          ))}
        </div>
      )}
      {legal.languages.map(({ lang, text, effective }) => (
        <section key={lang} lang={lang === "en" ? "en-IN" : lang} data-legal-lang={lang} className="mt-6">
          {multi && <h2 className="sr-only">{LANGUAGE_NAMES[lang] ?? lang}</h2>}
          <p className="text-sm text-muted-foreground">
            Version {legal.version} · Effective {effective ?? "date not stated"}
          </p>
          <div className="text-[15px] leading-relaxed">
            <Markdown text={text} />
          </div>
        </section>
      ))}
    </article>
  );
}
