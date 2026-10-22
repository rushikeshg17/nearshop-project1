import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono, Noto_Sans_Kannada } from "next/font/google";
import { cookies, headers } from "next/headers";

import { Providers } from "@/components/providers";
import { languageInfo, requestI18n, type ScriptFont } from "@/lib/i18n";

import "./globals.css";

// One font on the critical path (P2.9): the body face, preloaded. The mono face (reservation and order codes)
// is declared but not preloaded, so the browser fetches it only on a page that shows a code. The display face
// that headings used (Bricolage Grotesque, 41 KB) is dropped: it was fetched on every page and, on slow 4G,
// competed with the page's own scripts and data; headings use the body face (globals.css, --font-heading).
const sans = Geist({ variable: "--font-geist-sans", subsets: ["latin"], display: "swap" });
const mono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"], preload: false });

// Fonts for scripts the body face does not cover (P7.5, plan section 8.16), one entry per ScriptFont in
// src/lib/i18n.ts. Each loads only its own script's subset, is never preloaded and never blocks text (swap), and is
// added to the body font's family list only while its language is active, and only after the page has loaded and
// the visitor has tapped or typed (Providers, useScriptFont): in any other language the page never names it, so
// the browser never fetches it. Latin text keeps the body face; the script's characters, which the
// body face lacks, fall through to this one.
// One static weight, not the variable font: the Kannada subset is 28 KB at 400, against 91 KB for the variable
// file. Medium text uses it as it is; semibold and bold are synthesised.
const kannada = Noto_Sans_Kannada({ subsets: ["kannada"], weight: "400", display: "swap", preload: false });
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
  // English. Reading them renders every page per request; the client components get the messages from Providers.
  const { language, messages } = await requestI18n({ cookies, headers });
  const { htmlLang, font } = languageInfo(language);
  // The body face first (Latin text), then the script's own face. Providers sets it as --font-geist-sans, which
  // feeds --font-sans and --font-heading (globals.css).
  const fontStack = font ? `${sans.style.fontFamily}, ${SCRIPT_FONTS[font].style.fontFamily}` : null;
  return (
    <html lang={htmlLang} suppressHydrationWarning className={`${sans.variable} ${mono.variable}`}>
      <body className="min-h-dvh antialiased">
        <Providers language={language} messages={messages} fontStack={fontStack}>
          {children}
        </Providers>
      </body>
    </html>
  );
}
