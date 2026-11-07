import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export const INTERVAL_H = 0.25;

export function timeToIndex(time: string): number {
  const [h, m] = time.split(":").map(Number);
  return Math.round((h * 60 + m) / 15);
}

export function indexToTime(i: number): string {
  const min = i * 15;
  const h = Math.floor(min / 60) % 24;
  return `${String(h).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}`;
}

/** Time at which an interval ends, e.g. "15:00" -> "15:15". "23:45" -> "24:00". */
export function intervalEnd(time: string): string {
  const i = timeToIndex(time) + 1;
  return i === 96 ? "24:00" : indexToTime(i);
}
