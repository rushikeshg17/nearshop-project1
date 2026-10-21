"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Ban, Bike, Check, CheckCheck, Loader2, MapPin, MessageSquare, PackageCheck, Phone, RotateCcw, Timer, X } from "lucide-react";
import { motion, MotionConfig } from "motion/react";
import { useId, useState } from "react";
import { toast } from "sonner";

import { ProductThumb } from "@/components/common/product-thumb";
import { ResponsiveDialog } from "@/components/common/responsive-dialog";
import { Countdown, StatusBadge } from "@/components/common/status";
import { ConfirmAction } from "@/components/fulfillment/confirm-action";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError, errorMessage } from "@/lib/api";
import { formatDistance, formatPrice, timeAgo } from "@/lib/format";
import type { Order, Reservation } from "@/lib/types";

type ActionInput = { id: number; action: string; reason?: string | null; code?: string };

function useOwnerAction<T>(kind: "reservations" | "orders", silent = false) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, action, reason, code }: ActionInput) =>
      api.post<T>(`/owner/${kind}/${id}/${action}`, { reason: reason ?? null, ...(code !== undefined ? { code } : {}) }),
    // The code dialog shows its own errors next to the field it asks for.
    meta: silent ? { silent: true } : undefined,
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: ["owner"] });
      const done: Record<string, string> = {
        confirm: "Confirmed. The customer has been notified.",
        reject: "Declined. The customer has been notified.",
        ready: "Marked ready. The customer has been told to come by.",
        complete: "Completed. Sale recorded.",
        prepare: "Marked as packing.",
        dispatch: "Out for delivery. Customer notified.",
        deliver: "Delivered. Cash collected and sale recorded.",
        fail: "Marked as failed.",
        return: "Returned to shop. Stock restored.",
        cancel: "Cancelled. Stock returned to the shelf.",
      };
      toast.success(done[v.action] ?? "Updated");
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

/**
 * Asks the owner for the code the customer has (plan section 8.2): the reservation code shown on the customer's
 * screen to complete a pickup, or the 4-digit delivery code only the customer can see to deliver an order. The
 * action runs only with it; a wrong code keeps the dialog open with the reason.
 */
function CodeDialog({
  open,
  onOpenChange,
  kind,
  title,
  confirmLabel,
  pending,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kind: "pickup" | "delivery";
  title: string;
  confirmLabel: string;
  pending: boolean;
  onSubmit: (code: string) => Promise<unknown>;
}) {
  const id = useId();
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const delivery = kind === "delivery";
  const ready = delivery ? /^\d{4}$/.test(code) : normalizeReservationCode(code).length >= 8;
  const close = (o: boolean) => {
    if (pending) return;
    if (!o) {
      setCode("");
      setError(null);
    }
    onOpenChange(o);
  };
  const submit = async () => {
    if (!ready || pending) return;
    setError(null);
    try {
      await onSubmit(delivery ? code : normalizeReservationCode(code));
      close(false);
    } catch (e) {
      const wrong = e instanceof ApiError && e.code === "wrong_code";
      setError(wrong ? `${errorMessage(e)}. Check the code with the customer and try again.` : errorMessage(e));
    }
  };
  return (
    <ResponsiveDialog
      open={open}
      onOpenChange={close}
      title={title}
      description={
        delivery
          ? "Ask the customer for the 4-digit delivery code on their order screen. Hand over the order only after it is accepted."
          : "Ask the customer to show the pickup code on their reservation screen, and type it here."
      }
      footer={
        <>
          <Button variant="ghost" onClick={() => close(false)} disabled={pending}>Not yet</Button>
          <Button type="submit" form={`${id}-form`} disabled={!ready || pending}>
            {pending && <Loader2 className="animate-spin" />} {confirmLabel}
          </Button>
        </>
      }
    >
      <form
        id={`${id}-form`}
        className="space-y-1.5 pb-2"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <Label htmlFor={`${id}-code`}>{delivery ? "Delivery code" : "Pickup code"}</Label>
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
          placeholder={delivery ? "4 digits" : "e.g. 7KQ2MX"}
          className="h-11 font-mono text-lg tracking-[0.2em]"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : undefined}
        />
        {error && (
          <p id={`${id}-error`} role="alert" className="text-sm font-medium text-destructive">
            {error}
          </p>
        )}
      </form>
    </ResponsiveDialog>
  );
}

export function OwnerReservationCard({ r }: { r: Reservation }) {
  const act = useOwnerAction<Reservation>("reservations");
  const complete = useOwnerAction<Reservation>("reservations", true);
  const [codeOpen, setCodeOpen] = useState(false);
  const busy = act.isPending || complete.isPending;
  const run = (action: string, reason?: string | null) => act.mutate({ id: r.id, action, reason });
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
              <span className="text-xs text-muted-foreground" suppressHydrationWarning>{timeAgo(r.created_at)}</span>
            </div>
            <p className="mt-1.5 font-medium">
              {r.quantity} x {r.product.name}
            </p>
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
                <Timer className="size-3.5" /> {r.status === "REQUESTED" ? "Respond within" : "Hold ends in"} <Countdown to={r.expires_at} />
                {r.status === "REQUESTED" && <span className="font-normal text-muted-foreground"> · {r.product.quantity_in_stock} on your shelf</span>}
              </p>
            )}
          </div>
        </div>
        {r.actions.length > 0 && (
          <div className="mt-4 flex flex-wrap gap-2 border-t pt-3">
            {has("confirm") && (
              <Button size="sm" onClick={() => run("confirm")} disabled={busy}><Check /> Confirm and hold</Button>
            )}
            {has("ready") && (
              <Button size="sm" onClick={() => run("ready")} disabled={busy}><PackageCheck /> Mark ready</Button>
            )}
            {has("complete") && (
              <Button size="sm" variant={has("ready") ? "outline" : "default"} onClick={() => setCodeOpen(true)} disabled={busy}><CheckCheck /> Picked up and paid</Button>
            )}
            {has("reject") && (
              <ConfirmAction title="Decline this reservation?" description="The customer will be told and pointed to other shops." confirmLabel="Decline"
                reasonLabel="Reason for the customer" reasonPlaceholder="e.g. Last piece just sold at the counter" pending={busy}
                onConfirm={(reason) => run("reject", reason)}
                trigger={(open) => <Button size="sm" variant="ghost" onClick={open} disabled={busy}><X /> Decline</Button>} />
            )}
            {has("cancel") && (
              <ConfirmAction title="Cancel this reservation?" description="Use this when the customer didn't show up. The item goes back into stock." confirmLabel="Cancel reservation"
                reasonLabel="Reason" reasonPlaceholder="e.g. Customer did not come" pending={busy}
                onConfirm={(reason) => run("cancel", reason)}
                trigger={(open) => <Button size="sm" variant="ghost" onClick={open} disabled={busy}><Ban /> No-show</Button>} />
            )}
          </div>
        )}
        <CodeDialog open={codeOpen} onOpenChange={setCodeOpen} kind="pickup" title="Complete this pickup"
          confirmLabel="Picked up and paid" pending={complete.isPending}
          onSubmit={(code) => complete.mutateAsync({ id: r.id, action: "complete", code })} />
      </motion.li>
    </MotionConfig>
  );
}

export function OwnerOrderCard({ o }: { o: Order }) {
  const act = useOwnerAction<Order>("orders");
  const deliver = useOwnerAction<Order>("orders", true);
  const [codeOpen, setCodeOpen] = useState(false);
  const busy = act.isPending || deliver.isPending;
  const run = (action: string, reason?: string | null) => act.mutate({ id: o.id, action, reason });
  const has = (a: string) => o.actions.includes(a);

  return (
    <MotionConfig reducedMotion="user">
      <motion.li layout initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="rounded-2xl border bg-card p-4">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={o.status} kind="order" />
          <span className="font-mono text-xs text-muted-foreground">{o.code}</span>
          <span className="text-xs text-muted-foreground" suppressHydrationWarning>{timeAgo(o.created_at)}</span>
          <span className="ml-auto font-semibold tabular">{formatPrice(o.total)} <span className="text-xs font-normal text-muted-foreground">COD</span></span>
        </div>
        <ul className="mt-3 space-y-1 text-sm">
          {o.items.map((i) => (
            <li key={i.product_id} className="flex justify-between gap-3"><span className="truncate">{i.quantity} x {i.name}</span><span className="text-muted-foreground tabular">{formatPrice(i.unit_price * i.quantity)}</span></li>
          ))}
        </ul>
        <div className="mt-3 space-y-1 rounded-xl bg-muted/60 p-3 text-sm">
          <p className="font-medium">{o.customer?.name}{o.contact_phone && <> · <a href={`tel:${o.contact_phone}`} className="hover:underline">{o.contact_phone}</a></>}</p>
          <p className="flex gap-1.5 text-muted-foreground"><MapPin className="mt-0.5 size-3.5 shrink-0" /> {o.delivery_address} · {formatDistance(o.distance_km)}</p>
          {o.note && <p className="text-muted-foreground">Note: {o.note}</p>}
          <a className="inline-flex items-center gap-1 text-xs font-medium text-info-ink hover:underline" target="_blank" rel="noreferrer"
            href={`https://www.openstreetmap.org/directions?to=${o.delivery_lat}%2C${o.delivery_lng}`}>Open route</a>
        </div>
        {o.actions.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {has("confirm") && <Button size="sm" onClick={() => run("confirm")} disabled={busy}><Check /> Confirm order</Button>}
            {has("prepare") && <Button size="sm" onClick={() => run("prepare")} disabled={busy}><PackageCheck /> Start packing</Button>}
            {has("dispatch") && <Button size="sm" onClick={() => run("dispatch")} disabled={busy}><Bike /> Out for delivery</Button>}
            {has("deliver") && <Button size="sm" onClick={() => setCodeOpen(true)} disabled={busy}><CheckCheck /> Delivered, cash collected</Button>}
            {has("fail") && (
              <ConfirmAction title="Delivery failed?" confirmLabel="Mark failed" reasonLabel="What happened?" reasonRequired reasonPlaceholder="e.g. Customer not reachable" pending={busy}
                onConfirm={(reason) => run("fail", reason)} trigger={(open) => <Button size="sm" variant="ghost" onClick={open}>Couldn&apos;t deliver</Button>} />
            )}
            {has("return") && <Button size="sm" variant="outline" onClick={() => run("return")} disabled={busy}><RotateCcw /> Back in shop</Button>}
            {has("cancel") && (
              <ConfirmAction title="Cancel this order?" description="The customer is notified and any held stock goes back on the shelf." confirmLabel="Cancel order"
                reasonLabel="Reason for the customer" reasonRequired pending={busy} onConfirm={(reason) => run("cancel", reason)}
                trigger={(open) => <Button size="sm" variant="ghost" onClick={open} disabled={busy}><X /> {o.status === "PENDING" ? "Decline" : "Cancel"}</Button>} />
            )}
          </div>
        )}
        <CodeDialog open={codeOpen} onOpenChange={setCodeOpen} kind="delivery" title="Deliver this order"
          confirmLabel="Delivered, cash collected" pending={deliver.isPending}
          onSubmit={(code) => deliver.mutateAsync({ id: o.id, action: "deliver", code })} />
      </motion.li>
    </MotionConfig>
  );
}
