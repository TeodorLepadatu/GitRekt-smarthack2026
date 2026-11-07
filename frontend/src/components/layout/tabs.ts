import { Activity, CalendarRange, History, Target, type LucideIcon } from "lucide-react";

export type TabId = "now" | "today" | "goals" | "history";

export const TABS: { id: TabId; label: string; icon: LucideIcon; hint: string }[] = [
  { id: "now", label: "Now", icon: Activity, hint: "What the battery is doing and why" },
  { id: "today", label: "Today", icon: CalendarRange, hint: "Prices, battery level and power for the whole day" },
  { id: "goals", label: "Goals", icon: Target, hint: "Tell the system what you want" },
  { id: "history", label: "History", icon: History, hint: "Every decision and who made it" },
];

export function tabFromHash(): TabId {
  const h = window.location.hash.replace("#", "");
  return TABS.some((t) => t.id === h) ? (h as TabId) : "now";
}
