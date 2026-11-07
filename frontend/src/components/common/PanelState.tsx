import { AlertTriangle, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export function PanelLoading({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-3", className)} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className={cn("h-6", i === 0 ? "w-2/3" : "w-full")} />
      ))}
    </div>
  );
}

interface PanelErrorProps {
  /** What failed to load, e.g. "your goals". */
  what?: string;
  /** Overrides the default "Could not load ..." sentence. */
  message?: string;
  onRetry?: () => void;
  className?: string;
}

export function PanelError({ what, message, onRetry, className }: PanelErrorProps) {
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-start gap-3 rounded-lg border border-bad/30 bg-bad/8 p-4 text-sm sm:flex-row sm:items-center",
        className,
      )}
    >
      <AlertTriangle className="size-5 shrink-0 text-bad" />
      <p className="flex-1">{message ?? `Could not load ${what}. The system may be restarting.`}</p>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry}>
          <RotateCw /> Try again
        </Button>
      )}
    </div>
  );
}
