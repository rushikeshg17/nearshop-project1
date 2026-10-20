"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Sheet, SheetContent, SheetFooter, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Button } from "@/components/ui/button";

export interface SearchFilterValues {
  inStock: boolean;
  fulfillment: string;
  minPrice: string;
  maxPrice: string;
}

/** The search filters sheet. Loaded by the search page on the first tap of its Filters button (P2.9: the
 * sheet, switch and toggle group are not first-load JavaScript); the page owns the button and the open state. */
export function SearchFiltersSheet({
  trigger,
  open,
  onOpenChange,
  values,
  setParam,
}: {
  trigger: React.ReactElement;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  values: SearchFilterValues;
  setParam: (updates: Record<string, string | null>) => void;
}) {
  const { inStock, fulfillment, minPrice, maxPrice } = values;
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetTrigger asChild>{trigger}</SheetTrigger>
      <SheetContent className="w-full sm:max-w-sm">
        <SheetHeader>
          <SheetTitle>Filters</SheetTitle>
        </SheetHeader>
        <div className="px-4">
          <div className="space-y-6">
            <div className="flex items-center justify-between gap-4">
              <Label htmlFor="instock" className="flex flex-col items-start gap-0.5">
                <span>In stock only</span>
                <span className="text-xs font-normal text-muted-foreground">Hide items shops have run out of</span>
              </Label>
              <Switch id="instock" checked={inStock} onCheckedChange={(v) => setParam({ stock: v ? "1" : null })} />
            </div>
            <div className="space-y-2">
              <Label>How you want it</Label>
              <ToggleGroup
                type="single"
                variant="outline"
                value={fulfillment || "any"}
                onValueChange={(v) => setParam({ fulfillment: v === "any" || !v ? null : v })}
                className="w-full"
              >
                <ToggleGroupItem value="any" className="flex-1">Any</ToggleGroupItem>
                <ToggleGroupItem value="pickup" className="flex-1">Pickup</ToggleGroupItem>
                <ToggleGroupItem value="delivery" className="flex-1">Delivery</ToggleGroupItem>
              </ToggleGroup>
            </div>
            <div className="space-y-2">
              <Label>Price range (Rs)</Label>
              <div className="flex items-center gap-2">
                <Input inputMode="numeric" placeholder="Min" defaultValue={minPrice} onBlur={(e) => setParam({ min: e.target.value.replace(/\D/g, "") })} aria-label="Minimum price" />
                <span className="text-muted-foreground">to</span>
                <Input inputMode="numeric" placeholder="Max" defaultValue={maxPrice} onBlur={(e) => setParam({ max: e.target.value.replace(/\D/g, "") })} aria-label="Maximum price" />
              </div>
            </div>
          </div>
        </div>
        <SheetFooter>
          <Button variant="ghost" onClick={() => setParam({ stock: null, fulfillment: null, min: null, max: null })}>
            Clear all
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}
