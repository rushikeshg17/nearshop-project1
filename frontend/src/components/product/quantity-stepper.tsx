"use client";

import { Minus, Plus } from "lucide-react";

import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";

export function QuantityStepper({ value, onChange, max, min = 1 }: { value: number; onChange: (v: number) => void; max: number; min?: number }) {
  const { t } = useI18n();
  return (
    <div className="inline-flex items-center rounded-xl border" role="group" aria-label={t("common.quantity.label")}>
      <Button variant="ghost" size="icon-lg" onClick={() => onChange(Math.max(min, value - 1))} disabled={value <= min} aria-label={t("common.quantity.decrease")}>
        <Minus />
      </Button>
      <span className="w-10 text-center font-semibold tabular" aria-live="polite">
        {value}
      </span>
      <Button variant="ghost" size="icon-lg" onClick={() => onChange(Math.min(max, value + 1))} disabled={value >= max} aria-label={t("common.quantity.increase")}>
        <Plus />
      </Button>
    </div>
  );
}
