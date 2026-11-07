import type { Goal } from "@/api/types";
import { GoalStatusWord, ProgressLine } from "@/components/common/marks";
import { goalProgressText, goalTypeLabel } from "@/lib/labels";
import { cn } from "@/lib/utils";

interface Props {
  goal: Goal;
  onDelete: (id: string) => void;
  deleting: boolean;
}

export function GoalItem({ goal, onDelete, deleting }: Props) {
  const progress = goalProgressText(goal);
  const pct = goal.type === "reserve" && goal.mwh ? ((goal.stored_mwh ?? 0) / goal.mwh) * 100 : null;
  const failed = goal.status === "missed" || goal.status === "violated";
  // Reserves already show progress; their message adds only the outcome ("6 MWh reached at 12:45").
  const showMessage = goal.message && (goal.type !== "reserve" || ["met", "missed", "violated"].includes(goal.status));

  return (
    <li className={cn("grid grid-cols-[1fr_auto] gap-x-4 gap-y-1.5 border-t border-rule py-5 first:border-t-0", deleting && "opacity-50")}>
      <p className="font-serif text-2xl leading-snug">{goal.source_text}</p>
      <GoalStatusWord status={goal.status} className="text-lg" />

      <p className="col-span-2 text-sm text-ink-3">
        {goalTypeLabel[goal.type]} ·{" "}
        <span title={goal.hard ? "The safety layer will not let any decision break it" : "The AI aims for it but may trade it off for money"}>
          {goal.hard ? "must be kept" : "preference"}
        </span>
        {goal.type === "min_sell_price" && goal.eur_per_mwh != null && <> · nothing is sold below €{goal.eur_per_mwh.toFixed(2)}/MWh</>}
      </p>

      {pct != null && progress && (
        <div className="col-span-2 flex flex-col gap-1">
          <ProgressLine value={pct} status={goal.status} label={progress} />
          <span className="num text-sm text-ink-3">{progress}</span>
        </div>
      )}
      {showMessage && <p className={cn("col-span-2 text-sm", failed ? "text-bad" : "text-ink-2")}>{goal.message}</p>}
      {goal.status === "unreachable" && (
        <p className="col-span-2 text-sm text-bad">Not enough time left to store this, even charging at full power.</p>
      )}

      <button
        type="button"
        onClick={() => onDelete(goal.id)}
        disabled={deleting}
        className="col-span-2 w-fit text-sm text-ink-3 underline-offset-2 hover:text-bad hover:underline"
        aria-label={`Remove goal "${goal.source_text}"`}
      >
        Remove
      </button>
    </li>
  );
}
