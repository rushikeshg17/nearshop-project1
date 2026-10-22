import type { Metadata } from "next";

import { GrievanceView } from "./grievance-view";

export const metadata: Metadata = {
  title: "Grievances",
  description: "File a grievance with NearShop's grievance officer and track it by ticket number. Every grievance is acknowledged on receipt and resolved within 30 days.",
};

// The in-app acknowledgement links here with ?ticket=G-XXXXXX, which pre-fills the status lookup.
export default async function GrievancePage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const { ticket } = await searchParams;
  return <GrievanceView initialTicket={typeof ticket === "string" ? ticket.slice(0, 20) : ""} />;
}
