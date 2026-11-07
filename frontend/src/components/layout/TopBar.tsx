import { Moon, Radio, Sun, Zap } from "lucide-react";
import type { StreamStatus } from "@/api/client";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { TABS, type TabId } from "./tabs";

interface Props {
  tab: TabId;
  onTab: (t: TabId) => void;
  live: boolean;
  stream: StreamStatus;
  mocks: boolean;
  theme: "light" | "dark";
  onToggleTheme: () => void;
}

function TabNav({ tab, onTab, className }: { tab: TabId; onTab: (t: TabId) => void; className?: string }) {
  return (
    <nav aria-label="Sections" className={cn("flex gap-1 rounded-2xl border bg-card p-1 shadow-card", className)}>
      {TABS.map((t) => {
        const active = t.id === tab;
        const I = t.icon;
        return (
          <button
            key={t.id}
            type="button"
            onClick={() => onTab(t.id)}
            aria-current={active ? "page" : undefined}
            title={t.hint}
            className={cn(
              "flex flex-1 items-center justify-center gap-2 rounded-xl px-4 py-2 text-sm font-semibold transition-colors",
              active ? "bg-primary text-primary-foreground shadow-sm" : "text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            <I className="size-4" />
            {t.label}
          </button>
        );
      })}
    </nav>
  );
}

export function TopBar({ tab, onTab, live, stream, mocks, theme, onToggleTheme }: Props) {
  return (
    <header className="border-b bg-background/80 backdrop-blur-md">
      <div className="mx-auto flex max-w-[1500px] flex-wrap items-center gap-x-4 gap-y-3 px-4 py-3 sm:px-6">
        <div className="flex items-center gap-3">
          <span className="relative flex size-10 items-center justify-center rounded-2xl bg-primary text-primary-foreground">
            <Zap className="size-5 fill-current" />
            <span className="absolute -right-0.5 -bottom-0.5 size-3.5 rounded-full border-2 border-background bg-solar" />
          </span>
          <div className="leading-tight">
            <h1 className="text-lg font-bold">Site Energy Agent</h1>
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <span className={cn("flex items-center gap-1 font-semibold", live ? "text-ok" : "text-warn")}>
                <Radio className={cn("size-3.5", live && "animate-pulse")} />
                {live ? "Live" : stream === "connecting" ? "Connecting…" : "Offline"}
              </span>
              · Solar, wind & 10 MWh battery{mocks && " · demo data"}
            </p>
          </div>
        </div>

        <TabNav tab={tab} onTab={onTab} className="order-last w-full lg:order-none lg:mx-auto lg:w-auto" />

        <div className="ml-auto flex items-center gap-2 lg:ml-0">
          <Button variant="outline" size="icon" className="rounded-xl" onClick={onToggleTheme} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}>
            {theme === "dark" ? <Sun /> : <Moon />}
          </Button>
        </div>
      </div>
    </header>
  );
}
