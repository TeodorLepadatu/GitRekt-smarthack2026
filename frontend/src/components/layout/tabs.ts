export type TabId = "now" | "day" | "goals" | "decisions";

export const TABS: { id: TabId; label: string; title: string; intro: string }[] = [
  { id: "now", label: "Now", title: "Now", intro: "" },
  {
    id: "day",
    label: "Day",
    title: "The day, interval by interval",
    intro: "Price, stored energy and site power for all 96 quarter-hours. Past intervals are measured, later ones are forecast.",
  },
  {
    id: "goals",
    label: "Goals",
    title: "What the battery should aim for",
    intro: "Write a goal as you would say it on the phone. It is turned into a rule and checked every 15 minutes.",
  },
  {
    id: "decisions",
    label: "Decisions",
    title: "Decision log",
    intro: "Every quarter-hour decision since 00:00, latest first. Open a row to read the AI's reasoning.",
  },
];

export function tabFromHash(): TabId {
  const h = window.location.hash.replace("#", "");
  if (h === "today") return "day";
  if (h === "history") return "decisions";
  return TABS.some((t) => t.id === h) ? (h as TabId) : "now";
}
