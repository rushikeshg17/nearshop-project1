"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, PauseCircle } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { safeNext } from "@/app/(auth)/login/login-form";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useMeta } from "@/hooks/use-session";
import { api, ApiError, errorMessage } from "@/lib/api";
import type { User } from "@/lib/types";

/** Shown on both sign-up screens while /api/meta reports signups_open: false. */
export function SignupsPaused({ signInHref = "/login" }: { signInHref?: string }) {
  return (
    <div role="status">
      <div className="mb-5 grid size-12 place-items-center rounded-2xl bg-warning-soft text-warning-ink">
        <PauseCircle className="size-6" aria-hidden />
      </div>
      <h1 className="text-3xl font-semibold">Sign-ups are paused while we prepare for launch</h1>
      <p className="mt-3 text-sm text-muted-foreground">
        New accounts cannot be created right now. You can still search what nearby shops have in stock.
      </p>
      <div className="mt-8 flex flex-wrap gap-3">
        <Button asChild size="lg" className="h-11">
          <Link href="/search">Search nearby</Link>
        </Button>
        <Button asChild variant="outline" size="lg" className="h-11">
          <Link href={signInHref}>Sign in</Link>
        </Button>
      </div>
    </div>
  );
}

export function RegisterForm() {
  const { data: meta } = useMeta();
  if (meta?.signups_open === false) return <SignupsPaused />;
  return <OpenRegisterForm />;
}

function OpenRegisterForm() {
  const router = useRouter();
  const params = useSearchParams();
  const qc = useQueryClient();
  const [form, setForm] = useState({ name: "", email: "", phone: "", password: "" });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const register = useMutation({
    mutationFn: () => api.post<User>("/auth/register", { ...form, phone: form.phone || null }),
    meta: { silent: true },
    onSuccess: (user) => {
      qc.setQueryData(["me"], user);
      router.replace(safeNext(params.get("next")) ?? "/search");
    },
  });
  const fieldError = (f: string) => register.error instanceof ApiError && register.error.field === f;
  const strong = form.password.length >= 8;

  return (
    <div>
      <h1 className="text-3xl font-semibold">Create your account</h1>
      <p className="mt-2 text-sm text-muted-foreground">Reserve items at nearby shops and track pickups in one place.</p>
      <form
        className="mt-8 space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          register.mutate();
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor="name">Full name</Label>
          <Input id="name" autoComplete="name" required minLength={2} value={form.name} onChange={set("name")} className="h-11" aria-invalid={fieldError("name")} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="email">Email</Label>
          <Input id="email" type="email" autoComplete="email" required value={form.email} onChange={set("email")} className="h-11" aria-invalid={fieldError("email")} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="phone">
            Mobile number <span className="font-normal text-muted-foreground">(optional, for delivery)</span>
          </Label>
          <Input
            id="phone"
            inputMode="numeric"
            autoComplete="tel-national"
            value={form.phone}
            onChange={(e) => setForm((f) => ({ ...f, phone: e.target.value.replace(/\D/g, "").slice(0, 10) }))}
            className="h-11"
            aria-invalid={fieldError("phone")}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="password">Password</Label>
          <Input id="password" type="password" autoComplete="new-password" required minLength={8} value={form.password} onChange={set("password")} className="h-11" aria-invalid={fieldError("password")} aria-describedby="pw-hint" />
          <p id="pw-hint" className={strong ? "text-xs text-success-ink" : "text-xs text-muted-foreground"}>At least 8 characters</p>
        </div>
        {register.isError && (
          <p role="alert" className="rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive">{errorMessage(register.error)}</p>
        )}
        <Button type="submit" size="lg" className="h-11 w-full" disabled={register.isPending}>
          {register.isPending && <Loader2 className="animate-spin" />} Create account
        </Button>
      </form>
      <p className="mt-6 text-sm text-muted-foreground">
        Already have an account? <Link href="/login" className="font-medium text-foreground underline underline-offset-4">Sign in</Link>
      </p>
      <p className="mt-2 text-sm text-muted-foreground">
        Own a shop? <Link href="/register/shop" className="font-medium text-foreground underline underline-offset-4">List it on NearShop</Link>
      </p>
    </div>
  );
}
