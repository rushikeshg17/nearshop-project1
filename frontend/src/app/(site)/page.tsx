import type { Metadata } from "next";
import { cookies, headers } from "next/headers";
import { Suspense } from "react";

import { requestI18n } from "@/lib/i18n";

import { HomeView } from "./home-view";

export async function generateMetadata(): Promise<Metadata> {
  const { t } = await requestI18n({ cookies, headers });
  return { title: { absolute: t("pages.titles.home") } };
}

export default function HomePage() {
  return (
    <Suspense>
      <HomeView />
    </Suspense>
  );
}
