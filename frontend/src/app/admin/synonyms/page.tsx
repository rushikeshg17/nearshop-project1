"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, Search } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { toast } from "sonner";

import { ErrorState, ListSkeleton } from "@/components/common/states";
import { PageTitle } from "@/components/dashboard/dashboard-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useDebounce } from "@/hooks/use-debounce";
import { api, errorMessage } from "@/lib/api";

/** GET /api/admin/synonyms (P5.4). */
type SynonymEntry = {
  id: number;
  term: string;
  expansions: string[];
  languages: string[];
  active: boolean;
  created_by: number | null;
  created_at: string | null;
  updated_at: string | null;
};
type SynonymList = { entries: SynonymEntry[]; total: number; active: number; limit: number };

/** POST /api/admin/synonyms/preview: the top 10 at the city centre, without the entry and with it. */
type PreviewSide = { total: number; expanded_terms: string[]; results: { key: string; name: string; category: string; relevance: number; shop_count: number }[] };
type Preview = { query: string; term: string; expansions: string[]; without: PreviewSide; with: PreviewSide };

const MAX_EXPANSIONS = 20;
const MAX_LENGTH = 40;

const splitList = (value: string) => value.split(",").map((v) => v.trim()).filter(Boolean);

type Draft = { id: number | null; term: string; expansions: string; languages: string };
const emptyDraft = (term = ""): Draft => ({ id: null, term, expansions: "", languages: "" });

function PreviewColumn({ title, side }: { title: string; side: PreviewSide }) {
  return (
    <div>
      <h3 className="text-sm font-medium">{title} <span className="font-normal text-muted-foreground">· {side.total} result{side.total === 1 ? "" : "s"}</span></h3>
      <p className="mt-0.5 text-xs text-muted-foreground">{side.expanded_terms.length ? `Also searched: ${side.expanded_terms.join(", ")}` : "No synonyms applied"}</p>
      {side.results.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">No results.</p>
      ) : (
        <ol className="mt-3 space-y-1.5">
          {side.results.map((r, i) => (
            <li key={r.key} className="flex gap-2 text-sm">
              <span className="w-5 shrink-0 text-muted-foreground tabular">{i + 1}</span>
              <span className="min-w-0 flex-1 truncate">{r.name}</span>
              <span className="shrink-0 text-xs text-muted-foreground tabular">{r.shop_count} shop{r.shop_count === 1 ? "" : "s"}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function SynonymsView() {
  const qc = useQueryClient();
  const params = useSearchParams();
  const [draft, setDraft] = useState<Draft>(() => emptyDraft(params.get("term") ?? ""));
  const [preview, setPreview] = useState<Preview | null>(null);
  const [text, setText] = useState("");
  const q = useDebounce(text, 250);
  const list = useQuery({
    queryKey: ["admin", "synonyms", q],
    queryFn: () => api.get<SynonymList>("/admin/synonyms", { q }),
    placeholderData: keepPreviousData,
  });

  const expansions = splitList(draft.expansions);
  const languages = splitList(draft.languages);
  const problem =
    !draft.term.trim() ? "Enter the word customers type." :
    expansions.length === 0 ? "Add at least one expansion." :
    expansions.length > MAX_EXPANSIONS ? `At most ${MAX_EXPANSIONS} expansions.` :
    expansions.some((e) => e.length > MAX_LENGTH) ? `Each expansion is at most ${MAX_LENGTH} characters.` :
    expansions.some((e) => e.toLowerCase() === draft.term.trim().toLowerCase()) ? "A word cannot be a synonym of itself." :
    null;

  const update = (patch: Partial<Draft>) => {
    setDraft((d) => ({ ...d, ...patch }));
    setPreview(null);
  };

  const runPreview = useMutation({
    mutationFn: () => api.post<Preview>("/admin/synonyms/preview", { term: draft.term, expansions, entry_id: draft.id }),
    onSuccess: setPreview,
    onError: (e) => toast.error(errorMessage(e)),
  });
  const save = useMutation({
    mutationFn: () =>
      draft.id === null
        ? api.post<SynonymEntry>("/admin/synonyms", { term: draft.term, expansions, languages })
        : api.patch<SynonymEntry>(`/admin/synonyms/${draft.id}`, { term: draft.term, expansions, languages }),
    onSuccess: (entry) => {
      qc.invalidateQueries({ queryKey: ["admin", "synonyms"] });
      toast.success(draft.id === null ? `Added "${entry.term}"` : `Saved "${entry.term}"`);
      setDraft(emptyDraft());
      setPreview(null);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const setActive = useMutation({
    mutationFn: ({ id, active }: { id: number; active: boolean }) => api.patch<SynonymEntry>(`/admin/synonyms/${id}`, { active }),
    onSuccess: (entry) => {
      qc.invalidateQueries({ queryKey: ["admin", "synonyms"] });
      toast.success(entry.active ? `"${entry.term}" is active again` : `"${entry.term}" deactivated`);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const edit = (e: SynonymEntry) => {
    setDraft({ id: e.id, term: e.term, expansions: e.expansions.join(", "), languages: e.languages.join(", ") });
    setPreview(null);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const counts = list.data;
  return (
    <>
      <PageTitle
        title="Synonyms"
        description="Words customers type, and the extra words search also looks for. Changes reach search within 30 seconds, without a deploy."
      />

      <section className="rounded-2xl border bg-card p-5" aria-labelledby="synonym-form-heading">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="synonym-form-heading" className="font-semibold">{draft.id === null ? "Add an entry" : `Edit "${draft.term}"`}</h2>
          {counts && <span className="text-xs text-muted-foreground tabular">{counts.active} active · {counts.total} of {counts.limit} entries</span>}
        </div>
        <form
          className="mt-4 grid gap-4 md:grid-cols-[1fr_2fr_1fr]"
          onSubmit={(e) => {
            e.preventDefault();
            if (!problem) save.mutate();
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor="syn-term">Customers type</Label>
            <Input id="syn-term" value={draft.term} maxLength={MAX_LENGTH} onChange={(e) => update({ term: e.target.value })} placeholder="chappal" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="syn-expansions">Also search for (comma-separated)</Label>
            <Input id="syn-expansions" value={draft.expansions} onChange={(e) => update({ expansions: e.target.value })} placeholder="slipper, sandal, flip flop" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="syn-languages">Languages (optional)</Label>
            <Input id="syn-languages" value={draft.languages} onChange={(e) => update({ languages: e.target.value })} placeholder="en, kn, hi-Latn" />
          </div>
          <div className="flex flex-wrap items-center gap-2 md:col-span-3">
            <Button type="button" variant="outline" disabled={!!problem || runPreview.isPending} onClick={() => runPreview.mutate()}>
              <Eye className="size-4" /> Preview results
            </Button>
            <Button type="submit" disabled={!!problem || save.isPending}>{draft.id === null ? "Add synonym" : "Save changes"}</Button>
            {(draft.id !== null || draft.term) && (
              <Button type="button" variant="ghost" onClick={() => { setDraft(emptyDraft()); setPreview(null); }}>Cancel</Button>
            )}
            {problem && draft.term && <p className="text-sm text-muted-foreground" role="status">{problem}</p>}
          </div>
        </form>
        {preview && (
          <div className="mt-5 border-t pt-4">
            <p className="mb-3 text-xs text-muted-foreground">Top 10 for &ldquo;{preview.query}&rdquo; at the city centre.</p>
            <div className="grid gap-6 md:grid-cols-2">
              <PreviewColumn title="Without this entry" side={preview.without} />
              <PreviewColumn title="With this entry" side={preview.with} />
            </div>
          </div>
        )}
      </section>

      <div className="relative mt-6 mb-4 max-w-sm">
        <Search className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="Find a word" aria-label="Find a synonym entry" className="h-9 pl-9" />
      </div>
      {list.isError ? (
        <ErrorState error={list.error} onRetry={() => list.refetch()} />
      ) : !list.data ? (
        <ListSkeleton />
      ) : list.data.entries.length === 0 ? (
        <p className="rounded-2xl border bg-card p-5 text-sm text-muted-foreground">No entries match.</p>
      ) : (
        <div className="overflow-x-auto rounded-2xl border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Customers type</TableHead>
                <TableHead>Also search for</TableHead>
                <TableHead className="hidden md:table-cell">Languages</TableHead>
                <TableHead>Active</TableHead>
                <TableHead><span className="sr-only">Edit</span></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {list.data.entries.map((e) => (
                <TableRow key={e.id} className={e.active ? undefined : "opacity-60"}>
                  <TableCell className="font-medium">{e.term}</TableCell>
                  <TableCell className="max-w-md whitespace-normal">{e.expansions.join(", ")}</TableCell>
                  <TableCell className="hidden md:table-cell">
                    <div className="flex flex-wrap gap-1">{e.languages.map((l) => <Badge key={l} variant="outline">{l}</Badge>)}</div>
                  </TableCell>
                  <TableCell>
                    <Switch checked={e.active} disabled={setActive.isPending} onCheckedChange={(v) => setActive.mutate({ id: e.id, active: v })} aria-label={`Active: ${e.term}`} />
                  </TableCell>
                  <TableCell className="text-right">
                    <Button type="button" size="sm" variant="ghost" onClick={() => edit(e)}>Edit</Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </>
  );
}

export default function AdminSynonymsPage() {
  // useSearchParams (the ?term= an "Add synonym" link pre-fills) needs a Suspense boundary for prerendering.
  return (
    <Suspense fallback={<ListSkeleton />}>
      <SynonymsView />
    </Suspense>
  );
}
