import type { Metadata } from "next";
import { cookies, headers } from "next/headers";

import { requestI18n } from "@/lib/i18n";

import { AccountView } from "./account-view";

export async function generateMetadata(): Promise<Metadata> {
  const { t } = await requestI18n({ cookies, headers });
  return { title: t("pages.titles.account") };
}

export default function AccountPage() {
  return <AccountView />;
}
