import type { MetadataRoute } from "next";
import { cookies, headers } from "next/headers";

import { languageInfo, requestI18n } from "@/lib/i18n";

// The web app manifest (P7.12), served at /manifest.webmanifest and linked from every page by Next.js. With it, the
// worker (public/sw.js) and the site served over https, Chrome offers its own install prompt: there is no custom
// install pop-up. The colours are the light theme's from src/app/layout.tsx. The icons are in public/icons (192 px,
// 512 px and a maskable 512 px, with the mark inside the central safe zone). The language is the visitor's
// interface language, as for the pages; with English the only language offered, this file stays static.
export default async function manifest(): Promise<MetadataRoute.Manifest> {
  const { language, t } = await requestI18n({ cookies, headers });
  return {
    name: "NearShop",
    short_name: "NearShop",
    description: t("pages.site.description"),
    lang: languageInfo(language).htmlLang,
    dir: "ltr",
    id: "/",
    start_url: "/",
    scope: "/",
    display: "standalone",
    theme_color: "#fbfaf8",
    background_color: "#fbfaf8",
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      { src: "/icons/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
