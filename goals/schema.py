"""Goal config: the contract from the goal parser to the feature builder, guard and settlement."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

Objective = Literal["max_profit", "self_consumption"]
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class ReserveGoal(BaseModel):
    """Have at least `mwh` stored by `by` and keep it until `until`.

    until == by is a plain deadline; until < by wraps past midnight (e.g. 22:00 -> 06:00 overnight).
    """

    id: str
    type: Literal["reserve"] = "reserve"
    mwh: float = Field(gt=0)
    by: HHMM
    until: HHMM
    hard: bool = True
    source_text: str = ""


class MinSellPriceGoal(BaseModel):
    """Never discharge to the grid while the price is below `eur_per_mwh`."""

    id: str
    type: Literal["min_sell_price"] = "min_sell_price"
    eur_per_mwh: float
    hard: bool = True
    source_text: str = ""


Goal = Annotated[Union[ReserveGoal, MinSellPriceGoal], Field(discriminator="type")]


class GoalConfig(BaseModel):
    objective: Objective = "max_profit"
    goals: list[Goal] = Field(default_factory=list)
