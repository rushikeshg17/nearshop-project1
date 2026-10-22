import type { Metadata } from "next";

import { LegalDocument, loadLegal } from "@/components/site/legal-document";

// Read from docs/legal once, when the page is built (see legal-document.tsx).
const legal = loadLegal("terms");

export const metadata: Metadata = {
  title: "Terms of use",
  description: "The terms for using NearShop to find, reserve and order from local shops.",
};

export default function TermsPage() {
  return <LegalDocument doc="terms" title="Terms of use" legal={legal} />;
}
