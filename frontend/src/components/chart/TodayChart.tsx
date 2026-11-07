import { useMemo, type ReactNode } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceArea,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useDay, useGoals } from "@/api/hooks";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { buildRows, goalMarkers, negativeRuns, nowX, xTickFormat, xTicks } from "./chartData";
import { IntervalTooltip, PowerTooltip } from "./ChartTooltip";

const SYNC = "today";
const MARGIN = { top: 14, right: 16, bottom: 0, left: 0 };
const AXIS_W = 52;

const statusFill: Record<string, string> = {
  on_track: "var(--ok)",
  behind: "var(--warn)",
  met: "var(--info)",
  unreachable: "var(--bad)",
};

function Legend({ items }: { items: { label: string; className: string; dashed?: boolean; area?: boolean }[] }) {
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
      {items.map((i) => (
        <li key={i.label} className="flex items-center gap-1.5">
          <span
            className={cn(
              i.area ? "h-2.5 w-3.5 rounded-sm" : "h-0.5 w-4",
              i.dashed && "border-t-2 border-dashed bg-transparent",
              i.className,
            )}
          />
          {i.label}
        </li>
      ))}
    </ul>
  );
}

function ChartBlock({ title, legend, children }: { title: string; legend: ReactNode; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center justify-between gap-2 pl-1">
        <h3 className="text-sm font-semibold">{title}</h3>
        {legend}
      </div>
      {children}
    </section>
  );
}

export function TodayChart() {
  const day = useDay();
  const goals = useGoals();

  const model = useMemo(() => {
    if (!day.data) return null;
    return {
      rows: buildRows(day.data),
      neg: negativeRuns(day.data),
      now: nowX(day.data),
      markers: goalMarkers(goals.data ?? []),
    };
  }, [day.data, goals.data]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Prices, battery and power</CardTitle>
        <CardDescription>Every 15 minutes of today. Hover any chart to see that moment on all three.</CardDescription>
      </CardHeader>
      <CardContent>
        {day.isPending ? (
          <PanelLoading rows={6} />
        ) : !model ? (
          <PanelError what="today's chart" onRetry={() => day.refetch()} />
        ) : (
          <div className="flex flex-col gap-5">
            <ChartBlock
              title="Electricity price (€/MWh)"
              legend={
                <Legend
                  items={[
                    { label: "Price", className: "bg-price" },
                    { label: "Break-even", className: "border-foreground/60", dashed: true },
                    { label: "Negative price", className: "bg-[var(--negative-zone)] border border-ok/40", area: true },
                  ]}
                />
              }
            >
              <div className="h-56">
                <ResponsiveContainer>
                  <ComposedChart data={model.rows} syncId={SYNC} margin={MARGIN}>
                    <CartesianGrid vertical={false} strokeDasharray="0" />
                    <XAxis dataKey="x" type="number" domain={[0, 96]} ticks={xTicks} tickFormatter={xTickFormat} hide />
                    <YAxis width={AXIS_W} tickLine={false} axisLine={false} fontSize={12} />
                    {model.neg.map(([a, b]) => (
                      <ReferenceArea key={a} x1={a} x2={b} fill="var(--negative-zone)" stroke="none" ifOverflow="visible" />
                    ))}
                    <ReferenceLine y={0} stroke="var(--muted-foreground)" strokeOpacity={0.5} />
                    <Line
                      dataKey="breakEven"
                      type="stepAfter"
                      stroke="var(--foreground)"
                      strokeOpacity={0.55}
                      strokeDasharray="6 4"
                      strokeWidth={1.5}
                      dot={false}
                      activeDot={false}
                      isAnimationActive={false}
                    />
                    <Line
                      dataKey="price"
                      type="stepAfter"
                      stroke="var(--price)"
                      strokeWidth={2}
                      dot={false}
                      activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--card)" }}
                      isAnimationActive={false}
                    />
                    {nowLine(model.now, true)}
                    <Tooltip content={<IntervalTooltip />} cursor={{ stroke: "var(--muted-foreground)", strokeWidth: 1 }} />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            </ChartBlock>

            <ChartBlock
              title="Battery level (MWh)"
              legend={
                <Legend
                  items={[
                    { label: "Stored energy", className: "bg-soc", area: true },
                    { label: "Goal target (colour = status)", className: "bg-ok rounded-full !w-2.5 !h-2.5", area: true },
                  ]}
                />
              }
            >
              <div className="h-44">
                <ResponsiveContainer>
                  <AreaChart data={model.rows} syncId={SYNC} margin={MARGIN}>
                    <defs>
                      <linearGradient id="socFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="var(--soc)" stopOpacity={0.45} />
                        <stop offset="100%" stopColor="var(--soc)" stopOpacity={0.05} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid vertical={false} />
                    <XAxis dataKey="x" type="number" domain={[0, 96]} ticks={xTicks} tickFormatter={xTickFormat} hide />
                    <YAxis width={AXIS_W} domain={[0, 10]} ticks={[0, 2, 4, 6, 8, 10]} tickLine={false} axisLine={false} fontSize={12} />
                    <Area
                      dataKey="soc"
                      type="linear"
                      stroke="var(--soc)"
                      strokeWidth={2}
                      fill="url(#socFill)"
                      connectNulls={false}
                      dot={false}
                      activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--card)" }}
                      isAnimationActive={false}
                    />
                    {model.markers.map((m) => (
                      <ReferenceDot
                        key={m.id}
                        x={m.x}
                        y={m.mwh}
                        r={6}
                        fill={statusFill[m.status]}
                        stroke="var(--card)"
                        strokeWidth={2}
                        ifOverflow="visible"
                        label={{ value: m.label, position: "top", fontSize: 11, fill: "var(--foreground)", offset: 9 }}
                      />
                    ))}
                    {nowLine(model.now)}
                    <Tooltip content={() => null} cursor={{ stroke: "var(--muted-foreground)", strokeWidth: 1 }} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </ChartBlock>

            <ChartBlock
              title="Production vs demand (MW)"
              legend={
                <Legend
                  items={[
                    { label: "Solar", className: "bg-solar", area: true },
                    { label: "Wind", className: "bg-wind", area: true },
                    { label: "Site demand", className: "border-demand", dashed: true },
                  ]}
                />
              }
            >
              <div className="h-44">
                <ResponsiveContainer>
                  <ComposedChart data={model.rows} syncId={SYNC} margin={{ ...MARGIN, bottom: 4 }}>
                    <CartesianGrid vertical={false} />
                    <XAxis
                      dataKey="x"
                      type="number"
                      domain={[0, 96]}
                      ticks={xTicks}
                      tickFormatter={xTickFormat}
                      tickLine={false}
                      fontSize={12}
                    />
                    <YAxis width={AXIS_W} tickLine={false} axisLine={false} fontSize={12} />
                    <Area
                      dataKey="wind"
                      stackId="p"
                      type="stepAfter"
                      stroke="var(--wind)"
                      fill="var(--wind)"
                      fillOpacity={0.35}
                      strokeWidth={1.5}
                      isAnimationActive={false}
                    />
                    <Area
                      dataKey="solar"
                      stackId="p"
                      type="stepAfter"
                      stroke="var(--solar)"
                      fill="var(--solar)"
                      fillOpacity={0.4}
                      strokeWidth={1.5}
                      isAnimationActive={false}
                    />
                    <Line
                      dataKey="demand"
                      type="stepAfter"
                      stroke="var(--demand)"
                      strokeWidth={2}
                      strokeDasharray="5 3"
                      dot={false}
                      activeDot={false}
                      isAnimationActive={false}
                    />
                    {nowLine(model.now)}
                    <Tooltip content={<PowerTooltip />} cursor={{ stroke: "var(--muted-foreground)", strokeWidth: 1 }} />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            </ChartBlock>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** Recharts only sees direct children of its own types, so call this as a function. */
function nowLine(x: number | null, label = false) {
  if (x == null) return null;
  return (
    <ReferenceLine
      x={x}
      stroke="var(--foreground)"
      strokeWidth={1.5}
      label={label ? { value: `Now ${xTickFormat(x)}`, position: "insideTopRight", fontSize: 12, fill: "var(--foreground)" } : undefined}
    />
  );
}
