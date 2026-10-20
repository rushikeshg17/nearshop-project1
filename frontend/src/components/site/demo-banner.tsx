"use client";

import { Info } from "lucide-react";

import { useMeta } from "@/hooks/use-session";

/** A slim notice on every site page while the API reports demo_mode (NEARSHOP_DEMO_MODE). */
export function DemoBanner() {
  const { data: meta } = useMeta();
  if (!meta?.demo_mode) return null;
  return (
    <div role="note" className="border-b border-warning/30 bg-warning-soft text-warning-ink">
      <p className="mx-auto flex max-w-7xl items-center justify-center gap-2 px-4 py-1.5 text-center text-xs font-medium md:px-6">
        <Info className="size-3.5 shrink-0" aria-hidden />
        Demo with generated data. Do not enter real personal details.
      </p>
    </div>
  );
}
