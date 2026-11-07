/** Tiny regex stand-in for the backend's LLM goal parser. */
import type { Goal } from "@/api/types";

export type ParseResult = { goal: Omit<Goal, "id" | "status"> } | { question: string };

function hhmm(hRaw: string, mRaw?: string, ampm?: string): string | null {
  let h = Number(hRaw);
  const m = mRaw ? Number(mRaw) : 0;
  if (ampm?.toLowerCase() === "pm" && h < 12) h += 12;
  if (ampm?.toLowerCase() === "am" && h === 12) h = 0;
  if (h > 24 || m > 59) return null;
  const q = Math.round(m / 15) * 15;
  const total = Math.min(24 * 60, h * 60 + q);
  if (total === 24 * 60) return "24:00";
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

export function parseGoal(raw: string): ParseResult {
  const text = raw.trim();
  const t = text.toLowerCase();
  const soft = /\b(if possible|try|ideally|prefer|soft)\b/.test(t);
  const mwh = /(\d+(?:[.,]\d+)?)\s*mwh/.exec(t);
  const amount = mwh ? Number(mwh[1].replace(",", ".")) : null;

  if (/(maxim[iy][sz]e|more|make)\s+(the\s+)?(profit|money)/.test(t)) {
    return { goal: { type: "max_profit", hard: false, source_text: text } };
  }

  const sell = /(never|don'?t|do not|no)\s+(sell|export)[^\d]*(\d+(?:[.,]\d+)?)/.exec(t);
  if (sell) {
    return {
      goal: { type: "min_sell_price", hard: !soft, eur_per_mwh: Number(sell[3].replace(",", ".")), source_text: text },
    };
  }

  if (amount != null && amount > 10) {
    return { question: `The battery holds at most 10 MWh. How much of it should I keep instead of ${amount} MWh?` };
  }

  if (amount != null && /(overnight|night)/.test(t)) {
    return { goal: { type: "reserve", hard: !soft, mwh: amount, by: "22:00", until: "06:00", source_text: text } };
  }

  const at = /(?:by|before|at|until)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?/.exec(t);
  if (amount != null && at) {
    const by = hhmm(at[1], at[2], at[3]);
    if (by) return { goal: { type: "reserve", hard: !soft, mwh: amount, by, source_text: text } };
  }

  if (amount != null) {
    return { question: `By what time should ${amount} MWh be stored? For example: "Store ${amount} MWh by 18:00".` };
  }
  if (/(keep|store|save|reserve|backup|evening|night)/.test(t)) {
    return { question: 'How much energy should I keep, and by when? For example: "Keep 3 MWh by 20:00".' };
  }
  return {
    question:
      "I didn't understand that goal. You can ask me to maximise profit, store an amount of energy by a time, or never sell below a price.",
  };
}
