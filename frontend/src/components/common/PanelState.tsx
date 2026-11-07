import { cn } from "@/lib/utils";

export function PanelLoading({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-3", className)} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className={cn("h-4 animate-pulse bg-wash", i === 0 ? "w-2/3" : "w-full")} />
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
    <p role="alert" className={cn("text-sm text-bad", className)}>
      {message ?? `Could not load ${what}. The system may be restarting.`}{" "}
      {onRetry && (
        <button type="button" onClick={onRetry} className="font-medium underline underline-offset-2">
          Try again
        </button>
      )}
    </p>
  );
}
