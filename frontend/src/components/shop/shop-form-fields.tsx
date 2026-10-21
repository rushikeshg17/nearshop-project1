"use client";

import { Crosshair, MapPin } from "lucide-react";
import { toast } from "sonner";

import { AppIcon } from "@/components/common/app-icon";
import { ShopMap } from "@/components/map/shop-map";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import type { KycType, Meta, ShopDetail } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface ShopFormState {
  name: string;
  tagline: string;
  description: string;
  category_slugs: string[];
  phone: string;
  address_line: string;
  locality: string;
  pincode: string;
  lat: number;
  lng: number;
  open_time: string;
  close_time: string;
  closed_on: string;
  offers_pickup: boolean;
  offers_delivery: boolean;
  delivery_radius_km: number;
  delivery_fee: number;
  free_delivery_above: string;
  hold_minutes: number;
}

const TIMES = Array.from({ length: 34 }, (_, i) => {
  const mins = 6 * 60 + i * 30;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  const h12 = h % 12 || 12;
  return `${h12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
});
const DAYS = ["None", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function hoursString(s: ShopFormState) {
  return `${s.open_time} - ${s.close_time}`;
}

export function parseHours(hours: string | null) {
  const m = hours?.match(/(\d{1,2}:\d{2} [AP]M)\s*-\s*(\d{1,2}:\d{2} [AP]M)/);
  return m ? { open_time: m[1], close_time: m[2] } : { open_time: "9:30 AM", close_time: "9:00 PM" };
}

type Update = <K extends keyof ShopFormState>(k: K, v: ShopFormState[K]) => void;

export function ShopBasicsFields({ s, set, meta }: { s: ShopFormState; set: Update; meta?: Meta }) {
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor="shop-name">Shop name</Label>
        <Input id="shop-name" value={s.name} onChange={(e) => set("name", e.target.value)} placeholder="e.g. Sri Lakshmi Hardware" className="h-11" />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="tagline">Tagline <span className="font-normal text-muted-foreground">(optional)</span></Label>
        <Input id="tagline" value={s.tagline} onChange={(e) => set("tagline", e.target.value)} placeholder="Tools, locks and fittings since 1994" />
      </div>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">What do you sell? <span className="font-normal text-muted-foreground">Up to 3</span></legend>
        <div className="flex flex-wrap gap-2">
          {meta?.categories.map((c) => {
            const on = s.category_slugs.includes(c.slug);
            return (
              <button
                key={c.slug}
                type="button"
                aria-pressed={on}
                onClick={() =>
                  set("category_slugs", on ? s.category_slugs.filter((x) => x !== c.slug) : s.category_slugs.length < 3 ? [...s.category_slugs, c.slug] : s.category_slugs)
                }
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition-colors",
                  on ? "border-primary bg-primary text-primary-foreground" : "hover:bg-accent",
                )}
              >
                <AppIcon name={c.icon} className="size-3.5" /> {c.name}
              </button>
            );
          })}
        </div>
      </fieldset>
      <div className="space-y-1.5">
        <Label htmlFor="shop-phone">Shop phone</Label>
        <Input id="shop-phone" inputMode="numeric" value={s.phone} onChange={(e) => set("phone", e.target.value.replace(/\D/g, "").slice(0, 10))} placeholder="10-digit mobile" />
      </div>
    </div>
  );
}

export function ShopLocationFields({ s, set, meta }: { s: ShopFormState; set: Update; meta?: Meta }) {
  function useGps() {
    navigator.geolocation?.getCurrentPosition(
      (p) => {
        set("lat", p.coords.latitude);
        set("lng", p.coords.longitude);
        toast.success("Pin moved to your current location");
      },
      () => toast.error("Couldn't get your location. Tap the map instead."),
      { enableHighAccuracy: true, timeout: 10_000 },
    );
  }
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor="addr">Street address</Label>
        <Input id="addr" value={s.address_line} onChange={(e) => set("address_line", e.target.value)} placeholder="Shop no., street, landmark" />
      </div>
      <div className="grid grid-cols-[1.4fr_1fr] gap-3">
        <div className="space-y-1.5">
          <Label>Area</Label>
          <Select
            value={s.locality}
            onValueChange={(v) => {
              const loc = meta?.city.localities.find((l) => l.name === v);
              set("locality", v);
              if (loc) {
                set("lat", loc.lat);
                set("lng", loc.lng);
                if (loc.pincode) set("pincode", loc.pincode);
              }
            }}
          >
            <SelectTrigger className="w-full"><SelectValue placeholder="Choose area" /></SelectTrigger>
            <SelectContent>
              {meta?.city.localities.map((l) => (
                <SelectItem key={l.name} value={l.name}>{l.name}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="pin">Pincode</Label>
          <Input id="pin" inputMode="numeric" value={s.pincode} onChange={(e) => set("pincode", e.target.value.replace(/\D/g, "").slice(0, 6))} />
        </div>
      </div>
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <Label className="flex items-center gap-1.5"><MapPin className="size-3.5" /> Exact shop location</Label>
          <Button type="button" variant="ghost" size="xs" onClick={useGps}><Crosshair /> Use GPS</Button>
        </div>
        <div className="h-52 overflow-hidden rounded-xl border">
          <ShopMap
            className="h-full"
            center={{ lat: s.lat, lng: s.lng }}
            zoom={15}
            fitToPoints={false}
            points={[{ id: 1, lat: s.lat, lng: s.lng, label: "Your shop", title: "Shop location" }]}
            selected={1}
            onMapClick={(p) => {
              set("lat", p.lat);
              set("lng", p.lng);
            }}
          />
        </div>
        <p className="text-xs text-muted-foreground">Tap the map to place the pin on your shop&apos;s door. Customers use this for directions and distance.</p>
      </div>
    </div>
  );
}

export function ShopOperationsFields({ s, set }: { s: ShopFormState; set: Update }) {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label>Opens</Label>
          <Select value={s.open_time} onValueChange={(v) => set("open_time", v)}>
            <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>{TIMES.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Closes</Label>
          <Select value={s.close_time} onValueChange={(v) => set("close_time", v)}>
            <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>{TIMES.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Weekly holiday</Label>
          <Select value={s.closed_on || "None"} onValueChange={(v) => set("closed_on", v === "None" ? "" : v)}>
            <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>{DAYS.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Hold reservations for</Label>
          <Select value={String(s.hold_minutes)} onValueChange={(v) => set("hold_minutes", Number(v))}>
            <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>
              {[15, 30, 45, 60, 120].map((m) => <SelectItem key={m} value={String(m)}>{m} minutes</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="rounded-2xl border p-4">
        <div className="flex items-center justify-between gap-4">
          <Label htmlFor="delivery" className="flex flex-col items-start gap-0.5">
            <span>I deliver myself</span>
            <span className="text-xs font-normal text-muted-foreground">You or your staff deliver; customers pay cash on delivery.</span>
          </Label>
          <Switch id="delivery" checked={s.offers_delivery} onCheckedChange={(v) => set("offers_delivery", v)} />
        </div>
        {s.offers_delivery && (
          <div className="mt-5 space-y-5">
            <div>
              <div className="mb-2 flex justify-between text-sm">
                <span>Delivery radius</span>
                <span className="font-medium tabular">{s.delivery_radius_km} km</span>
              </div>
              <Slider min={1} max={15} step={0.5} value={[s.delivery_radius_km]} onValueChange={([v]) => set("delivery_radius_km", v)} aria-label="Delivery radius" />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label htmlFor="fee">Delivery fee (Rs)</Label>
                <Input id="fee" inputMode="numeric" value={s.delivery_fee} onChange={(e) => set("delivery_fee", Number(e.target.value.replace(/\D/g, "")) || 0)} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="free">Free above (Rs)</Label>
                <Input id="free" inputMode="numeric" placeholder="Optional" value={s.free_delivery_above} onChange={(e) => set("free_delivery_above", e.target.value.replace(/\D/g, ""))} />
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export function shopPayload(s: ShopFormState) {
  return {
    name: s.name.trim(),
    tagline: s.tagline.trim() || null,
    description: s.description.trim() || null,
    category_slugs: s.category_slugs,
    phone: s.phone,
    address_line: s.address_line.trim(),
    locality: s.locality || null,
    pincode: s.pincode || null,
    lat: s.lat,
    lng: s.lng,
    opening_hours: hoursString(s),
    closed_on: s.closed_on || null,
    offers_delivery: s.offers_delivery,
    delivery_radius_km: s.offers_delivery ? s.delivery_radius_km : 0,
    delivery_fee: s.offers_delivery ? s.delivery_fee : 0,
    free_delivery_above: s.offers_delivery && s.free_delivery_above ? Number(s.free_delivery_above) : null,
    hold_minutes: s.hold_minutes,
  };
}

// ---------------------------------------------------------------- shop verification (P3.4; rules from P3.3)

/** The fields whose change sends a verified shop back to review (backend/app/api/routes/owner.py, REVIEW_FIELDS). */
export function reviewFieldsChanged(initial: ShopDetail, s: ShopFormState): boolean {
  const p = shopPayload(s);
  return (
    p.address_line !== initial.address_line ||
    (p.locality ?? null) !== (initial.locality ?? null) ||
    (p.pincode ?? null) !== (initial.pincode ?? null) ||
    (p.phone || null) !== (initial.phone || null) ||
    p.lat !== initial.lat ||
    p.lng !== initial.lng
  );
}

export const KYC_TYPES: { value: KycType; label: string; hint: string; example: string }[] = [
  { value: "gstin", label: "GSTIN", hint: "Your 15-character GST number.", example: "27AAPFU0939F1ZV" },
  { value: "udyam", label: "Udyam registration", hint: "From your Udyam (MSME) certificate.", example: "UDYAM-KR-03-0012345" },
  {
    value: "trade_licence",
    label: "Trade licence",
    hint: "Trade licence or Shop and Establishment registration number.",
    example: "MCC/TL/2025/00123",
  },
];

const GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";
const GSTIN_PATTERN = /^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$/;
const UDYAM_PATTERN = /^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$/;
const TRADE_LICENCE_PATTERN = /^[A-Z0-9][A-Z0-9 /.-]{2,38}[A-Z0-9]$/;
const AADHAAR_LIKE = /(?<![0-9])[0-9]{12}(?![0-9])/;

function gstinCheckChar(first14: string) {
  let total = 0;
  for (let i = 0; i < first14.length; i++) {
    const product = GSTIN_CHARS.indexOf(first14[i]) * (i % 2 ? 2 : 1);
    total += Math.floor(product / 36) + (product % 36);
  }
  return GSTIN_CHARS[(36 - (total % 36)) % 36];
}

export function normalizeKycNumber(number: string) {
  return number.split(/\s+/).filter(Boolean).join(" ").toUpperCase();
}

/** The same checks as the API (backend/app/models/core.py, validate_kyc_number), so mistakes show while typing.
 * Returns an error message, or null when the number is acceptable. */
export function kycNumberError(type: KycType, number: string): string | null {
  const value = normalizeKycNumber(number);
  if (!value) return "Enter the number";
  if (AADHAAR_LIKE.test(value.replace(/[\s.-]/g, ""))) {
    return "That looks like an Aadhaar number. NearShop never collects Aadhaar: enter your GSTIN, Udyam number or trade licence number";
  }
  if (type === "gstin") {
    if (value.length !== 15 || !GSTIN_PATTERN.test(value)) return "A GSTIN has 15 characters, like 27AAPFU0939F1ZV";
    if (gstinCheckChar(value.slice(0, 14)) !== value[14]) return "That GSTIN's last character does not match: check it for a typing mistake";
  } else if (type === "udyam") {
    if (!UDYAM_PATTERN.test(value)) return "A Udyam number looks like UDYAM-KR-03-0012345";
  } else if (!(value.length >= 4 && value.length <= 40 && TRADE_LICENCE_PATTERN.test(value))) {
    return "A trade licence number has 4 to 40 letters, digits, spaces, slashes, dots or hyphens";
  }
  return null;
}

export interface KycFormState {
  kyc_type: KycType;
  kyc_number: string;
  document_photo: File | null;
  storefront_photo: File | null;
}

type KycUpdate = <K extends keyof KycFormState>(k: K, v: KycFormState[K]) => void;

const PHOTO_TYPES = "image/jpeg,image/png,image/webp";

/** Evidence type, number (with inline format errors once the field is left or a submit was tried), document photo and storefront photo. */
export function KycFields({
  s,
  set,
  numberTouched,
  attempted,
  onBlurNumber,
}: {
  s: KycFormState;
  set: KycUpdate;
  numberTouched: boolean;
  attempted: boolean;
  onBlurNumber: () => void;
}) {
  const type = KYC_TYPES.find((t) => t.value === s.kyc_type) ?? KYC_TYPES[0];
  const numberError = numberTouched || attempted ? kycNumberError(s.kyc_type, s.kyc_number) : null;
  const documentRequired = s.kyc_type === "trade_licence";
  return (
    <div className="space-y-4">
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Evidence type</legend>
        <RadioGroup value={s.kyc_type} onValueChange={(v) => set("kyc_type", v as KycType)} className="gap-3">
          {KYC_TYPES.map((t) => (
            <div key={t.value} className="flex items-start gap-2.5">
              <RadioGroupItem id={`kyc-${t.value}`} value={t.value} className="mt-0.5" />
              <Label htmlFor={`kyc-${t.value}`} className="flex flex-col items-start gap-0.5">
                <span>{t.label}</span>
                <span className="text-xs font-normal text-muted-foreground">{t.hint}</span>
              </Label>
            </div>
          ))}
        </RadioGroup>
      </fieldset>
      <div className="space-y-1.5">
        <Label htmlFor="kyc-number">{type.label} number</Label>
        <Input
          id="kyc-number"
          value={s.kyc_number}
          onChange={(e) => set("kyc_number", e.target.value.slice(0, 60))}
          onBlur={onBlurNumber}
          placeholder={type.example}
          autoComplete="off"
          aria-invalid={numberError ? true : undefined}
          aria-describedby={numberError ? "kyc-number-error" : "kyc-number-hint"}
        />
        {numberError ? (
          <p id="kyc-number-error" className="text-xs text-destructive">{numberError}</p>
        ) : (
          <p id="kyc-number-hint" className="text-xs text-muted-foreground">Never enter an Aadhaar number: NearShop does not collect it.</p>
        )}
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="kyc-document">
          Photo of the {documentRequired ? "licence or registration" : "certificate"}{" "}
          {!documentRequired && <span className="font-normal text-muted-foreground">(optional)</span>}
        </Label>
        <Input id="kyc-document" type="file" accept={PHOTO_TYPES} onChange={(e) => set("document_photo", e.target.files?.[0] ?? null)} />
        {attempted && documentRequired && !s.document_photo && (
          <p className="text-xs text-destructive">Add a photo of your trade licence or Shop and Establishment registration</p>
        )}
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="kyc-storefront">Storefront photo showing your signboard</Label>
        <Input id="kyc-storefront" type="file" accept={PHOTO_TYPES} onChange={(e) => set("storefront_photo", e.target.files?.[0] ?? null)} />
        {attempted && !s.storefront_photo && <p className="text-xs text-destructive">Add a photo of your shopfront with the signboard</p>}
      </div>
    </div>
  );
}

export function kycFormReady(s: KycFormState) {
  return !kycNumberError(s.kyc_type, s.kyc_number) && !!s.storefront_photo && (s.kyc_type !== "trade_licence" || !!s.document_photo);
}
