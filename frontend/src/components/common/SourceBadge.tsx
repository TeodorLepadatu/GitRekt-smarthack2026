import { Bot, ListChecks, ShieldCheck } from "lucide-react";
import type { Interval } from "@/api/types";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { sourceHint, sourceKind, sourceLabel, type SourceKind } from "@/lib/labels";
import { cn } from "@/lib/utils";

const variant: Record<SourceKind, BadgeVariant> = { ai: "ai", backup: "warn", safety: "info" };
const Icon = { ai: Bot, backup: ListChecks, safety: ShieldCheck };

export function SourceBadge({ interval, className }: { interval: Pick<Interval, "decision" | "guard">; className?: string }) {
  const kind = sourceKind(interval);
  const I = Icon[kind];
  return (
    <Badge variant={variant[kind]} title={sourceHint[kind]} className={cn(className)}>
      <I />
      {sourceLabel[kind]}
    </Badge>
  );
}
