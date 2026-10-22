"use client";

import Link from "next/link";

import { useI18n } from "@/components/providers";
import { useMe, useMeta } from "@/hooks/use-session";

// The parts of the footer that depend on the deployment (P7.1), read from /api/meta in the browser:
// - the sentence about generated data only while the API runs on it (NEARSHOP_DEMO_MODE: the demo deployment);
// - the AI Lab link only where the lab is open to everyone (ai_lab_public: the demo and development), or to an
//   admin, since production answers 404 to everyone else. Same rule as site-header.tsx and user-menu.tsx.

export function FooterLabLink() {
  const { t } = useI18n();
  const { data: meta } = useMeta();
  const { data: me } = useMe();
  // ai_lab_public is new in the API's meta (P7.1); src/lib/types.ts does not declare it yet.
  const labPublic = (meta as { ai_lab_public?: boolean } | undefined)?.ai_lab_public === true;
  if (!labPublic && me?.role !== "admin") return null;
  return (
    <li>
      <Link href="/ai" className="hover:text-foreground">{t("shell.nav.aiLab")}</Link>
    </li>
  );
}

export function FooterDemoNotice() {
  const { t } = useI18n();
  const { data: meta } = useMeta();
  if (!meta?.demo_mode) return null;
  return <> {t("shell.footer.demoData")}</>;
}
