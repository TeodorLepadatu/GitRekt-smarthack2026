import { CheckCircle2, CircleAlert, Clock, Trash2, TrendingUp, XCircle } from "lucide-react";
import type { Goal, GoalStatus } from "@/api/types";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { goalProgressText, goalStatusLabel, goalTypeLabel } from "@/lib/labels";

const statusVariant: Record<GoalStatus, BadgeVariant> = {
  on_track: "ok",
  behind: "warn",
  met: "info",
  unreachable: "bad",
};
const statusBar: Record<GoalStatus, string> = {
  on_track: "bg-ok",
  behind: "bg-warn",
  met: "bg-info",
  unreachable: "bg-bad",
};
const StatusIcon = { on_track: TrendingUp, behind: Clock, met: CheckCircle2, unreachable: XCircle };

interface Props {
  goal: Goal;
  onDelete: (id: string) => void;
  deleting: boolean;
}

export function GoalItem({ goal, onDelete, deleting }: Props) {
  const I = StatusIcon[goal.status];
  const progress = goalProgressText(goal);
  const pct = goal.type === "reserve" && goal.mwh ? ((goal.stored_mwh ?? 0) / goal.mwh) * 100 : null;
  return (
    <li className="group flex flex-col gap-2.5 rounded-xl border bg-background/40 p-4">
      <div className="flex items-start gap-2">
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <p className="font-display text-base leading-snug font-semibold">“{goal.source_text}”</p>
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge variant={statusVariant[goal.status]}>
              <I />
              {goalStatusLabel[goal.status]}
            </Badge>
            <Badge
              variant="outline"
              title={goal.hard ? "Must be kept: the safety layer enforces it" : "Preference: the AI tries, but may trade it off"}
            >
              {goal.hard ? "HARD" : "SOFT"}
            </Badge>
            <span className="text-xs text-muted-foreground">{goalTypeLabel[goal.type]}</span>
          </div>
        </div>
        <Button
          variant="ghost"
          size="icon"
          aria-label={`Delete goal "${goal.source_text}"`}
          title="Delete goal"
          disabled={deleting}
          onClick={() => onDelete(goal.id)}
          className="text-muted-foreground hover:text-bad"
        >
          <Trash2 />
        </Button>
      </div>
      {pct != null && progress && (
        <div className="flex flex-col gap-1">
          <Progress value={pct} indicatorClassName={statusBar[goal.status]} label={progress} />
          <span className="tabular text-xs text-muted-foreground">{progress}</span>
        </div>
      )}
      {goal.type === "min_sell_price" && goal.eur_per_mwh != null && (
        <span className="text-xs text-muted-foreground">No selling while the price is below €{goal.eur_per_mwh.toFixed(2)}/MWh.</span>
      )}
      {goal.status === "unreachable" && (
        <span className="flex items-center gap-1 text-xs text-bad">
          <CircleAlert className="size-3.5" /> Not enough time left to store this energy, even at full power.
        </span>
      )}
    </li>
  );
}
