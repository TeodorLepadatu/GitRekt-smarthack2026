import * as React from "react";
import { cn } from "@/lib/utils";

type DivProps = React.HTMLAttributes<HTMLDivElement>;

export const Card = ({ className, ...p }: DivProps) => (
  <div className={cn("rounded-2xl border bg-card text-card-foreground shadow-card", className)} {...p} />
);
export const CardHeader = ({ className, ...p }: DivProps) => (
  <div className={cn("flex flex-col gap-1 p-5 pb-3 sm:p-6 sm:pb-4", className)} {...p} />
);
export const CardTitle = ({ className, ...p }: React.HTMLAttributes<HTMLHeadingElement>) => (
  <h2 className={cn("text-lg font-semibold leading-tight", className)} {...p} />
);
export const CardDescription = ({ className, ...p }: React.HTMLAttributes<HTMLParagraphElement>) => (
  <p className={cn("text-sm text-muted-foreground", className)} {...p} />
);
export const CardContent = ({ className, ...p }: DivProps) => (
  <div className={cn("p-5 pt-0 sm:p-6 sm:pt-0", className)} {...p} />
);

/** Small uppercase caption used above a card's main number, e.g. "AM I ON TRACK?". */
export const Eyebrow = ({ className, ...p }: React.HTMLAttributes<HTMLSpanElement>) => (
  <span className={cn("text-xs font-semibold tracking-[0.08em] text-muted-foreground uppercase", className)} {...p} />
);
