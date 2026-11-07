import type { StreamStatus } from "@/api/client";

interface Props {
  apiDown: boolean;
  stream: StreamStatus;
}

/** Full-width warning when the backend cannot be reached. */
export function ApiBanner({ apiDown, stream }: Props) {
  if (!apiDown && stream !== "down") return null;
  const text = apiDown
    ? "No answer from the energy system. Figures below are from the last successful update; retrying."
    : "Lost the live connection. Figures update again as soon as it reconnects.";
  return (
    <div role="alert" className="bg-bad px-4 py-2 text-sm font-medium text-paper sm:px-8">
      {text}
    </div>
  );
}
