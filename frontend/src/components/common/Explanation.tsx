import type { Interval } from "@/api/types";
import { cn } from "@/lib/utils";

/**
 * The supporting part of an interval's explanation: facts with numbers, what the
 * safety layer changed, and the AI's own note. The one-sentence summary is shown
 * by the caller (explanationText).
 */
export function ExplanationFacts({ iv, className, showNote = true }: { iv: Interval; className?: string; showNote?: boolean }) {
  const e = iv.explanation;
  if (!e) return null;
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {e.facts.length > 0 && (
        <ul className="flex flex-col gap-0.5">
          {e.facts.map((f) => (
            <li key={f} className="num">
              · {f}
            </li>
          ))}
        </ul>
      )}
      {e.safety && <p className="text-warn">{e.safety}</p>}
      {showNote && e.ai_note && (
        <p className="text-ink-3">
          <span className="label mr-2 align-middle">AI's note</span>“{e.ai_note}”
        </p>
      )}
      {showNote && e.ai_note_dropped && (
        <p className="text-ink-3">The AI's own sentence contradicted its action, so it is not shown.</p>
      )}
    </div>
  );
}
