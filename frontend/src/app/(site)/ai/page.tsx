import type { Metadata } from "next";
import { cookies, headers } from "next/headers";

import { I18nAreas } from "@/components/providers";
import { pickAreas, requestI18n } from "@/lib/i18n";

import { AiLabView } from "./ai-lab-view";

export async function generateMetadata(): Promise<Metadata> {
  const { t } = await requestI18n({ cookies, headers });
  return { title: t("pages.titles.aiLab"), description: t("pages.descriptions.aiLab") };
}

export default async function AiLabPage() {
  // The AI Lab's strings (P7.10) reach the browser on this page only.
  const { messages } = await requestI18n({ cookies, headers });
  return (
    <I18nAreas messages={pickAreas(messages, ["aiLab"])}>
      <AiLabView />
    </I18nAreas>
  );
}
