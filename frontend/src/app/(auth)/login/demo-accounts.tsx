"use client";

import { useI18n } from "@/components/providers";

// Demo-account shortcuts for a demo deployment only. login-form.tsx loads this module only when the
// build-time flag NEXT_PUBLIC_NEARSHOP_DEMO_MODE is "true"; otherwise the build drops it entirely.
const DEMO = [
  { role: "customer", email: "priya@nearshop.demo" },
  { role: "owner", email: "owner@nearshop.demo" },
  { role: "admin", email: "admin@nearshop.demo" },
] as const;

export default function DemoAccounts({ onPick }: { onPick: (email: string) => void }) {
  const { t } = useI18n();
  return (
    <div className="mt-10 rounded-2xl border border-dashed p-4">
      <p className="text-xs font-medium text-muted-foreground">{t("auth.demo.title")}</p>
      <div className="mt-2 flex flex-wrap gap-2">
        {DEMO.map((d) => (
          <button
            key={d.email}
            type="button"
            onClick={() => onPick(d.email)}
            className="rounded-full border bg-card px-3 py-1 text-xs transition-colors hover:bg-accent"
          >
            {t(`auth.demo.${d.role}`)}
          </button>
        ))}
      </div>
    </div>
  );
}
