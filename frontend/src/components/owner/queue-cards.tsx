"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Bike, Check, CheckCheck, Loader2, MapPin, MessageSquare, PackageCheck, Phone, RotateCcw, Timer, X } from "lucide-react";
import { motion, MotionConfig } from "motion/react";
import Link from "next/link";
import { useId, useState } from "react";
import { toast } from "sonner";

import { ProductThumb } from "@/components/common/product-thumb";
import { ResponsiveDialog } from "@/components/common/responsive-dialog";
import { Countdown, StatusBadge } from "@/components/common/status";
import { ConfirmAction } from "@/components/fulfillment/confirm-action";
import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { useNow } from "@/hooks/use-debounce";
import { useMe } from "@/hooks/use-session";
import { api, ApiError, errorMessage } from "@/lib/api";
import { formatDistance, formatNumber, formatPrice, timeAgo } from "@/lib/format";
import { type MessageKey, richText } from "@/lib/i18n";
import type { Order, OwnerShopSettings, PaymentMethod, Reservation, WorkOverview } from "@/lib/types";

type Action = "confirm" | "reject" | "ready" | "complete" | "prepare" | "dispatch" | "deliver" | "fail" | "return" | "cancel";
type ActionInput = { id: number; action: Action; reason?: string | null; code?: string; payment_method?: PaymentMethod };

function useOwnerAction<T>(kind: "reservations" | "orders", silent = false) {
  const { t } = useI18n();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, action, reason, code, payment_method }: ActionInput) =>
      api.post<T>(`/owner/${kind}/${id}/${action}`, {
        reason: reason ?? null,
        ...(code !== undefined ? { code } : {}),
        ...(payment_method !== undefined ? { payment_method } : {}),
      }),
    // The code dialog shows its own errors next to the field it asks for.
    meta: silent ? { silent: true } : undefined,
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: ["owner"] });
      // Completing or delivering records how the customer paid (P6.1): the toast says which.
      if ((v.action === "complete" || v.action === "deliver") && v.payment_method) {
        toast.success(t(`owner.queue.done.${v.action}Paid.${v.payment_method === "upi_direct" ? "upi" : "cash"}`));
      } else {
        toast.success(t(`owner.queue.done.${v.action}`));
      }
    },
  });
}

/** The reservation code as the backend stores it (R- and 6 characters). Customers see it without the R- on their
 * screen, so the owner may type it either way; case and spaces do not matter (P3.5). */
function normalizeReservationCode(input: string) {
  const raw = input.replace(/\s+/g, "").toUpperCase();
  if (!raw) return "";
  if (raw.startsWith("R-")) return raw;
  return `R-${raw.replace(/^R(?=[A-Z0-9]{6}$)/, "")}`;
}

/** Whether the shop has a UPI ID (P6.1): UPI is offered as a payment method only then. For the owner, their own
 * settings, the same query as the settings page, so a UPI ID saved there reaches the queue straight away. Staff
 * (P9.13) cannot read the settings, which are the owner's alone: for them the overview's shop.takes_upi. Null while
 * either is still loading (or failed), so the queue never claims cash only for a shop that takes UPI. */
function useShopTakesUpi(): boolean | null {
  const { data: me } = useMe();
  const isOwner = me?.role === "owner";
  const isStaff = me?.role === "staff";
  const shop = useQuery({ queryKey: ["owner", "shop"], queryFn: () => api.get<OwnerShopSettings>("/owner/shop"), enabled: isOwner });
  const work = useQuery({ queryKey: ["owner", "overview"], queryFn: () => api.get<WorkOverview>("/owner/overview"), enabled: isStaff });
  if (isOwner) return shop.data ? !!shop.data.upi_id : null;
  if (isStaff) return work.data ? work.data.shop.takes_upi : null;
  return null;
}

const PAYMENT_OPTIONS: { value: PaymentMethod; label: MessageKey; hint: MessageKey }[] = [
  { value: "cod", label: "owner.queue.payment.cash", hint: "owner.queue.payment.cashHint" },
  { value: "upi_direct", label: "owner.queue.payment.upi", hint: "owner.queue.payment.upiHint" },
];

/**
 * Asks the owner for the code the customer has (plan section 8.2): the reservation code shown on the customer's
 * screen to complete a pickup, or the 4-digit delivery code only the customer can see to deliver an order, and how
 * the customer paid, cash or UPI (P6.1, P6.2; UPI only when the shop has a UPI ID). The action runs only with both;
 * a wrong code keeps the dialog open with the reason.
 */
function CodeDialog({
  open,
  onOpenChange,
  kind,
  title,
  confirmLabel,
  pending,
  takesUpi,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kind: "pickup" | "delivery";
  title: string;
  confirmLabel: string;
  pending: boolean;
  takesUpi: boolean | null;
  onSubmit: (code: string, method: PaymentMethod) => Promise<unknown>;
}) {
  const { t } = useI18n();
  const id = useId();
  const [code, setCode] = useState("");
  const [chosen, setChosen] = useState<PaymentMethod | null>(null);
  const [error, setError] = useState<string | null>(null);
  const delivery = kind === "delivery";
  const options = PAYMENT_OPTIONS.filter((o) => takesUpi === true || o.value !== "upi_direct");
  // A UPI choice no longer offered (the UPI ID was removed meanwhile) counts as no choice.
  const method = options.some((o) => o.value === chosen) ? chosen : null;
  const codeReady = delivery ? /^\d{4}$/.test(code) : normalizeReservationCode(code).length >= 8;
  const ready = codeReady && method !== null;
  const close = (o: boolean) => {
    if (pending) return;
    if (!o) {
      setCode("");
      setChosen(null);
      setError(null);
    }
    onOpenChange(o);
  };
  const submit = async () => {
    if (!ready || pending) return;
    setError(null);
    try {
      await onSubmit(delivery ? code : normalizeReservationCode(code), method);
      close(false);
    } catch (e) {
      // A wrong code is said in the interface language, not with the API's English text.
      const wrong = e instanceof ApiError && e.code === "wrong_code";
      setError(wrong ? t(delivery ? "owner.queue.code.wrongDelivery" : "owner.queue.code.wrongPickup") : errorMessage(e));
    }
  };
  return (
    <ResponsiveDialog
      open={open}
      onOpenChange={close}
      title={title}
      description={delivery ? t("owner.queue.code.deliveryDescription") : t("owner.queue.code.pickupDescription")}
      footer={
        <>
          <Button variant="ghost" onClick={() => close(false)} disabled={pending}>{t("owner.queue.code.notYet")}</Button>
          <Button type="submit" form={`${id}-form`} disabled={!ready || pending}>
            {pending && <Loader2 className="animate-spin" />} {confirmLabel}
          </Button>
        </>
      }
    >
      <form
        id={`${id}-form`}
        className="space-y-4 pb-2"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-code`}>{delivery ? t("owner.queue.code.deliveryLabel") : t("owner.queue.code.pickupLabel")}</Label>
          <Input
            id={`${id}-code`}
            value={code}
            onChange={(e) => {
              setError(null);
              setCode(delivery ? e.target.value.replace(/\D/g, "").slice(0, 4) : e.target.value.toUpperCase().slice(0, 12));
            }}
            inputMode={delivery ? "numeric" : "text"}
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
            placeholder={delivery ? t("owner.queue.code.deliveryPlaceholder") : t("owner.queue.code.pickupPlaceholder")}
            className="h-11 font-mono text-lg tracking-[0.2em]"
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? `${id}-error` : undefined}
          />
          {error && (
            <p id={`${id}-error`} role="alert" className="text-sm font-medium text-destructive">
              {error}
            </p>
          )}
        </div>
        <fieldset className="space-y-2">
          <legend className="mb-2 text-sm font-medium">{t("owner.queue.payment.legend")}</legend>
          <RadioGroup
            value={method ?? ""}
            onValueChange={(v) => {
              setError(null);
              setChosen(v as PaymentMethod);
            }}
            className="gap-2"
          >
            {options.map((o) => (
              <div key={o.value} className="flex items-start gap-2.5">
                <RadioGroupItem value={o.value} id={`${id}-${o.value}`} className="mt-0.5" />
                <Label htmlFor={`${id}-${o.value}`} className="flex-col items-start gap-0 font-normal">
                  <span className="font-medium">{t(o.label)}</span>
                  <span className="text-xs text-muted-foreground">{t(o.hint)}</span>
                </Label>
              </div>
            ))}
          </RadioGroup>
          {takesUpi === null && (
            <p role="status" className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Loader2 className="size-3 animate-spin" /> {t("owner.queue.payment.checkingUpi")}
            </p>
          )}
          {takesUpi === false && (
            <p className="text-xs text-muted-foreground">
              {richText(t("owner.queue.payment.addUpi"), {
                settings: (
                  <Link href="/shop/settings" className="font-medium text-foreground underline underline-offset-4">
                    {t("owner.queue.payment.settingsLink")}
                  </Link>
                ),
              })}
            </p>
          )}
        </fieldset>
      </form>
    </ResponsiveDialog>
  );
}

export function OwnerReservationCard({ r }: { r: Reservation }) {
  const { t, language } = useI18n();
  const now = useNow(30_000);
  const act = useOwnerAction<Reservation>("reservations");
  const complete = useOwnerAction<Reservation>("reservations", true);
  const takesUpi = useShopTakesUpi();
  const [codeOpen, setCodeOpen] = useState(false);
  const busy = act.isPending || complete.isPending;
  const run = (action: Action, reason?: string | null) => act.mutate({ id: r.id, action, reason });
  const has = (a: string) => r.actions.includes(a);

  return (
    <MotionConfig reducedMotion="user">
      <motion.li layout initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="rounded-2xl border bg-card p-4">
        <div className="flex gap-4">
          <ProductThumb icon={r.product.icon} imageUrl={r.product.image_url} thumbnailUrl={r.product.thumbnail_url} alt="" size="md" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={r.status} kind="reservation" />
              {/* Not the reservation code: the customer shows it at the counter to complete the pickup (P3.5). */}
              <span className="text-xs text-muted-foreground tabular">#{r.id}</span>
              <span className="text-xs text-muted-foreground" suppressHydrationWarning>{timeAgo(r.created_at, now, language)}</span>
            </div>
            <p className="mt-1.5 font-medium">{t("owner.common.quantityItem", { count: formatNumber(r.quantity), name: r.product.name })}</p>
            <p className="text-sm text-muted-foreground">
              {r.customer?.name}
              {r.customer?.phone && (
                <> · <a href={`tel:${r.customer.phone}`} className="inline-flex items-center gap-1 hover:text-foreground"><Phone className="size-3" />{r.customer.phone}</a></>
              )}
              {" · "}
              <span className="font-medium text-foreground tabular">{formatPrice(r.total)}</span>
            </p>
            {r.note && (
              <p className="mt-1.5 inline-flex items-start gap-1.5 rounded-lg bg-muted px-2 py-1 text-sm"><MessageSquare className="mt-0.5 size-3.5 shrink-0" /> {r.note}</p>
            )}
            {["REQUESTED", "CONFIRMED", "READY_FOR_PICKUP"].includes(r.status) && (
              <p className="mt-1.5 inline-flex items-center gap-1 text-xs font-medium text-brand-ink">
                <Timer className="size-3.5" /> {r.status === "REQUESTED" ? t("owner.queue.respondWithin") : t("owner.queue.holdEndsIn")} <Countdown to={r.expires_at} />
                {r.status === "REQUESTED" && (
                  <span className="font-normal text-muted-foreground">
                    {" · "}
                    {t("owner.queue.onShelf", { count: formatNumber(r.product.quantity_in_stock) })}
                  </span>
                )}
              </p>
            )}
          </div>
        </div>
        {r.actions.length > 0 && (
          <div className="mt-4 flex flex-wrap gap-2 border-t pt-3">
            {has("confirm") && (
              <Button size="sm" onClick={() => run("confirm")} disabled={busy}><Check /> {t("owner.queue.reservation.confirm")}</Button>
            )}
            {has("ready") && (
              <Button size="sm" onClick={() => run("ready")} disabled={busy}><PackageCheck /> {t("owner.queue.reservation.ready")}</Button>
            )}
            {has("complete") && (
              <Button size="sm" variant={has("ready") ? "outline" : "default"} onClick={() => setCodeOpen(true)} disabled={busy}><CheckCheck /> {t("owner.queue.reservation.complete")}</Button>
            )}
            {has("reject") && (
              <ConfirmAction title={t("owner.queue.reservation.declineTitle")} description={t("owner.queue.reservation.declineBody")}
                confirmLabel={t("owner.queue.reservation.decline")} reasonLabel={t("owner.queue.reasonForCustomer")}
                reasonPlaceholder={t("owner.queue.reservation.declinePlaceholder")} pending={busy}
                onConfirm={(reason) => run("reject", reason)}
                trigger={(open) => <Button size="sm" variant="ghost" onClick={open} disabled={busy}><X /> {t("owner.queue.reservation.decline")}</Button>} />
            )}
            {has("cancel") && (
              <ConfirmAction title={t("owner.queue.reservation.cancelTitle")} description={t("owner.queue.reservation.cancelBody")}
                confirmLabel={t("owner.queue.reservation.cancelConfirm")} reasonLabel={t("owner.queue.reservation.cancelReason")}
                reasonPlaceholder={t("owner.queue.reservation.cancelPlaceholder")} pending={busy}
                onConfirm={(reason) => run("cancel", reason)}
                trigger={(open) => <Button size="sm" variant="ghost" onClick={open} disabled={busy}><Ban /> {t("owner.queue.reservation.noShow")}</Button>} />
            )}
          </div>
        )}
        <CodeDialog open={codeOpen} onOpenChange={setCodeOpen} kind="pickup" title={t("owner.queue.reservation.completeTitle")}
          confirmLabel={t("owner.queue.reservation.complete")} pending={complete.isPending} takesUpi={takesUpi}
          onSubmit={(code, payment_method) => complete.mutateAsync({ id: r.id, action: "complete", code, payment_method })} />
      </motion.li>
    </MotionConfig>
  );
}

export function OwnerOrderCard({ o }: { o: Order }) {
  const { t, language } = useI18n();
  const now = useNow(30_000);
  const act = useOwnerAction<Order>("orders");
  const deliver = useOwnerAction<Order>("orders", true);
  const takesUpi = useShopTakesUpi();
  const [codeOpen, setCodeOpen] = useState(false);
  const busy = act.isPending || deliver.isPending;
  const run = (action: Action, reason?: string | null) => act.mutate({ id: o.id, action, reason });
  const has = (a: string) => o.actions.includes(a);

  return (
    <MotionConfig reducedMotion="user">
      <motion.li layout initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="rounded-2xl border bg-card p-4">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={o.status} kind="order" />
          <span className="font-mono text-xs text-muted-foreground">{o.code}</span>
          <span className="text-xs text-muted-foreground" suppressHydrationWarning>{timeAgo(o.created_at, now, language)}</span>
          <span className="ml-auto font-semibold tabular">
            {formatPrice(o.total)}{" "}
            <span className="text-xs font-normal text-muted-foreground">
              {o.payment_status === "paid"
                ? t(o.payment_method === "upi_direct" ? "owner.queue.order.paidUpi" : "owner.queue.order.paidCash")
                : takesUpi === null
                  ? t("owner.queue.order.payOnDelivery")
                  : takesUpi
                    ? t("owner.queue.order.cashOrUpi")
                    : t("owner.queue.order.cod")}
            </span>
          </span>
        </div>
        <ul className="mt-3 space-y-1 text-sm">
          {o.items.map((i) => (
            <li key={i.product_id} className="flex justify-between gap-3"><span className="truncate">{t("owner.common.quantityItem", { count: formatNumber(i.quantity), name: i.name })}</span><span className="text-muted-foreground tabular">{formatPrice(i.unit_price * i.quantity)}</span></li>
          ))}
        </ul>
        <div className="mt-3 space-y-1 rounded-xl bg-muted/60 p-3 text-sm">
          <p className="font-medium">{o.customer?.name}{o.contact_phone && <> · <a href={`tel:${o.contact_phone}`} className="hover:underline">{o.contact_phone}</a></>}</p>
          <p className="flex gap-1.5 text-muted-foreground"><MapPin className="mt-0.5 size-3.5 shrink-0" /> {o.delivery_address} · {formatDistance(o.distance_km)}</p>
          {o.note && <p className="text-muted-foreground">{t("owner.queue.order.note", { note: o.note })}</p>}
          <a className="inline-flex items-center gap-1 text-xs font-medium text-info-ink hover:underline" target="_blank" rel="noreferrer"
            href={`https://www.openstreetmap.org/directions?to=${o.delivery_lat}%2C${o.delivery_lng}`}>{t("owner.queue.order.openRoute")}</a>
        </div>
        {o.actions.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {has("confirm") && <Button size="sm" onClick={() => run("confirm")} disabled={busy}><Check /> {t("owner.queue.order.confirm")}</Button>}
            {has("prepare") && <Button size="sm" onClick={() => run("prepare")} disabled={busy}><PackageCheck /> {t("owner.queue.order.prepare")}</Button>}
            {has("dispatch") && <Button size="sm" onClick={() => run("dispatch")} disabled={busy}><Bike /> {t("owner.queue.order.dispatch")}</Button>}
            {has("deliver") && <Button size="sm" onClick={() => setCodeOpen(true)} disabled={busy}><CheckCheck /> {t("owner.queue.order.deliver")}</Button>}
            {has("fail") && (
              <ConfirmAction title={t("owner.queue.order.failTitle")} confirmLabel={t("owner.queue.order.failConfirm")}
                reasonLabel={t("owner.queue.order.failReason")} reasonRequired reasonPlaceholder={t("owner.queue.order.failPlaceholder")} pending={busy}
                onConfirm={(reason) => run("fail", reason)}
                trigger={(open) => <Button size="sm" variant="ghost" onClick={open}>{t("owner.queue.order.couldNotDeliver")}</Button>} />
            )}
            {has("return") && <Button size="sm" variant="outline" onClick={() => run("return")} disabled={busy}><RotateCcw /> {t("owner.queue.order.return")}</Button>}
            {has("cancel") && (
              <ConfirmAction title={t("owner.queue.order.cancelTitle")} description={t("owner.queue.order.cancelBody")}
                confirmLabel={t("owner.queue.order.cancelConfirm")} reasonLabel={t("owner.queue.reasonForCustomer")} reasonRequired pending={busy}
                onConfirm={(reason) => run("cancel", reason)}
                trigger={(open) => (
                  <Button size="sm" variant="ghost" onClick={open} disabled={busy}>
                    <X /> {o.status === "PENDING" ? t("owner.queue.order.decline") : t("owner.queue.order.cancel")}
                  </Button>
                )} />
            )}
          </div>
        )}
        <CodeDialog open={codeOpen} onOpenChange={setCodeOpen} kind="delivery" title={t("owner.queue.order.deliverTitle")}
          confirmLabel={t("owner.queue.order.deliver")} pending={deliver.isPending} takesUpi={takesUpi}
          onSubmit={(code, payment_method) => deliver.mutateAsync({ id: o.id, action: "deliver", code, payment_method })} />
      </motion.li>
    </MotionConfig>
  );
}
