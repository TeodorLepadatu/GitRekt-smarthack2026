import { useState, type FormEvent } from "react";
import { useAddGoal, useDeleteGoal, useGoals } from "@/api/hooks";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { GoalItem } from "./GoalItem";

export function GoalsPanel() {
  const goals = useGoals();
  const add = useAddGoal();
  const del = useDeleteGoal();
  const [text, setText] = useState("");
  const [exchange, setExchange] = useState<{ asked: string; reply: string; ok: boolean } | null>(null);

  async function submit(value: string) {
    const t = value.trim();
    if (!t || add.isPending) return;
    setExchange(null);
    setText(""); // the goal shows in the list at once, while it is being read
    try {
      const res = await add.mutateAsync(t);
      if (res.question) {
        setText(t); // back in the box, so it can be made clearer
        setExchange({ asked: t, reply: res.question, ok: false });
      } else {
        setExchange({ asked: t, reply: "Added. It is checked from the next interval on.", ok: true });
      }
    } catch {
      setText(t); /* the error is shown below via add.isError */
    }
  }

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void submit(text);
  };

  const pendingText = add.isPending ? add.variables : undefined;
  const list = goals.data ?? [];

  return (
    <div className="grid gap-x-14 gap-y-10 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
      <div className="flex flex-col gap-6">
        <form onSubmit={onSubmit} className="flex flex-col gap-2">
          <label htmlFor="goal-input" className="label">
            New goal
          </label>
          <div className="flex border border-ink bg-sheet focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-ink">
            <input
              id="goal-input"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Store 6 MWh by 18:00"
              maxLength={200}
              className="min-w-0 flex-1 bg-transparent px-3 py-3 font-serif text-lg outline-none placeholder:text-ink-3"
            />
            <button
              type="submit"
              disabled={!text.trim() || add.isPending}
              className="border-l border-ink bg-ink px-5 text-sm font-semibold text-paper disabled:opacity-40"
            >
              {add.isPending ? "Adding…" : "Add"}
            </button>
          </div>
        </form>

        {exchange && (
          <div className="flex flex-col gap-2 text-sm" aria-live="polite">
            <p className="text-ink-3">
              You wrote: <span className="text-ink">“{exchange.asked}”</span>
            </p>
            <p className={exchange.ok ? "text-ok" : "font-serif text-lg leading-snug text-ink"}>
              {!exchange.ok && <span className="label mr-2 align-middle">Question</span>}
              {exchange.reply}
            </p>
          </div>
        )}
        {add.isError && <PanelError message="The goal could not be saved. Try again in a moment." />}

        <dl className="grid gap-4 border-t border-rule pt-4 text-sm">
          <div>
            <dt className="font-semibold">Must be kept</dt>
            <dd className="text-ink-2">
              Hard goals. The safety layer blocks any decision that would break one, even if the AI proposes it.
            </dd>
          </div>
          <div>
            <dt className="font-semibold">Preference</dt>
            <dd className="text-ink-2">Soft goals, written with “try”, “ideally” or “if possible”. The AI aims for them but can trade them off for money.</dd>
          </div>
        </dl>
      </div>

      <div className="flex flex-col">
        <span className="label mb-1">Active goals</span>
        {goals.isPending ? (
          <PanelLoading rows={4} />
        ) : goals.isError && !goals.data ? (
          <PanelError what="your goals" onRetry={() => goals.refetch()} />
        ) : list.length === 0 && !pendingText ? (
          <p className="py-4 font-serif text-lg text-ink-2">No goals. The battery is run purely for profit until you add one.</p>
        ) : (
          <ul>
            {list.map((g) => (
              <GoalItem key={g.id} goal={g} onDelete={(id) => del.mutate(id)} deleting={del.isPending && del.variables === g.id} />
            ))}
            {pendingText && (
              <li className="grid gap-y-1.5 border-t border-rule py-5 first:border-t-0" aria-live="polite" aria-busy="true">
                <p className="font-serif text-2xl leading-snug text-ink-2">{pendingText}</p>
                <p className="text-sm text-ink-3">Reading your goal…</p>
              </li>
            )}
          </ul>
        )}
        {del.isError && <PanelError message="The goal could not be removed. Try again in a moment." />}
      </div>
    </div>
  );
}
