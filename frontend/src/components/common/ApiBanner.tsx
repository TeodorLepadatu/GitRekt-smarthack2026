import { WifiOff } from "lucide-react";
import type { StreamStatus } from "@/api/client";

interface Props {
  apiDown: boolean;
  stream: StreamStatus;
}

/** Full-width warning when the backend cannot be reached. */
export function ApiBanner({ apiDown, stream }: Props) {
  if (!apiDown && stream !== "down") return null;
  const text = apiDown
    ? "The energy system is not responding. The numbers below may be out of date. Retrying automatically…"
    : "Live updates paused: lost the connection to the energy system. Reconnecting…";
  return (
    <div role="alert" className="flex items-center gap-3 bg-bad px-4 py-2.5 text-sm font-medium text-white">
      <WifiOff className="size-5 shrink-0" />
      <span>{text}</span>
    </div>
  );
}
