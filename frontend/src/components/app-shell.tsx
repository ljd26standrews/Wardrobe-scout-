"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import useSWR, { SWRConfig } from "swr";
import { Toaster } from "sonner";
import { BarChart3, Bookmark, ClipboardCheck, Compass, GitCompareArrows, LayoutGrid, Loader2, Menu, Moon, Settings, Shirt, Store, Sun } from "lucide-react";
import * as D from "@radix-ui/react-dialog";
import { fetcher } from "@/lib/api";
import { ReasonsHost } from "@/components/product/reject-dialog";
import { cn } from "@/lib/utils";

type NavItem = { href: string; label: string; icon: typeof Compass };

// Every route keeps its URL. Shopping first; your own data and tools after (design system §4).
const SHOP: NavItem[] = [
  { href: "/", label: "Discover", icon: Compass },
  { href: "/browse", label: "Browse all", icon: LayoutGrid },
  { href: "/saved", label: "Saved", icon: Bookmark },
  { href: "/compare", label: "Compare", icon: GitCompareArrows },
];
const YOURS: NavItem[] = [
  { href: "/wardrobe", label: "Wardrobe", icon: Shirt },
  { href: "/sellers", label: "Sellers", icon: Store },
  { href: "/insights", label: "Insights", icon: BarChart3 },
  { href: "/evaluate", label: "Evaluate", icon: ClipboardCheck },
];
const SETTINGS: NavItem = { href: "/settings", label: "Settings", icon: Settings };
const TABS = ["/", "/browse", "/saved", "/wardrobe"]; // phone bottom tabs; the rest is under More

function isActive(path: string, href: string) {
  return href === "/" ? path === "/" : path.startsWith(href);
}

function useTheme() {
  const [dark, setDark] = useState<boolean | null>(null);
  useEffect(() => {
    const t = document.documentElement.dataset.theme;
    setDark(t ? t === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches);
  }, []);
  const toggle = () => {
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("ws-theme", next);
    } catch {}
    setDark(!dark);
  };
  return { dark, toggle };
}

function ThemeButton({ withLabel }: { withLabel?: boolean }) {
  const { dark, toggle } = useTheme();
  if (dark === null) return <span className="size-10" />;
  const label = dark ? "Light theme" : "Dark theme";
  return (
    <button
      aria-label={withLabel ? undefined : label}
      onClick={toggle}
      className={cn(
        "inline-flex items-center gap-3 rounded-md text-sm text-muted-foreground hover:bg-secondary hover:text-foreground",
        withLabel ? "h-10 w-full px-3 [@media(pointer:coarse)]:h-11" : "size-10 justify-center [@media(pointer:coarse)]:size-11",
      )}
    >
      {dark ? <Sun className="size-[18px]" /> : <Moon className="size-[18px]" />}
      {withLabel && label}
    </button>
  );
}

/** Background work (scans, recalibration) in progress, from /api/status. */
function JobsIndicator() {
  const { data } = useSWR<{ running_jobs: number }>("/api/status", { refreshInterval: 15000 });
  if (!data?.running_jobs) return null;
  return (
    <Link href="/sellers" className="flex h-10 items-center gap-3 rounded-md px-3 text-[13px] text-muted-foreground hover:bg-secondary hover:text-foreground">
      <Loader2 className="size-4 animate-spin motion-reduce:animate-none" />
      {data.running_jobs} background job{data.running_jobs === 1 ? "" : "s"} running
    </Link>
  );
}

function SideLink({ item, path }: { item: NavItem; path: string }) {
  const active = isActive(path, item.href);
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex h-9 items-center gap-3 rounded-md px-3 text-sm transition-colors",
        active ? "bg-secondary font-medium text-foreground" : "text-muted-foreground hover:bg-secondary/70 hover:text-foreground",
      )}
    >
      <item.icon className={cn("size-[18px]", active && "text-primary")} strokeWidth={1.75} />
      {item.label}
    </Link>
  );
}

function Sidebar({ path }: { path: string }) {
  return (
    <aside className="sticky top-0 hidden h-dvh flex-col border-r bg-surface px-3 py-5 lg:flex" aria-label="Sidebar">
      <Link href="/" className="mb-6 px-3 text-[15px] font-semibold tracking-tight">
        Wardrobe Scout
      </Link>
      <nav aria-label="Main" className="flex flex-col gap-0.5">
        {SHOP.map((n) => (
          <SideLink key={n.href} item={n} path={path} />
        ))}
      </nav>
      <p className="label mb-1 mt-6 px-3">Your data</p>
      <nav aria-label="Your data" className="flex flex-col gap-0.5">
        {YOURS.map((n) => (
          <SideLink key={n.href} item={n} path={path} />
        ))}
      </nav>
      <div className="mt-auto flex flex-col gap-0.5">
        <JobsIndicator />
        <SideLink item={SETTINGS} path={path} />
        <ThemeButton withLabel />
      </div>
    </aside>
  );
}

function BottomTabs({ path }: { path: string }) {
  const [moreOpen, setMoreOpen] = useState(false);
  useEffect(() => setMoreOpen(false), [path]);
  const tabs = [...SHOP, ...YOURS].filter((n) => TABS.includes(n.href));
  const rest = [...SHOP, ...YOURS, SETTINGS].filter((n) => !TABS.includes(n.href));
  const tabClass = (active: boolean) =>
    cn("flex min-h-[52px] flex-col items-center justify-center gap-0.5 text-[11px] font-medium", active ? "text-foreground" : "text-muted-foreground");
  return (
    <nav className="safe-bottom fixed inset-x-0 bottom-0 z-40 border-t bg-background lg:hidden" aria-label="Mobile">
      <div className="mx-auto grid max-w-lg grid-cols-5">
        {tabs.map((n) => {
          const active = isActive(path, n.href);
          return (
            <Link key={n.href} href={n.href} aria-current={active ? "page" : undefined} className={tabClass(active)}>
              <n.icon className={cn("size-[22px]", active && "text-primary")} strokeWidth={active ? 2 : 1.75} />
              {n.href === "/browse" ? "Browse" : n.label}
            </Link>
          );
        })}
        <D.Root open={moreOpen} onOpenChange={setMoreOpen}>
          <D.Trigger className={tabClass(rest.some((n) => isActive(path, n.href)))}>
            <Menu className="size-[22px]" strokeWidth={1.75} />
            More
          </D.Trigger>
          <D.Portal>
            <D.Overlay className="fixed inset-0 z-50 bg-black/40" />
            <D.Content className="overlay-shadow safe-bottom fixed inset-x-0 bottom-0 z-50 rounded-t-sheet border-t bg-popover p-3 pb-6">
              <D.Title className="px-3 pb-2 pt-1 text-sm font-semibold">More</D.Title>
              <D.Description className="sr-only">Other sections of Wardrobe Scout</D.Description>
              {rest.map((n) => (
                <Link
                  key={n.href}
                  href={n.href}
                  aria-current={isActive(path, n.href) ? "page" : undefined}
                  className="flex min-h-12 items-center gap-4 rounded-md px-3 text-[15px] hover:bg-secondary"
                >
                  <n.icon className="size-5 text-muted-foreground" strokeWidth={1.75} />
                  {n.label}
                </Link>
              ))}
              <div className="mt-1 border-t pt-1">
                <ThemeButton withLabel />
              </div>
            </D.Content>
          </D.Portal>
        </D.Root>
      </div>
    </nav>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  return (
    <SWRConfig value={{ fetcher, revalidateOnFocus: false, shouldRetryOnError: false }}>
      <a href="#content" className="sr-only z-50 rounded-md bg-primary px-3 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:left-3 focus:top-3">
        Skip to content
      </a>
      <div className="min-h-dvh lg:grid lg:grid-cols-[232px_minmax(0,1fr)]">
        <Sidebar path={path} />
        <div className="min-w-0">
          <header className="sticky top-0 z-40 flex h-14 items-center justify-between border-b bg-background px-4 lg:hidden">
            <Link href="/" className="text-[15px] font-semibold tracking-tight">
              Wardrobe Scout
            </Link>
            <ThemeButton />
          </header>
          <main id="content" className="mx-auto w-full max-w-[1440px] overflow-x-clip px-4 pb-28 pt-5 md:px-6 md:pt-8 lg:px-10 lg:pb-16">
            {children}
          </main>
        </div>
      </div>
      <BottomTabs path={path} />
      <ReasonsHost />
      <Toaster position="top-center" toastOptions={{ className: "!rounded-lg !border !bg-popover !text-popover-foreground !font-sans" }} />
    </SWRConfig>
  );
}

/** One header pattern for every page: title, an optional line of context, actions on the right. */
export function PageHeader({ title, description, actions, className }: { title: string; description?: React.ReactNode; actions?: React.ReactNode; className?: string }) {
  return (
    <header className={cn("mb-6", className)}>
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
        <h1 className="page-title min-w-0">{title}</h1>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {description && <div className="mt-1.5 max-w-2xl text-sm text-muted-foreground">{description}</div>}
    </header>
  );
}
