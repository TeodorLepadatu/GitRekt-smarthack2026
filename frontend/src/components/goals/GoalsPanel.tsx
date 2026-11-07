import { useState, type FormEvent } from "react";
import { Loader2, Lock, MessageCircleQuestion, Send, Sparkles, Sprout } from "lucide-react";
import { useAddGoal, useDeleteGoal, useGoals } from "@/api/hooks";
import { PanelError, PanelLoading } from "@/components/common/PanelState";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { GOAL_EXAMPLES } from "@/lib/labels";
import { GoalItem } from "./GoalItem";

export function GoalsPanel() {
  const goals = useGoals();
  const add = useAddGoal();
  const del = useDeleteGoal();
  const [text, setText] = useState("");
  const [question, setQuestion] = useState<{ asked: string; reply: string } | null>(null);
  const [added, setAdded] = useState<string | null>(null);

  async function submit(value: string) {
    const t = value.trim();
    if (!t || add.isPending) return;
    setQuestion(null);
    setAdded(null);
    try {
      const res = await add.mutateAsync(t);
      if (res.question) setQuestion({ asked: t, reply: res.question });
      else {
        setText("");
        setAdded(t);
      }
    } catch {
      /* shown below via add.isError */
    }
  }

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void submit(text);
  };

  return (
    <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles className="size-5 text-solar" /> Tell the system what you want
          </CardTitle>
          <CardDescription>Write it the way you would say it to a colleague. The system turns it into a rule.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <form onSubmit={onSubmit} className="flex gap-2">
            <Input
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="e.g. Store 6 MWh by 18:00"
              aria-label="Tell the system what you want"
              maxLength={200}
              className="h-12 rounded-xl text-base"
            />
            <Button type="submit" disabled={!text.trim() || add.isPending} className="h-12 rounded-xl px-5" aria-label="Add goal">
              {add.isPending ? <Loader2 className="animate-spin" /> : <Send />}
              <span className="hidden sm:inline">Add</span>
            </Button>
          </form>

          <div className="flex flex-col gap-2">
            <span className="text-xs font-semibold text-muted-foreground">Or try one of these:</span>
            <div className="flex flex-wrap gap-2">
              {GOAL_EXAMPLES.map((ex) => (
                <button
                  key={ex}
                  type="button"
                  onClick={() => {
                    setText(ex);
                    void submit(ex);
                  }}
                  disabled={add.isPending}
                  className="rounded-full border bg-secondary px-3.5 py-1.5 text-sm font-medium text-secondary-foreground transition-colors hover:border-foreground/30 hover:bg-accent disabled:opacity-50"
                >
                  {ex}
                </button>
              ))}
            </div>
          </div>

          {question && (
            <div className="flex flex-col gap-2" aria-live="polite">
              <div className="ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-primary px-4 py-2.5 text-sm text-primary-foreground">
                {question.asked}
              </div>
              <div className="flex max-w-[90%] gap-2 rounded-2xl rounded-bl-md border border-info/30 bg-info/10 px-4 py-2.5 text-sm">
                <MessageCircleQuestion className="mt-0.5 size-4 shrink-0 text-info" />
                <span>{question.reply}</span>
              </div>
            </div>
          )}
          {added && (
            <p className="rounded-xl bg-ok/10 px-4 py-2.5 text-sm text-ok" aria-live="polite">
              Got it — “{added}” is now an active goal.
            </p>
          )}
          {add.isError && <PanelError message="Could not save your goal. Please try again." className="p-3" />}

          <div className="grid gap-3 border-t pt-5 text-sm sm:grid-cols-2">
            <div className="flex gap-2.5">
              <Lock className="mt-0.5 size-4 shrink-0 text-foreground" />
              <p>
                <b>HARD</b> goals are always kept. The safety layer blocks any decision that would break them.
              </p>
            </div>
            <div className="flex gap-2.5">
              <Sprout className="mt-0.5 size-4 shrink-0 text-foreground" />
              <p>
                <b>SOFT</b> goals are preferences. The AI aims for them but may trade them off for profit.
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Active goals</CardTitle>
          <CardDescription>Live status, updated every 15 minutes.</CardDescription>
        </CardHeader>
        <CardContent>
          {goals.isPending ? (
            <PanelLoading rows={4} />
          ) : goals.isError && !goals.data ? (
            <PanelError what="your goals" onRetry={() => goals.refetch()} />
          ) : goals.data.length === 0 ? (
            <p className="rounded-xl border border-dashed p-6 text-sm text-muted-foreground">
              No goals yet. The system will simply try to make the most money. Add a goal on the left or pick an example.
            </p>
          ) : (
            <ul className="flex flex-col gap-3">
              {goals.data.map((g) => (
                <GoalItem key={g.id} goal={g} onDelete={(id) => del.mutate(id)} deleting={del.isPending && del.variables === g.id} />
              ))}
            </ul>
          )}
          {del.isError && <PanelError message="Could not delete the goal. Please try again." className="mt-3 p-3" />}
        </CardContent>
      </Card>
    </div>
  );
}
