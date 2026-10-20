import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { Providers } from "@/components/providers";

import "./globals.css";

// One font on the critical path (P2.9): the body face, preloaded. The mono face (reservation and order codes)
// is declared but not preloaded, so the browser fetches it only on a page that shows a code. The display face
// that headings used (Bricolage Grotesque, 41 KB) is dropped: it was fetched on every page and, on slow 4G,
// competed with the page's own scripts and data; headings use the body face (globals.css, --font-heading).
const sans = Geist({ variable: "--font-geist-sans", subsets: ["latin"], display: "swap" });
const mono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"], preload: false });

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

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en-IN" suppressHydrationWarning className={`${sans.variable} ${mono.variable}`}>
      <body className="min-h-dvh antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
