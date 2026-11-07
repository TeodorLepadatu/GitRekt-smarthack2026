import { indexToTime, intervalEnd, timeToIndex } from "@/lib/utils";

/** "HH:MM" -> interval index, with "24:00" = 96 (the end of the day). */
export const slotOf = (t: string) => (t === "24:00" ? 96 : timeToIndex(t));
const timeOf = (i: number) => (i >= 96 ? "24:00" : indexToTime(i));

/** Start of the next interval to run (a command applies from there): right after the latest decided one. */
export function nextStart(latest: string | null): string {
  return latest ? intervalEnd(latest) : "00:00";
}

/** The end of an override that runs `n` intervals from `start`, capped at the end of the day. */
export function untilAfter(start: string, n: number): string {
  return timeOf(slotOf(start) + n);
}

/** A clock time typed by the operator ("17:40"), rounded up to the next interval start ("17:45"). */
export function roundUpToSlot(t: string): string {
  const [h, m] = t.split(":").map(Number);
  return timeOf(Math.ceil((h * 60 + m) / 15));
}

export const DURATIONS = [
  { id: "15m", label: "15 min", intervals: 1 },
  { id: "1h", label: "1 hour", intervals: 4 },
  { id: "4h", label: "4 hours", intervals: 16 },
  { id: "day", label: "Rest of the day", intervals: 96 },
] as const;
