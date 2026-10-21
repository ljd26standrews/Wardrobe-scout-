"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Toaster } from "sonner";
import { SWRConfig } from "swr";
import { BarChart3, Bookmark, Compass, GitCompareArrows, LayoutGrid, Menu, Moon, Settings, Shirt, Store, Sun } from "lucide-react";
import { fetcher } from "@/lib/api";
import { cn } from "@/lib/utils";
import * as D from "@radix-ui/react-dialog";

const NAV = [
  { href: "/", label: "Discover", icon: Compass },
  { href: "/browse", label: "Browse", icon: LayoutGrid },
  { href: "/compare", label: "Compare", icon: GitCompareArrows },
  { href: "/saved", label: "Saved", icon: Bookmark },
  { href: "/wardrobe", label: "Wardrobe", icon: Shirt },
  { href: "/sellers", label: "Sellers", icon: Store },
  { href: "/insights", label: "Insights", icon: BarChart3 },
  { href: "/settings", label: "Settings", icon: Settings },
];
const MOBILE_PRIMARY = ["/", "/browse", "/saved", "/wardrobe"];

function isActive(path: string, href: string) {
  return href === "/" ? path === "/" : path.startsWith(href);
}

function ThemeToggle() {
  const [dark, setDark] = useState<boolean | null>(null);
  useEffect(() => {
    const t = document.documentElement.dataset.theme;
    setDark(t ? t === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches);
  }, []);
  if (dark === null) return <span className="size-9" />;
  return (
    <button
      aria-label="Toggle theme"
      className="rounded-full p-2 text-muted-foreground hover:bg-secondary hover:text-foreground"
      onClick={() => {
        const next = dark ? "light" : "dark";
        document.documentElement.dataset.theme = next;
        try {
          localStorage.setItem("ws-theme", next);
        } catch {}
        setDark(!dark);
      }}
    >
      {dark ? <Sun className="size-[18px]" /> : <Moon className="size-[18px]" />}
    </button>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => setMenuOpen(false), [path]);

  return (
    <SWRConfig value={{ fetcher, revalidateOnFocus: false, shouldRetryOnError: false }}>
      <header className="sticky top-0 z-40 border-b bg-background/85 backdrop-blur-md supports-[backdrop-filter]:bg-background/70">
        <div className="container flex h-14 items-center gap-6 md:h-16">
          <Link href="/" className="font-serif text-[22px] font-semibold leading-none tracking-tight md:text-2xl">
            Wardrobe <span className="italic font-normal">Scout</span>
          </Link>
          <nav className="hidden flex-1 items-center gap-1 lg:flex" aria-label="Main">
            {NAV.map((n) => (
              <Link
                key={n.href}
                href={n.href}
                className={cn(
                  "rounded-full px-3 py-1.5 text-[13px] transition-colors",
                  isActive(path, n.href) ? "bg-secondary text-foreground" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {n.label}
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-1">
            <ThemeToggle />
          </div>
        </div>
      </header>
      <main className="container overflow-x-clip min-h-[calc(100dvh-4rem)] pb-28 pt-6 md:pt-10 lg:pb-16">{children}</main>

      {/* Mobile bottom navigation */}
      <nav className="fixed inset-x-0 bottom-0 z-40 border-t bg-background/90 backdrop-blur-md safe-bottom lg:hidden" aria-label="Mobile">
        <div className="mx-auto grid max-w-lg grid-cols-5">
          {NAV.filter((n) => MOBILE_PRIMARY.includes(n.href)).map((n) => (
            <Link key={n.href} href={n.href} className={cn("flex flex-col items-center gap-0.5 pt-2.5 pb-1 text-[10.5px]", isActive(path, n.href) ? "text-foreground" : "text-muted-foreground")}>
              <n.icon className="size-[22px]" strokeWidth={isActive(path, n.href) ? 2 : 1.5} />
              {n.label}
            </Link>
          ))}
          <D.Root open={menuOpen} onOpenChange={setMenuOpen}>
            <D.Trigger className={cn("flex flex-col items-center gap-0.5 pt-2.5 pb-1 text-[10.5px]", NAV.filter((n) => !MOBILE_PRIMARY.includes(n.href)).some((n) => isActive(path, n.href)) ? "text-foreground" : "text-muted-foreground")}>
              <Menu className="size-[22px]" strokeWidth={1.5} />
              More
            </D.Trigger>
            <D.Portal>
              <D.Overlay className="fixed inset-0 z-50 bg-black/30" />
              <D.Content className="fixed inset-x-0 bottom-0 z-50 rounded-t-3xl border-t bg-popover p-3 pb-8 animate-fade-up safe-bottom">
                <D.Title className="sr-only">More pages</D.Title>
                <D.Description className="sr-only">Navigate to other sections</D.Description>
                <div className="mx-auto mb-3 h-1 w-10 rounded-full bg-border" />
                {NAV.filter((n) => !MOBILE_PRIMARY.includes(n.href)).map((n) => (
                  <Link key={n.href} href={n.href} className="flex items-center gap-4 rounded-xl px-4 py-3.5 text-base hover:bg-secondary">
                    <n.icon className="size-5 text-muted-foreground" strokeWidth={1.5} />
                    {n.label}
                  </Link>
                ))}
              </D.Content>
            </D.Portal>
          </D.Root>
        </div>
      </nav>
      <Toaster position="top-center" toastOptions={{ className: "!rounded-xl !border !bg-popover !text-popover-foreground !font-sans" }} />
    </SWRConfig>
  );
}
