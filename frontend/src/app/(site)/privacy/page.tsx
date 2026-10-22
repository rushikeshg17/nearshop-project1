import type { Metadata } from "next";
import { cookies, headers } from "next/headers";

import { LegalDocument, loadLegal } from "@/components/site/legal-document";
import { requestI18n } from "@/lib/i18n";

// Read from docs/legal once, when the page is built (see legal-document.tsx).
const legal = loadLegal("privacy");

export const metadata: Metadata = {
  title: "Privacy notice",
  description: "How NearShop collects, uses and protects your personal data.",
};

export default async function PrivacyPage() {
  const { t } = await requestI18n({ cookies, headers });
  return <LegalDocument doc="privacy" title={t("shell.footer.privacy")} legal={legal} />;
}
