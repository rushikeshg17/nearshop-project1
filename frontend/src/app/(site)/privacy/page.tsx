import type { Metadata } from "next";

import { LegalDocument, loadLegal } from "@/components/site/legal-document";

// Read from docs/legal once, when the page is built (see legal-document.tsx).
const legal = loadLegal("privacy");

export const metadata: Metadata = {
  title: "Privacy notice",
  description: "How NearShop collects, uses and protects your personal data.",
};

export default function PrivacyPage() {
  return <LegalDocument doc="privacy" title="Privacy notice" legal={legal} />;
}
