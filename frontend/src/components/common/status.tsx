"use client";

import { useI18n } from "@/components/providers";
import { useNow } from "@/hooks/use-debounce";
import type { OrderStatus, ReservationStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

type Tone = "brand" | "info" | "success" | "warning" | "muted" | "danger";

// The English labels are for the admin console, which stays in English (plan section 8.16). Every other screen shows
// a status through StatusBadge, in the interface language (common.status in src/messages).
export const RESERVATION_LABELS: Record<ReservationStatus, { label: string; tone: Tone }> = {
  REQUESTED: { label: "Waiting for shop", tone: "warning" },
  CONFIRMED: { label: "Held for you", tone: "info" },
  READY_FOR_PICKUP: { label: "Ready for pickup", tone: "brand" },
  COMPLETED: { label: "Picked up", tone: "success" },
  REJECTED: { label: "Declined by shop", tone: "danger" },
  CANCELLED: { label: "Cancelled", tone: "muted" },
  EXPIRED: { label: "Expired", tone: "muted" },
};

export const ORDER_LABELS: Record<OrderStatus, { label: string; tone: Tone }> = {
  PENDING: { label: "Waiting for shop", tone: "warning" },
  SHOP_CONFIRMED: { label: "Confirmed", tone: "info" },
  PREPARING: { label: "Packing", tone: "info" },
  OUT_FOR_DELIVERY: { label: "On the way", tone: "brand" },
  DELIVERED: { label: "Delivered", tone: "success" },
  CANCELLED: { label: "Cancelled", tone: "muted" },
  DELIVERY_FAILED: { label: "Delivery failed", tone: "danger" },
  RETURNED_TO_SHOP: { label: "Returned to shop", tone: "muted" },
};

const TONES: Record<Tone, string> = {
  brand: "bg-brand-soft text-brand-ink",
  info: "bg-info-soft text-info-ink",
  success: "bg-success-soft text-success-ink",
  warning: "bg-warning-soft text-warning-ink",
  muted: "bg-muted text-muted-foreground",
  danger: "bg-destructive/10 text-destructive",
};

export function StatusBadge({ status, kind, className }: { status: string; kind: "reservation" | "order"; className?: string }) {
  const { t } = useI18n();
  let tone: Tone = "muted";
  // A status this build does not know yet is shown as the API sends it.
  let label = status;
  if (kind === "reservation" && status in RESERVATION_LABELS) {
    tone = RESERVATION_LABELS[status as ReservationStatus].tone;
    label = t(`common.status.reservation.${status as ReservationStatus}`);
  } else if (kind === "order" && status in ORDER_LABELS) {
    tone = ORDER_LABELS[status as OrderStatus].tone;
    label = t(`common.status.order.${status as OrderStatus}`);
  }
  return (
    <span className={cn("inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium whitespace-nowrap", TONES[tone], className)}>
      {label}
    </span>
  );
}

/** Live mm:ss countdown to a deadline (reservation hold, response window). */
export function Countdown({ to, className, expiredLabel }: { to: string; className?: string; expiredLabel?: string }) {
  const { t } = useI18n();
  const now = useNow(1000);
  const ms = new Date(to).getTime() - now;
  if (ms <= 0) return <span className={cn("tabular", className)}>{expiredLabel ?? t("common.status.timeUp")}</span>;
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const text =
    h > 0 ? t("common.status.hoursMinutes", { hours: h, minutes: String(m).padStart(2, "0") }) : `${m}:${String(s).padStart(2, "0")}`;
  return (
    <span className={cn("tabular", ms < 5 * 60_000 && "text-destructive", className)} suppressHydrationWarning>
      {text}
    </span>
  );
}
