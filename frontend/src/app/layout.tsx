import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono, Noto_Sans_Kannada } from "next/font/google";
import { cookies, headers } from "next/headers";

import { Providers } from "@/components/providers";
import { CLIENT_AREAS, languageInfo, pickAreas, requestI18n, type ScriptFont, servableLanguages } from "@/lib/i18n";

import "./globals.css";

// One font on the critical path (P2.9): the body face, preloaded. The mono face (reservation and order codes)
// is declared but not preloaded, so the browser fetches it only on a page that shows a code. The display face
// that headings used (Bricolage Grotesque, 41 KB) is dropped: it was fetched on every page and, on slow 4G,
// competed with the page's own scripts and data; headings use the body face (globals.css, --font-heading).
const sans = Geist({ variable: "--font-geist-sans", subsets: ["latin"], display: "swap" });
const mono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"], preload: false });

// Fonts for scripts the body face does not cover (P7.5, plan section 8.16), one entry per ScriptFont in
// src/lib/i18n.ts. Each loads only its own script's subset and is never preloaded. It is added to the body font's
// family list on the html element from the first paint, only while its language is active: in any other language
// the page never names it, so the browser never fetches it. Latin text keeps the body face; the script's
// characters, which the body face lacks, fall through to this one.
// display "optional", not "swap": the text is never held back for it (the browser waits at most about 100 ms, then
// paints with the device's own font for the script; every Android phone, Windows and macOS have a Kannada one).
// A font that arrives later is kept for the next page load instead of being swapped in, so the page is never laid
// out again mid-visit, and a late swap can never become a new, late largest contentful paint (P2.9 budgets).
// One static weight, not the variable font: the Kannada subset is 28 KB at 400, against 91 KB for the variable
// file. Medium text uses it as it is; semibold and bold are synthesised.
const kannada = Noto_Sans_Kannada({ subsets: ["kannada"], weight: "400", display: "optional", preload: false });
const SCRIPT_FONTS: Record<ScriptFont, { style: { fontFamily: string } }> = { kannada };

export const metadata: Metadata = {
  title: { default: "NearShop: find it nearby", template: "%s · NearShop" },
  description:
    "See which shops near you have what you need in stock right now. Compare prices, reserve for pickup, or get it delivered by the shop.",
  applicationName: "NearShop",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fbfaf8" },
    { media: "(prefers-color-scheme: dark)", color: "#141210" },
  ],
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  // The interface language (src/lib/i18n.ts): the nearshop_lang cookie, else the browser's first language, else
  // English, among the languages this build offers. With English alone (the default) the request is not read and
  // pages stay static. The client components get the shell's messages from Providers.
  const [{ language, messages }, languages] = await Promise.all([
    requestI18n({ cookies, headers }),
    servableLanguages(),
  ]);
  const { htmlLang, font } = languageInfo(language);
  // The body face first (Latin text), then the script's own face, as --font-geist-sans, which feeds --font-sans and
  // --font-heading (globals.css). Inline, so it overrides the value from the body face's class.
  const style = font
    ? ({ "--font-geist-sans": `${sans.style.fontFamily}, ${SCRIPT_FONTS[font].style.fontFamily}` } as React.CSSProperties)
    : undefined;
  return (
    <html lang={htmlLang} suppressHydrationWarning className={`${sans.variable} ${mono.variable}`} style={style}>
      <body className="min-h-dvh antialiased">
        <Providers language={language} languages={languages} messages={pickAreas(messages, CLIENT_AREAS)}>
          {children}
        </Providers>
      </body>
    </html>
  );
}
