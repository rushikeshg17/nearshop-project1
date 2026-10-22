import type { Metadata } from "next";
import { cookies, headers } from "next/headers";
import { Suspense } from "react";

import { requestI18n } from "@/lib/i18n";

import { SearchView } from "./search-view";

export async function generateMetadata({ searchParams }: PageProps<"/search">): Promise<Metadata> {
  const [{ q }, { t }] = await Promise.all([searchParams, requestI18n({ cookies, headers })]);
  return { title: typeof q === "string" && q ? t("pages.titles.searchQuery", { query: q }) : t("pages.titles.search") };
}

export default function SearchPage() {
  return (
    <Suspense>
      <SearchView />
    </Suspense>
  );
}
