import type { Metadata } from "next";
import { cookies, headers } from "next/headers";

import { I18nAreas } from "@/components/providers";
import { pickAreas, requestI18n } from "@/lib/i18n";

import { OwnerShell } from "./owner-shell";

export async function generateMetadata(): Promise<Metadata> {
  const { t } = await requestI18n({ cookies, headers });
  const name = t("pages.titles.shopDashboard");
  return { title: { default: name, template: `%s · ${name}` } };
}

export default async function OwnerLayout({ children }: LayoutProps<"/shop">) {
  // The owner console's strings (P7.8, P7.9) reach the browser on its pages only, with the shop form's (settings). With English the only language
  // offered, the request is not read and the pages stay static (src/lib/i18n.ts).
  const { messages } = await requestI18n({ cookies, headers });
  return (
    <I18nAreas messages={pickAreas(messages, ["owner", "shopForm"])}>
      <OwnerShell>{children}</OwnerShell>
    </I18nAreas>
  );
}
