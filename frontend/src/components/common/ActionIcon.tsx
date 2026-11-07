import { BatteryCharging, Pause, Zap } from "lucide-react";
import type { ActionKind } from "@/lib/labels";
import { cn } from "@/lib/utils";

export const actionColor: Record<ActionKind, string> = {
  charge: "text-charge",
  discharge: "text-discharge",
  hold: "text-hold",
};

export const actionBg: Record<ActionKind, string> = {
  charge: "bg-charge/12",
  discharge: "bg-discharge/12",
  hold: "bg-hold/12",
};

const icons = { charge: BatteryCharging, discharge: Zap, hold: Pause };

export function ActionIcon({ kind, className }: { kind: ActionKind; className?: string }) {
  const I = icons[kind];
  return <I aria-hidden className={cn(actionColor[kind], className)} />;
}
