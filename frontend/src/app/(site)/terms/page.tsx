import type { Metadata } from "next";
import { cookies, headers } from "next/headers";

import { LegalDocument, loadLegal } from "@/components/site/legal-document";
import { requestI18n } from "@/lib/i18n";

// Read from docs/legal once, when the page is built (see legal-document.tsx).
const legal = loadLegal("terms");

export const metadata: Metadata = {
  title: "Terms of use",
  description: "The terms for using NearShop to find, reserve and order from local shops.",
};

export default async function TermsPage() {
  const { t } = await requestI18n({ cookies, headers });
  return <LegalDocument doc="terms" title={t("shell.footer.terms")} legal={legal} />;
}
