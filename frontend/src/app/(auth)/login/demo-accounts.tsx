"use client";

// Demo-account shortcuts for a demo deployment only. login-form.tsx loads this module only when the
// build-time flag NEXT_PUBLIC_NEARSHOP_DEMO_MODE is "true"; otherwise the build drops it entirely.
const DEMO = [
  { label: "Customer", email: "priya@nearshop.demo" },
  { label: "Shop owner", email: "owner@nearshop.demo" },
  { label: "Admin", email: "admin@nearshop.demo" },
];

export default function DemoAccounts({ onPick }: { onPick: (email: string) => void }) {
  return (
    <div className="mt-10 rounded-2xl border border-dashed p-4">
      <p className="text-xs font-medium text-muted-foreground">Demo accounts</p>
      <div className="mt-2 flex flex-wrap gap-2">
        {DEMO.map((d) => (
          <button
            key={d.email}
            type="button"
            onClick={() => onPick(d.email)}
            className="rounded-full border bg-card px-3 py-1 text-xs transition-colors hover:bg-accent"
          >
            {d.label}
          </button>
        ))}
      </div>
    </div>
  );
}
