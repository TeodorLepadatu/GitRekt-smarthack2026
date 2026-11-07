import { useCallback, useEffect, useState } from "react";
import { USE_MOCKS } from "@/api/client";
import { useLiveStream, useStateSnapshot } from "@/api/hooks";
import { TodayChart } from "@/components/chart/TodayChart";
import { ApiBanner } from "@/components/common/ApiBanner";
import { DemoControls } from "@/components/demo/DemoControls";
import { GoalsPanel } from "@/components/goals/GoalsPanel";
import { tabFromHash, TABS, type TabId } from "@/components/layout/tabs";
import { TopBar } from "@/components/layout/TopBar";
import { MetricsCards } from "@/components/metrics/MetricsCards";
import { NowHero } from "@/components/now/NowHero";
import { MoneyCard, OnTrackCard, TrustCard } from "@/components/now/QuestionCards";
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
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);
  return [tab, go] as const;
}

function PageTitle({ tab }: { tab: TabId }) {
  const t = TABS.find((x) => x.id === tab)!;
  return (
    <div className="flex flex-col gap-1">
      <h2 className="text-2xl font-bold sm:text-3xl">{t.label === "Now" ? "Right now" : t.label}</h2>
      <p className="text-muted-foreground">{t.hint}.</p>
    </div>
  );
}

export default function App() {
  const stream = useLiveStream();
  const state = useStateSnapshot();
  const { theme, toggle } = useTheme();
  const [tab, go] = useTab();
  const apiDown = state.isError;

  return (
    <div className="min-h-screen">
      <div className="sticky top-0 z-20">
        <ApiBanner apiDown={apiDown} stream={stream} />
        <TopBar
          tab={tab}
          onTab={go}
          live={stream === "live" && !apiDown}
          stream={stream}
          mocks={USE_MOCKS}
          theme={theme}
          onToggleTheme={toggle}
        />
      </div>

      <main className="mx-auto flex max-w-[1500px] flex-col gap-6 px-4 pt-6 pb-44 sm:px-6 sm:pt-8">
        {tab !== "now" && <PageTitle tab={tab} />}

        {tab === "now" && (
          <>
            <NowHero />
            <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-3">
              <OnTrackCard go={go} />
              <MoneyCard go={go} />
              <TrustCard go={go} />
            </div>
          </>
        )}

        {tab === "today" && (
          <>
            <MetricsCards kind="energy" />
            <TodayChart />
          </>
        )}

        {tab === "goals" && <GoalsPanel />}

        {tab === "history" && (
          <>
            <MetricsCards kind="trust" />
            <DecisionTimeline />
          </>
        )}
      </main>
      <DemoControls />
    </div>
  );
}
