import { cookies, headers } from "next/headers";

import { I18nAreas } from "@/components/providers";
import { pickAreas, requestI18n } from "@/lib/i18n";

import { OwnerShell } from "./owner-shell";

export const metadata = { title: { default: "Shop dashboard", template: "%s · Shop dashboard" } };

export default async function OwnerLayout({ children }: LayoutProps<"/shop">) {
  // The owner console's strings (P7.8, P7.9) reach the browser on its pages only. With English the only language
  // offered, the request is not read and the pages stay static (src/lib/i18n.ts).
  const { messages } = await requestI18n({ cookies, headers });
  return (
    <I18nAreas messages={pickAreas(messages, ["owner"])}>
      <OwnerShell>{children}</OwnerShell>
    </I18nAreas>
  );
}
