import type { StreamStatus } from "@/api/client";
import { cn, intervalEnd } from "@/lib/utils";
import { TABS, type TabId } from "./tabs";

interface Props {
  tab: TabId;
  onTab: (t: TabId) => void;
  live: boolean;
  stream: StreamStatus;
  mocks: boolean;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  /** Latest decided interval start, e.g. "11:15". */
  now: string | null;
  date: string | null;
}

function fmtDate(iso: string | null) {
  if (!iso) return "";
  const d = new Date(`${iso}T12:00:00`);
  return d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

/** Sun/moon drawn as a half-filled disc: left half ink in night mode, right half in day mode. */
function ThemeGlyph({ dark }: { dark: boolean }) {
  return (
    <svg viewBox="0 0 20 20" className="size-5" aria-hidden>
      <circle cx="10" cy="10" r="7.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d={dark ? "M10 2.5 A7.5 7.5 0 0 0 10 17.5 Z" : "M10 2.5 A7.5 7.5 0 0 1 10 17.5 Z"} fill="currentColor" />
    </svg>
  );
}

/** Two-part switch: the active side is filled, so the current mode is obvious. */
export function ThemeButton({ theme, onToggle }: { theme: "light" | "dark"; onToggle: () => void }) {
  const dark = theme === "dark";
  const side = (on: boolean) =>
    cn("flex items-center gap-2 px-4 text-base transition-colors", on ? "bg-ink font-semibold text-paper" : "text-ink-2 hover:text-ink");
  return (
    <div role="radiogroup" aria-label="Colours" className="flex h-11 shrink-0 border border-ink">
      <button type="button" role="radio" aria-checked={!dark} onClick={() => dark && onToggle()} className={side(!dark)}>
        <ThemeGlyph dark={false} />
        Day
      </button>
      <button type="button" role="radio" aria-checked={dark} onClick={() => !dark && onToggle()} className={cn(side(dark), "border-l border-ink")}>
        <ThemeGlyph dark />
        Night
      </button>
    </div>
  );
}

export function TopBar({ tab, onTab, live, stream, mocks, theme, onToggleTheme, now, date }: Props) {
  const conn = live ? "connected" : stream === "connecting" ? "connecting" : "no connection";
  return (
    <header className="border-b border-rule-strong bg-paper print:hidden">
      <div className="flex items-start justify-between gap-6 px-6 pt-6 lg:px-12">
        <div className="flex flex-col">
          <span className="font-serif text-4xl leading-none font-semibold tracking-tight">Site energy log</span>
          <span className="num mt-2.5 text-sm text-ink-3">
            {fmtDate(date)}
            {now && (
              <>
                {" "}
                · interval {now}–{intervalEnd(now)}
              </>
            )}{" "}
            · <span className={live ? "text-ink-2" : "text-bad"}>{conn}</span>
            {mocks && " · simulated day, not live site data"}
          </span>
        </div>
        <ThemeButton theme={theme} onToggle={onToggleTheme} />
      </div>
      <nav aria-label="Sections" className="mt-5 flex gap-8 overflow-x-auto px-6 lg:px-12">
        {TABS.map((t) => {
          const active = t.id === tab;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => onTab(t.id)}
              aria-current={active ? "page" : undefined}
              className={cn(
                "-mb-px border-b-[3px] pb-3 text-lg whitespace-nowrap transition-colors",
                active ? "border-ink font-semibold text-ink" : "border-transparent text-ink-3 hover:text-ink",
              )}
            >
              {t.label}
            </button>
          );
        })}
      </nav>
    </header>
  );
}
