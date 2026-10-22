import type { Metadata } from "next";
import { cookies, headers } from "next/headers";

import { I18nAreas } from "@/components/providers";
import { pickAreas, requestI18n } from "@/lib/i18n";

import { GrievanceView } from "./grievance-view";

export async function generateMetadata(): Promise<Metadata> {
  const { t } = await requestI18n({ cookies, headers });
  return { title: t("pages.titles.grievance"), description: t("pages.descriptions.grievance") };
}

// The in-app acknowledgement links here with ?ticket=G-XXXXXX, which pre-fills the status lookup.
export default async function GrievancePage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const [{ ticket }, { messages }] = await Promise.all([searchParams, requestI18n({ cookies, headers })]);
  // The grievance page's strings (P7.10) reach the browser on this page only.
  return (
    <I18nAreas messages={pickAreas(messages, ["support"])}>
      <GrievanceView initialTicket={typeof ticket === "string" ? ticket.slice(0, 20) : ""} />
    </I18nAreas>
  );
}
