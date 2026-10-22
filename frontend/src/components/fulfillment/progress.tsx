"use client";

import { Check } from "lucide-react";
import { motion, MotionConfig } from "motion/react";

import { useI18n } from "@/components/providers";
import { formatTime } from "@/lib/format";
import type { MessageKey } from "@/lib/i18n";
import type { Order, OrderStatus, Reservation, ReservationStatus, TimelineEvent } from "@/lib/types";
import { cn } from "@/lib/utils";

// The happy path of each, with each step's label (common.progress in src/messages).
const RES_STEPS: { status: ReservationStatus; label: MessageKey }[] = [
  { status: "REQUESTED", label: "common.progress.reservationSteps.REQUESTED" },
  { status: "CONFIRMED", label: "common.progress.reservationSteps.CONFIRMED" },
  { status: "READY_FOR_PICKUP", label: "common.progress.reservationSteps.READY_FOR_PICKUP" },
  { status: "COMPLETED", label: "common.progress.reservationSteps.COMPLETED" },
];
const ORDER_STEPS: { status: OrderStatus; label: MessageKey }[] = [
  { status: "PENDING", label: "common.progress.orderSteps.PENDING" },
  { status: "SHOP_CONFIRMED", label: "common.progress.orderSteps.SHOP_CONFIRMED" },
  { status: "PREPARING", label: "common.progress.orderSteps.PREPARING" },
  { status: "OUT_FOR_DELIVERY", label: "common.progress.orderSteps.OUT_FOR_DELIVERY" },
  { status: "DELIVERED", label: "common.progress.orderSteps.DELIVERED" },
];

/** Horizontal stepper for the happy path; side exits (cancelled, expired...) are shown by the caller. */
export function FulfillmentProgress({ item, className }: { item: Reservation | Order; className?: string }) {
  const { t, language } = useI18n();
  const steps: { status: string; label: MessageKey }[] = item.kind === "reservation" ? RES_STEPS : ORDER_STEPS;
  const reached = new Map<string, string>();
  for (const e of item.timeline ?? []) reached.set(e.to_status, e.at);
  let current = steps.findIndex((s) => s.status === item.status);
  if (current === -1) {
    // closed on a side path: progress stops at the last happy-path step reached
    current = Math.max(-1, ...steps.map((s, i) => (reached.has(s.status) ? i : -1)));
  }
  const failed = !steps.some((s) => s.status === item.status);

  return (
    <MotionConfig reducedMotion="user">
      <ol className={cn("flex items-start", className)} aria-label={t("common.progress.label")}>
        {steps.map((s, i) => {
          const done = i < current || (i === current && (s.status === "COMPLETED" || s.status === "DELIVERED"));
          const active = i === current && !done && !failed;
          return (
            <li key={s.status} className="relative flex flex-1 flex-col items-center text-center">
              {i > 0 && (
                <span className="absolute top-3.5 right-1/2 h-0.5 w-full -translate-y-1/2 bg-muted" aria-hidden>
                  <motion.span
                    className="block h-full origin-left bg-brand"
                    initial={{ scaleX: 0 }}
                    animate={{ scaleX: i <= current ? 1 : 0 }}
                    transition={{ duration: 0.5, delay: i * 0.08 }}
                  />
                </span>
              )}
              <span
                className={cn(
                  "relative z-10 grid size-7 place-items-center rounded-full border-2 text-xs font-semibold transition-colors",
                  done ? "border-brand bg-brand text-brand-foreground" : active ? "border-brand bg-card text-brand-ink" : "border-muted bg-card text-muted-foreground",
                )}
              >
                {done ? <Check className="size-3.5" strokeWidth={3} /> : i + 1}
                {active && <span className="absolute inset-0 animate-ping rounded-full border-2 border-brand/50" aria-hidden />}
              </span>
              <span className={cn("mt-2 text-xs font-medium", i <= current ? "text-foreground" : "text-muted-foreground")}>{t(s.label)}</span>
              {reached.get(s.status) && <span className="text-[11px] text-muted-foreground tabular">{formatTime(reached.get(s.status), language)}</span>}
            </li>
          );
        })}
      </ol>
    </MotionConfig>
  );
}

// What each status change in the timeline says (common.progress.events); a status this build does not know yet is
// shown as the API sends it. The same goes for who made the change (common.progress.actors).
const EVENT_STATUSES = new Set<string>([
  "REQUESTED",
  "CONFIRMED",
  "READY_FOR_PICKUP",
  "COMPLETED",
  "REJECTED",
  "CANCELLED",
  "EXPIRED",
  "PENDING",
  "SHOP_CONFIRMED",
  "PREPARING",
  "OUT_FOR_DELIVERY",
  "DELIVERED",
  "DELIVERY_FAILED",
  "RETURNED_TO_SHOP",
] satisfies (ReservationStatus | OrderStatus)[]);
const ACTORS = new Set(["customer", "owner", "system", "admin"]);
type Actor = "you" | "customer" | "shop" | "system" | "admin";

export function EventTimeline({ events, viewer = "customer" }: { events: TimelineEvent[]; viewer?: "customer" | "owner" }) {
  const { t, language } = useI18n();
  const actor = (role: string) => {
    if (!ACTORS.has(role)) return role;
    // Each side sees itself as "You".
    const who: Actor = role === viewer ? "you" : role === "owner" ? "shop" : (role as Actor);
    return t(`common.progress.actors.${who}`);
  };
  return (
    <ol className="relative space-y-4 border-l pl-5">
      {[...events].reverse().map((e, i) => (
        <li key={`${e.to_status}-${e.at}-${i}`} className="relative">
          <span className={cn("absolute top-1.5 -left-[25px] size-2.5 rounded-full border-2 border-card", i === 0 ? "bg-brand" : "bg-muted-foreground/40")} />
          <p className="text-sm font-medium">{EVENT_STATUSES.has(e.to_status) ? t(`common.progress.events.${e.to_status as ReservationStatus | OrderStatus}`) : e.to_status}</p>
          <p className="text-xs text-muted-foreground">
            {formatTime(e.at, language)} · {actor(e.actor_role)}
            {e.note && <> · {t("common.progress.note", { note: e.note })}</>}
          </p>
        </li>
      ))}
    </ol>
  );
}
