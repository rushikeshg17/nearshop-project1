import type { Metadata } from "next";
import { cookies, headers } from "next/headers";
import { Suspense } from "react";

import { requestI18n } from "@/lib/i18n";

import { ShopsView } from "./shops-view";

export async function generateMetadata(): Promise<Metadata> {
  const { t } = await requestI18n({ cookies, headers });
  return { title: t("pages.titles.shops") };
}

export default function ShopsPage() {
  return (
    <Suspense>
      <ShopsView />
    </Suspense>
  );
}
