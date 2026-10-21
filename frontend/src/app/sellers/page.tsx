"use client";
import { useEffect, useState } from "react";
import useSWR from "swr";
import { toast } from "sonner";
import { AlertTriangle, CheckCircle2, CircleDashed, ExternalLink, FileUp, Link2, MessageSquareQuote, MoreHorizontal, Pause, Play, Plus, RefreshCw, ScanSearch, ShieldQuestion, Trash2, XCircle } from "lucide-react";
import { api, type Job, type Seller } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, EmptyState, ErrorState, Skeleton, Spinner } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogTrigger } from "@/components/ui/dialog";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AddEvidenceDialog } from "@/components/product/evidence";
import { useJobWatcher } from "@/components/job-watcher";
import { ScanPanel } from "@/components/scan-panel";
import * as DM from "@radix-ui/react-dropdown-menu";
import { cn, timeAgo, titleCase } from "@/lib/utils";

const STATUS_ICON: Record<string, React.ReactNode> = {
  succeeded: <CheckCircle2 className="size-4 text-positive" />,
  partial: <AlertTriangle className="size-4 text-caution" />,
  blocked: <XCircle className="size-4 text-destructive" />,
  failed: <XCircle className="size-4 text-destructive" />,
  cancelled: <XCircle className="size-4 text-muted-foreground" />,
  running: <Spinner />,
};

function SellerDialog({ seller, onSaved, trigger }: { seller?: Seller; onSaved: () => void; trigger: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", base_url: "", platform: "yupoo", priority: 3, notes: "", scan_interval_hours: 24 });
  useEffect(() => {
    if (open)
      setForm(
        seller
          ? { name: seller.name, base_url: seller.base_url ?? "", platform: seller.platform, priority: seller.priority, notes: seller.notes ?? "", scan_interval_hours: seller.scan_interval_hours }
          : { name: "", base_url: "", platform: "yupoo", priority: 3, notes: "", scan_interval_hours: 24 },
      );
  }, [open, seller]);
  const save = async () => {
    try {
      const body = { ...form, base_url: form.base_url || null };
      if (seller) await api.patch(`/api/sellers/${seller.id}`, body);
      else await api.post("/api/sellers", body);
      toast.success(seller ? "Seller updated" : "Seller added");
      setOpen(false);
      onSaved();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not save");
    }
  };
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent title={seller ? `Edit ${seller.name}` : "Add a seller"} description="New sellers start as unassessed — popularity is never treated as proof of quality.">
        <div className="grid gap-4">
          <Field label="Name">
            <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="e.g. MadeByKungFu" />
          </Field>
          <Field label="Catalogue URL" hint="Yupoo: https://<name>.x.yupoo.com · Shopify stores: the shop's home page">
            <Input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="https://seller.x.yupoo.com" />
          </Field>
          <div className="grid grid-cols-3 gap-3">
            <Field label="Platform">
              <Select value={form.platform} onChange={(e) => setForm({ ...form, platform: e.target.value })}>
                <option value="yupoo">Yupoo</option>
                <option value="shopify">Shopify store</option>
                <option value="website">Other website</option>
                <option value="weidian">Weidian (links only)</option>
                <option value="manual">Manual only</option>
              </Select>
            </Field>
            <Field label="Priority">
              <Select value={form.priority} onChange={(e) => setForm({ ...form, priority: Number(e.target.value) })}>
                {[1, 2, 3, 4, 5].map((p) => (
                  <option key={p} value={p}>
                    {p === 1 ? "1 · highest" : p === 5 ? "5 · lowest" : p}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Refresh (h)">
              <Input type="number" min={1} value={form.scan_interval_hours} onChange={(e) => setForm({ ...form, scan_interval_hours: Number(e.target.value) })} />
            </Field>
          </div>
          <Field label="Notes">
            <Textarea value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} rows={3} />
          </Field>
          <Button onClick={save} disabled={!form.name}>
            Save
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function SellerRow({ s, refresh }: { s: Seller; refresh: () => void }) {
  const watch = useJobWatcher();
  const scan = async (mode: "incremental" | "full") => {
    try {
      const r = await api.post<{ job_id: number }>(`/api/sellers/${s.id}/scan`, { mode });
      watch(r.job_id, `Scanning ${s.name}`, () => refresh());
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Cannot scan");
    }
  };
  const research = async () => {
    const r = await api.post<{ job_id: number }>("/api/research/community-search", { query: s.name, seller_id: s.id });
    watch(r.job_id, `Searching community posts for ${s.name}`, () => refresh());
  };
  return (
    <Card className="p-4 md:p-5" data-testid="seller-row">
      <div className="flex flex-wrap items-start gap-x-6 gap-y-3">
        <div className="min-w-0 flex-1 basis-56">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-serif text-2xl">{s.name}</h3>
            <Badge variant="outline">{titleCase(s.platform)}</Badge>
            {s.status === "paused" && <Badge variant="muted">Paused</Badge>}
            <Badge variant={s.reputation_status === "trusted" ? "positive" : s.reputation_status === "concerns" ? "destructive" : "muted"}>
              <ShieldQuestion className="size-3" /> {s.reputation_status}
            </Badge>
            <span className="text-xs text-muted-foreground">priority {s.priority}</span>
          </div>
          {s.base_url && (
            <a href={s.base_url} target="_blank" rel="noreferrer" className="mt-1 inline-flex items-center gap-1 text-xs text-muted-foreground hover:underline">
              {s.base_url.replace(/^https?:\/\//, "")} <ExternalLink className="size-3" />
            </a>
          )}
        </div>
        <dl className="grid grid-cols-3 gap-x-6 text-sm sm:grid-cols-4">
          <div>
            <dt className="label">Scanned</dt>
            <dd className="mt-1 tabular-nums">{s.products.scraped}</dd>
          </div>
          <div>
            <dt className="label">Imported</dt>
            <dd className="mt-1 tabular-nums">{s.products.manual + s.products.seed}</dd>
          </div>
          <div>
            <dt className="label">Photos</dt>
            <dd className="mt-1 tabular-nums">{s.products.images_downloaded}</dd>
          </div>
          <div className="hidden sm:block">
            <dt className="label">New</dt>
            <dd className="mt-1 tabular-nums">{s.products.new}</dd>
          </div>
        </dl>
        <div className="flex items-center gap-1.5">
          <Button size="sm" variant="outline" onClick={() => scan("incremental")} disabled={!s.base_url || s.platform === "manual"} data-testid="scan-seller">
            <RefreshCw /> Scan
          </Button>
          <DM.Root>
            <DM.Trigger asChild>
              <Button size="icon" variant="ghost" className="size-8" aria-label="More actions">
                <MoreHorizontal />
              </Button>
            </DM.Trigger>
            <DM.Portal>
              <DM.Content align="end" className="z-50 min-w-[12rem] rounded-xl border bg-popover p-1 text-sm shadow-xl animate-fade-up">
                <DM.Item className="cursor-pointer rounded-lg px-3 py-2 outline-none data-[highlighted]:bg-secondary" onSelect={() => scan("full")}>
                  Full rescan (detect removals)
                </DM.Item>
                <DM.Item
                  className="cursor-pointer rounded-lg px-3 py-2 outline-none data-[highlighted]:bg-secondary"
                  onSelect={async () => {
                    await api.patch(`/api/sellers/${s.id}`, { status: s.status === "active" ? "paused" : "active" });
                    refresh();
                  }}
                >
                  {s.status === "active" ? <span className="inline-flex items-center gap-2"><Pause className="size-3.5" /> Pause</span> : <span className="inline-flex items-center gap-2"><Play className="size-3.5" /> Resume</span>}
                </DM.Item>
                <DM.Item className="cursor-pointer rounded-lg px-3 py-2 outline-none data-[highlighted]:bg-secondary" onSelect={research}>
                  Search community reviews
                </DM.Item>
                <DM.Item
                  className="cursor-pointer rounded-lg px-3 py-2 text-destructive outline-none data-[highlighted]:bg-secondary"
                  onSelect={async () => {
                    if (!confirm(`Remove ${s.name}? Products are kept unless you choose otherwise.`)) return;
                    const also = confirm("Also delete this seller's products?");
                    await api.del(`/api/sellers/${s.id}?delete_products=${also}`);
                    refresh();
                  }}
                >
                  <span className="inline-flex items-center gap-2"><Trash2 className="size-3.5" /> Remove seller</span>
                </DM.Item>
              </DM.Content>
            </DM.Portal>
          </DM.Root>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap items-start gap-2 border-t pt-3 text-xs text-muted-foreground">
        <span className="mt-0.5">{STATUS_ICON[s.last_scan_status ?? ""] ?? <CircleDashed className="size-4" />}</span>
        <div className="min-w-0 flex-1">
          <span className="text-foreground">{s.last_scan_status ? titleCase(s.last_scan_status) : "Never scanned"}</span>
          {s.last_scan_at && <span> · {timeAgo(s.last_scan_at)}</span>}
          {s.last_run && <span> · {s.last_run.pages_fetched} requests · {s.last_run.products_seen} albums seen</span>}
          {s.last_scan_message && <p className="mt-1 break-words leading-relaxed">{s.last_scan_message}</p>}
        </div>
        <div className="flex gap-1.5">
          <SellerDialog seller={s} onSaved={refresh} trigger={<Button size="sm" variant="ghost">Edit</Button>} />
          <AddEvidenceDialog endpoint={`/api/sellers/${s.id}/evidence`} onAdded={refresh} trigger={<Button size="sm" variant="ghost">Add evidence ({s.evidence_count})</Button>} />
        </div>
      </div>
    </Card>
  );
}

function ImportPanel({ sellers, onDone }: { sellers: Seller[]; onDone: () => void }) {
  const watch = useJobWatcher();
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const backend = process.env.NEXT_PUBLIC_BACKEND_PUBLIC_URL || "http://localhost:8000";
  const { data: bm } = useSWR<{ token: string }>("/api/bookmarklet-config");
  const token = bm?.token ?? "";
  const bookmarklet = `javascript:(()=>{fetch('${backend}/api/import/bookmarklet',{method:'POST',headers:{'Content-Type':'application/json','X-WS-Token':'${token}'},body:JSON.stringify({url:location.href,html:document.documentElement.outerHTML})}).then(r=>r.json()).then(j=>alert(j.ok?'Wardrobe Scout: saved '+(j.created?'new':'updated')+' product':'Wardrobe Scout: '+(j.error||j.detail))).catch(e=>alert('Wardrobe Scout is not reachable: '+e))})()`;
  const bookmarkletRef = (el: HTMLAnchorElement | null) => {
    if (el) el.setAttribute("href", bookmarklet);
  };

  return (
    <Card className="p-5">
      <Tabs defaultValue="link">
        <TabsList>
          <TabsTrigger value="link">Paste link</TabsTrigger>
          <TabsTrigger value="manual">Manual entry</TabsTrigger>
          <TabsTrigger value="file">CSV / JSON</TabsTrigger>
          <TabsTrigger value="bookmarklet">Bookmarklet</TabsTrigger>
        </TabsList>
        <TabsContent value="link">
          <p className="mb-3 text-sm text-muted-foreground">Yupoo album, Weidian item or any shop product page. The link is also watched for price and stock changes.</p>
          <div className="flex gap-2">
            <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" data-testid="import-url-input" />
            <Button
              disabled={!url.startsWith("http") || busy}
              onClick={async () => {
                setBusy(true);
                try {
                  const r = await api.post<{ job_id: number }>("/api/import/url", { url, watch: true });
                  watch(r.job_id, "Importing link", (job) => {
                    if (job.result?.ok) toast.success("Product imported", { action: { label: "Open", onClick: () => (window.location.href = `/product/${job.result.product_id}`) } });
                    onDone();
                  });
                  setUrl("");
                } catch (e) {
                  toast.error(e instanceof Error ? e.message : "Import failed");
                } finally {
                  setBusy(false);
                }
              }}
            >
              <Link2 /> Import
            </Button>
          </div>
        </TabsContent>
        <TabsContent value="manual">
          <form
            className="grid gap-3 md:grid-cols-2"
            onSubmit={async (e) => {
              e.preventDefault();
              const fd = new FormData(e.currentTarget);
              try {
                const r = await api.form<{ product_id: number }>("/api/import/manual", fd);
                toast.success("Product saved", { action: { label: "Open", onClick: () => (window.location.href = `/product/${r.product_id}`) } });
                (e.target as HTMLFormElement).reset();
                onDone();
              } catch (err) {
                toast.error(err instanceof Error ? err.message : "Could not save");
              }
            }}
          >
            <Field label="Title" className="md:col-span-2">
              <Input name="title" required placeholder="As listed (Chinese is fine — it's translated)" />
            </Field>
            <Field label="Seller">
              <Select name="seller_id" defaultValue="">
                <option value="">Detect from link / none</option>
                {sellers.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Link">
              <Input name="url" placeholder="https://weidian.com/item.html?itemID=…" />
            </Field>
            <Field label="Price">
              <div className="flex gap-2">
                <Input name="price" inputMode="decimal" placeholder="399" />
                <Select name="currency" defaultValue="CNY" className="w-24">
                  <option>CNY</option>
                  <option>GBP</option>
                  <option>USD</option>
                  <option>EUR</option>
                </Select>
              </div>
            </Field>
            <Field label="Material (as claimed)">
              <Input name="material_text" placeholder="70% wool 30% cashmere" />
            </Field>
            <Field label="Brand / style">
              <Input name="brand" />
            </Field>
            <Field label="Category">
              <Input name="category" placeholder="hoodie, trousers…" />
            </Field>
            <Field label="Colours">
              <Input name="colours" placeholder="black, charcoal" />
            </Field>
            <Field label="Sizes">
              <Input name="sizes" placeholder="S, M, L" />
            </Field>
            <Field label="Image URLs" className="md:col-span-2">
              <Textarea name="image_urls" rows={2} placeholder="One per line" />
            </Field>
            <Field label="Photos / screenshots" className="md:col-span-2">
              <Input name="images" type="file" accept="image/*" multiple />
            </Field>
            <Field label="Description" className="md:col-span-2">
              <Textarea name="description" rows={3} />
            </Field>
            <Button type="submit" className="md:col-span-2">
              Save product
            </Button>
          </form>
        </TabsContent>
        <TabsContent value="file">
          <p className="mb-3 text-sm text-muted-foreground">
            Columns: seller, title, url, price, currency, description, material_text, brand, category, colours, sizes, image_urls (separated by |).{" "}
            {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- file download */}
            <a className="underline" href="/api/import/template.csv" download>
              Download template
            </a>
          </p>
          <label className="inline-flex cursor-pointer items-center gap-2 rounded-full border px-5 py-2.5 text-sm hover:bg-secondary">
            <FileUp className="size-4" /> Choose CSV or JSON file
            <input
              type="file"
              accept=".csv,.json,application/json,text/csv"
              className="hidden"
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (!f) return;
                const fd = new FormData();
                fd.append("file", f);
                try {
                  const r = await api.form<{ imported: number; failed: number; results: { row: number; ok: boolean; error?: string }[] }>(f.name.endsWith(".json") ? "/api/import/json" : "/api/import/csv", fd);
                  toast.success(`Imported ${r.imported} product(s)`, { description: r.failed ? `${r.failed} row(s) failed: ${r.results.filter((x) => !x.ok).map((x) => `row ${x.row}: ${x.error}`).slice(0, 3).join("; ")}` : undefined });
                  onDone();
                } catch (err) {
                  toast.error(err instanceof Error ? err.message : "Import failed");
                }
                e.target.value = "";
              }}
            />
          </label>
        </TabsContent>
        <TabsContent value="bookmarklet">
          <div className="max-w-2xl space-y-3 text-sm leading-relaxed">
            <p>
              When a site blocks automated access, browse it yourself and save listings with one click. Drag this button to your bookmarks bar, open a Yupoo album, Weidian item or shop page, then click the bookmark:
            </p>
            <a ref={bookmarkletRef} onClick={(e) => e.preventDefault()} className="inline-flex items-center gap-2 rounded-full bg-primary px-5 py-2.5 text-primary-foreground" href="#">
              <Plus className="size-4" /> Save to Wardrobe Scout
            </a>
            <p className="text-muted-foreground">
              It sends the page you are viewing (as your browser sees it) to your local backend at <code className="rounded bg-muted px-1">{backend}</code>. It never logs in, solves CAPTCHAs or reads other tabs. It contains your private API key — don&apos;t share it; it only works on this computer. Safari on iPhone: add any bookmark, then edit its address and paste the code below.
            </p>
            <Textarea readOnly value={bookmarklet} rows={3} className="font-mono text-xs" onFocus={(e) => e.currentTarget.select()} />
          </div>
        </TabsContent>
      </Tabs>
    </Card>
  );
}

type Watched = { id: number; url: string; last_status: string | null; last_message: string | null; last_checked_at: string | null; product_id: number | null };
type Lead = { id: number; source: string; url: string; title: string; excerpt: string | null; posted_at: string | null; possible_affiliate: boolean; seller_id: number | null; product_id: number | null };

function LeadsPanel({ sellers }: { sellers: Seller[] }) {
  const { data, mutate } = useSWR<Lead[]>("/api/research/leads");
  const accept = async (l: Lead, polarity: number) => {
    const claim = prompt("Summarise what this source shows (it becomes an independent buyer report):", l.title);
    if (!claim) return;
    await api.post(`/api/research/leads/${l.id}/accept`, { aspect: "reputation", polarity, claim, seller_id: l.seller_id, product_id: l.product_id });
    toast.success("Added as evidence");
    mutate();
  };
  if (!data?.length)
    return <p className="text-sm text-muted-foreground">No research leads. Use “Search community reviews” on a seller (requires internet access to reddit.com from your computer) or add evidence manually.</p>;
  return (
    <ul className="space-y-3">
      {data.map((l) => (
        <li key={l.id} className="rounded-xl border p-3 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline">{l.source}</Badge>
            {l.possible_affiliate && <Badge variant="caution">possible affiliate</Badge>}
            {l.seller_id && <span className="text-xs text-muted-foreground">{sellers.find((s) => s.id === l.seller_id)?.name}</span>}
            <span className="ml-auto text-xs text-muted-foreground">{timeAgo(l.posted_at)}</span>
          </div>
          <a href={l.url} target="_blank" rel="noreferrer" className="mt-2 block font-medium hover:underline">
            {l.title}
          </a>
          {l.excerpt && <p className="mt-1 line-clamp-3 text-muted-foreground">{l.excerpt}</p>}
          <div className="mt-2 flex gap-1.5">
            <Button size="sm" variant="outline" onClick={() => accept(l, 1)}>
              Positive report
            </Button>
            <Button size="sm" variant="outline" onClick={() => accept(l, -1)}>
              Negative report
            </Button>
            <Button size="sm" variant="ghost" onClick={async () => { await api.post(`/api/research/leads/${l.id}/dismiss`); mutate(); }}>
              Dismiss
            </Button>
          </div>
        </li>
      ))}
    </ul>
  );
}

export default function SellersPage() {
  const { data, error, isLoading, mutate } = useSWR<Seller[]>("/api/sellers");
  const { data: jobs, mutate: mutateJobs } = useSWR<Job[]>("/api/jobs?limit=15", { refreshInterval: 4000 });
  const { data: watched, mutate: mutateWatched } = useSWR<Watched[]>("/api/watched");
  const watch = useJobWatcher();
  const refresh = () => {
    mutate();
    mutateJobs();
    mutateWatched();
  };
  const totals = (data ?? []).reduce((a, s) => ({ scraped: a.scraped + s.products.scraped, manual: a.manual + s.products.manual, seed: a.seed + s.products.seed }), { scraped: 0, manual: 0, seed: 0 });

  return (
    <div>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="label">Sources & credibility</p>
          <h1 className="page-title mt-2">Sellers</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            {totals.scraped} products genuinely collected by scans · {totals.manual} imported by you · {totals.seed} seed records
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            onClick={async () => {
              const r = await api.post<{ job_id: number }>("/api/sellers/scan-all", { mode: "incremental" });
              watch(r.job_id, "Refreshing all active sellers", refresh);
            }}
          >
            <ScanSearch /> Scan all
          </Button>
          <SellerDialog onSaved={refresh} trigger={<Button><Plus /> Add seller</Button>} />
        </div>
      </div>

      <div className="mt-8">
        <ScanPanel onChange={refresh} />
      </div>

      <div className="mt-8 space-y-3">
        {error ? (
          <ErrorState error={error} retry={() => mutate()} />
        ) : isLoading ? (
          <Skeleton className="h-40" />
        ) : !data?.length ? (
          <EmptyState title="No sellers yet">Add a Yupoo catalogue or a shop to begin.</EmptyState>
        ) : (
          data.map((s) => <SellerRow key={s.id} s={s} refresh={refresh} />)
        )}
      </div>

      <h2 className="mt-14 text-3xl">Import products</h2>
      <p className="mt-1 text-sm text-muted-foreground">Reliable alternatives when automated access isn&apos;t available.</p>
      <div className="mt-5">
        <ImportPanel sellers={data ?? []} onDone={refresh} />
      </div>

      <div className="mt-14 grid gap-8 lg:grid-cols-2">
        <div>
          <div className="flex items-center justify-between">
            <h2 className="text-3xl">Watched links</h2>
            <Button size="sm" variant="outline" onClick={async () => { const r = await api.post<{ job_id: number }>("/api/watched/check"); watch(r.job_id, "Checking watched links", refresh); }}>
              <RefreshCw /> Check now
            </Button>
          </div>
          <ul className="mt-4 divide-y rounded-2xl border bg-card">
            {(watched ?? []).map((w) => (
              <li key={w.id} className="flex items-start gap-3 p-3 text-sm">
                <span className="mt-0.5">{w.last_status === "ok" ? STATUS_ICON.succeeded : w.last_status ? STATUS_ICON.failed : <CircleDashed className="size-4 text-muted-foreground" />}</span>
                <div className="min-w-0 flex-1">
                  <a href={w.url} target="_blank" rel="noreferrer" className="block truncate hover:underline">
                    {w.url}
                  </a>
                  <p className="text-xs text-muted-foreground">
                    {w.last_checked_at ? `checked ${timeAgo(w.last_checked_at)}` : "not checked yet"}
                    {w.last_message && ` · ${w.last_message}`}
                  </p>
                </div>
                <button aria-label="Stop watching" className="text-muted-foreground hover:text-destructive" onClick={async () => { await api.del(`/api/watched/${w.id}`); mutateWatched(); }}>
                  <Trash2 className="size-4" />
                </button>
              </li>
            ))}
            {!watched?.length && <li className="p-4 text-sm text-muted-foreground">No watched links.</li>}
          </ul>
        </div>
        <div>
          <h2 className="text-3xl">Recent jobs</h2>
          <ul className="mt-4 divide-y rounded-2xl border bg-card">
            {(jobs ?? []).map((j) => (
              <li key={j.id} className="p-3 text-sm">
                <div className="flex items-center gap-2">
                  {STATUS_ICON[j.status] ?? <CircleDashed className="size-4 text-muted-foreground" />}
                  <span>{titleCase(j.kind.replace(/_/g, " "))}</span>
                  <span className="ml-auto text-xs text-muted-foreground">{timeAgo(j.created_at)}</span>
                </div>
                {j.message && <p className={cn("mt-1 break-words text-xs text-muted-foreground", j.status === "running" && "animate-pulse")}>{j.message}</p>}
              </li>
            ))}
            {!jobs?.length && <li className="p-4 text-sm text-muted-foreground">No jobs yet.</li>}
          </ul>
        </div>
      </div>

      <h2 className="mt-14 flex items-center gap-2 text-3xl">
        <MessageSquareQuote className="size-6" strokeWidth={1.25} /> Research leads
      </h2>
      <p className="mb-4 mt-1 text-sm text-muted-foreground">Community posts found by searches. You decide what counts as evidence; promotional posts are flagged.</p>
      <LeadsPanel sellers={data ?? []} />
    </div>
  );
}
