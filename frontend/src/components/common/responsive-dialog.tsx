"use client";

import { useRef, useSyncExternalStore } from "react";

import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Drawer, DrawerContent, DrawerDescription, DrawerFooter, DrawerHeader, DrawerTitle } from "@/components/ui/drawer";

function subscribe(cb: () => void) {
  const mq = window.matchMedia("(min-width: 768px)");
  mq.addEventListener("change", cb);
  return () => mq.removeEventListener("change", cb);
}

export function useIsDesktop() {
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia("(min-width: 768px)").matches,
    () => true,
  );
}

/** Centred dialog on desktop, bottom sheet on phones (thumb-reachable actions). */
export function ResponsiveDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  const desktop = useIsDesktop();
  const drawerRef = useRef<HTMLDivElement>(null);
  if (desktop) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            {description && <DialogDescription>{description}</DialogDescription>}
          </DialogHeader>
          {children}
          {footer && <DialogFooter>{footer}</DialogFooter>}
        </DialogContent>
      </Dialog>
    );
  }
  return (
    <Drawer open={open} onOpenChange={onOpenChange}>
      {/* The drawer takes focus when it opens, so keyboard and screen-reader users are inside it (P2.10). It focuses
          itself rather than its first field, so a phone's on-screen keyboard does not pop up over it. */}
      <DrawerContent
        ref={drawerRef}
        className="outline-none"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          drawerRef.current?.focus();
        }}
      >
        <DrawerHeader className="text-left">
          <DrawerTitle>{title}</DrawerTitle>
          {description && <DrawerDescription>{description}</DrawerDescription>}
        </DrawerHeader>
        <div className="px-4">{children}</div>
        {footer && <DrawerFooter className="pb-safe">{footer}</DrawerFooter>}
      </DrawerContent>
    </Drawer>
  );
}
