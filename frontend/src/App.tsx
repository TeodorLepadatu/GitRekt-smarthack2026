import { useCallback, useEffect, useState, type CSSProperties } from "react";
import { MotionConfig } from "motion/react";
import { USE_MOCKS } from "@/api/client";
import { useDay, useLiveStream, useStateSnapshot } from "@/api/hooks";
import { TodayChart } from "@/components/chart/TodayChart";
import { ApiBanner } from "@/components/common/ApiBanner";
import { OperatorStrip } from "@/components/control/OperatorStrip";
import { Reveal } from "@/components/common/Reveal";
import { DemoControls } from "@/components/demo/DemoControls";
import { GoalsPanel } from "@/components/goals/GoalsPanel";
import { tabFromHash, TABS, type TabId } from "@/components/layout/tabs";
import { TopBar } from "@/components/layout/TopBar";
import { MetricsCards } from "@/components/metrics/MetricsCards";
import { NowPage } from "@/components/now/NowPage";
import { DecisionTimeline } from "@/components/timeline/DecisionTimeline";
import { useTheme } from "@/hooks/useTheme";

/** Tab lives in the URL hash so refresh and the browser back button keep it. */
function useTab() {
  const [tab, setTab] = useState<TabId>(tabFromHash);
  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = useCallback((t: TabId) => {
    if (window.location.hash !== `#${t}`) window.location.hash = t;
    setTab(t);
    window.scrollTo({ top: 0 });
  }, []);
  return [tab, go] as const;
}

/** Page title with a big tilted section number behind it, off the grid. */
function PageIntro({ tab }: { tab: TabId }) {
  const i = TABS.findIndex((x) => x.id === tab);
  const t = TABS[i];
  return (
    <div className="relative flex max-w-[62ch] flex-col gap-3 pt-6 lg:ml-[8%]">
      <span aria-hidden className="ghost-text -top-6 -left-[12%] text-[clamp(8rem,16vw,14rem)]">
        {String(i + 1).padStart(2, "0")}
      </span>
      <h1 className="enter relative font-serif text-5xl leading-tight font-semibold tracking-tight xl:text-6xl" style={{ ["--i" as string]: 0 } as CSSProperties}>
        {t.title}
      </h1>
      {t.intro && (
        <p className="enter relative text-lg text-ink-2" style={{ ["--i" as string]: 1 } as CSSProperties}>
          {t.intro}
        </p>
      )}
    </div>
  );
}

export default function App() {
  const stream = useLiveStream();
  const state = useStateSnapshot();
  const day = useDay();
  const { theme, toggle } = useTheme();
  const [tab, go] = useTab();
  const apiDown = state.isError;

  return (
    <MotionConfig reducedMotion="user">
    <div className="min-h-screen overflow-x-clip">
      <ApiBanner apiDown={apiDown} stream={stream} />
      <TopBar
        tab={tab}
        onTab={go}
        live={stream === "live" && !apiDown}
        stream={stream}
        mocks={USE_MOCKS}
        theme={theme}
        onToggleTheme={toggle}
        now={state.data?.current?.time ?? null}
        date={day.data?.date ?? null}
      />

      <main key={tab} className="flex flex-col gap-12 px-6 pt-10 pb-44 lg:px-12">
        {tab !== "now" && <PageIntro tab={tab} />}

        {tab === "now" && <NowPage go={go} />}

        {tab === "day" && (
          <>
            <Reveal>
              <MetricsCards kind="energy" />
            </Reveal>
            <Reveal delay={0.1}>
              <TodayChart />
            </Reveal>
          </>
        )}

        {tab === "goals" && (
          <Reveal>
            <GoalsPanel />
          </Reveal>
        )}

        {tab === "decisions" && (
          <>
            <Reveal>
              <MetricsCards kind="trust" />
            </Reveal>
            <Reveal delay={0.1}>
              <DecisionTimeline />
            </Reveal>
          </>
        )}
      </main>
      {/* Operator strip on every tab, the demo controls under it. */}
      <div className="fixed inset-x-0 bottom-0 z-30">
        <OperatorStrip />
        <DemoControls />
      </div>
    </div>
    </MotionConfig>
  );
}
