"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, Loader2, ShieldCheck } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { homeFor } from "@/hooks/use-session";
import { ApiError, api, errorMessage } from "@/lib/api";
import type { User } from "@/lib/types";

// Build-time flag, off by default (next.config.ts gives it the value "false"). Only a demo
// deployment built with NEXT_PUBLIC_NEARSHOP_DEMO_MODE=true shows the demo-account shortcuts;
// in any other build this condition is the constant false and the demo module is left out.
const DemoAccounts =
  process.env.NEXT_PUBLIC_NEARSHOP_DEMO_MODE === "true" ? dynamic(() => import("./demo-accounts")) : null;

export function safeNext(next: string | null) {
  // Only allow same-site relative paths to avoid open redirects.
  return next && next.startsWith("/") && !next.startsWith("//") ? next : null;
}

export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  // Admins confirm the password with a second factor: the API answers totp_required, and the form then
  // asks for an authenticator code (or a recovery code) and sends it with the same email and password.
  const [needsCode, setNeedsCode] = useState(false);
  const [code, setCode] = useState("");

  const changeEmail = (value: string) => {
    setEmail(value);
    setNeedsCode(false);
    setCode("");
  };

  const login = useMutation({
    mutationFn: () => api.post<User>("/auth/login", needsCode ? { email, password, code: code.trim() } : { email, password }),
    meta: { silent: true },
    onError: (error) => {
      if (error instanceof ApiError && error.code === "totp_required") setNeedsCode(true);
    },
    onSuccess: (user) => {
      qc.setQueryData(["me"], user);
      qc.invalidateQueries({ refetchType: "none" });
      const next = safeNext(params.get("next"));
      const allowed = next && !(next.startsWith("/shop") && user.role !== "owner") && !(next.startsWith("/admin") && user.role !== "admin");
      router.replace(allowed ? next : homeFor(user));
    },
  });

  return (
    <div>
      <h1 className="text-3xl font-semibold">Welcome back</h1>
      <p className="mt-2 text-sm text-muted-foreground">Sign in to reserve items and track your pickups.</p>

      <form
        className="mt-8 space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          login.mutate();
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor="email">Email</Label>
          <Input id="email" type="email" autoComplete="email" required value={email} onChange={(e) => changeEmail(e.target.value)} className="h-11" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="password">Password</Label>
          <div className="relative">
            <Input
              id="password"
              type={show ? "text" : "password"}
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
                setNeedsCode(false);
                setCode("");
              }}
              className="h-11 pr-11"
            />
            <button
              type="button"
              onClick={() => setShow((v) => !v)}
              className="absolute top-1/2 right-2 grid size-8 -translate-y-1/2 place-items-center rounded-md text-muted-foreground hover:bg-muted"
              aria-label={show ? "Hide password" : "Show password"}
            >
              {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
            </button>
          </div>
        </div>
        {needsCode && (
          <div className="space-y-1.5 rounded-lg border p-3">
            <Label htmlFor="code" className="flex items-center gap-1.5">
              <ShieldCheck className="size-4" aria-hidden /> Authentication code
            </Label>
            <p id="code-hint" className="text-xs text-muted-foreground">
              Enter the 6-digit code from your authenticator app, or one of your recovery codes.
            </p>
            <Input
              id="code"
              autoComplete="one-time-code"
              autoCapitalize="off"
              spellCheck={false}
              autoFocus
              required
              maxLength={32}
              aria-describedby="code-hint"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className="h-11 font-mono tracking-widest"
            />
          </div>
        )}
        {login.isError && !(login.error instanceof ApiError && login.error.code === "totp_required") && (
          <p role="alert" className="rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {errorMessage(login.error)}
          </p>
        )}
        <Button type="submit" size="lg" className="h-11 w-full" disabled={login.isPending}>
          {login.isPending && <Loader2 className="animate-spin" />} {needsCode ? "Verify and sign in" : "Sign in"}
        </Button>
      </form>

      <p className="mt-6 text-sm text-muted-foreground">
        New here?{" "}
        <Link href={`/register${params.get("next") ? `?next=${encodeURIComponent(params.get("next")!)}` : ""}`} className="font-medium text-foreground underline underline-offset-4">
          Create an account
        </Link>{" "}
        or{" "}
        <Link href="/register/shop" className="font-medium text-foreground underline underline-offset-4">list your shop</Link>
      </p>

      {DemoAccounts && <DemoAccounts onPick={changeEmail} />}
    </div>
  );
}
