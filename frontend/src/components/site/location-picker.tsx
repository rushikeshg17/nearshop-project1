"use client";

import { ChevronDown, MapPin } from "lucide-react";
import { lazy, Suspense, useState } from "react";

import { useI18n } from "@/components/providers";
import { formatNumber } from "@/lib/format";
import { useLocation } from "@/lib/location";
import { cn } from "@/lib/utils";

// The popover (Radix popover, slider and scroll area) is not first-load JavaScript (P2.9): it loads when the
// pointer or keyboard focus reaches the button, and opens on the first tap.
const loadPanel = () => import("@/components/site/location-panel");
const LocationPanel = lazy(() => loadPanel().then((m) => ({ default: m.LocationPanel })));

export function LocationPicker({ className, align = "start" }: { className?: string; align?: "start" | "end" | "center" }) {
  const { location, radiusKm } = useLocation();
  const { t } = useI18n();
  const [used, setUsed] = useState(false);
  const [open, setOpen] = useState(false);

  const trigger = (
    <button
      type="button"
      className={cn(
        "group inline-flex min-w-0 items-center gap-1.5 rounded-full border bg-card px-3 py-1.5 text-sm shadow-soft transition-colors hover:bg-accent focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none",
        className,
      )}
      aria-label={t("common.location.change")}
      aria-haspopup="dialog"
      aria-expanded={open}
      data-state={open ? "open" : "closed"}
      onPointerEnter={() => void loadPanel()}
      onFocus={() => void loadPanel()}
      onClick={
        used
          ? undefined
          : () => {
              setUsed(true);
              setOpen(true);
            }
      }
    >
      <MapPin className="size-4 shrink-0 text-brand" />
      <span className="truncate font-medium">{location?.label ?? t("common.location.set")}</span>
      <span className="shrink-0 text-muted-foreground tabular">· {t("common.location.km", { km: formatNumber(radiusKm) })}</span>
      <ChevronDown className="size-3.5 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-180" />
    </button>
  );

  if (!used) return trigger;
  return (
    <Suspense fallback={trigger}>
      <LocationPanel trigger={trigger} open={open} setOpen={setOpen} align={align} />
    </Suspense>
  );
}
