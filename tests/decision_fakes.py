"""A scripted decision model, in place of TypeSafe (`app.llm.decisions`).

`ScriptedDecisionModel` is a real Pydantic AI `DecisionModel` whose `decide`
(the one method that would reach the service) answers from a script, so the
whole path from answer type to questions to the typed output runs as it does
against Jev. The `decision_model` fixture puts it behind
`app.llm.decisions.build_decision_model`.

A script step is one of:

- a dict of plain answers by field: `{"allowed": False, "broken": "never_1"}`
  (a yes/no is a `bool`, a pick-one its label, a score its level);
- an exception instance, raised (`UnfillableRoute(...)` for a hand-off);
- `Delay(seconds)`, which sleeps and then answers with the next step.

`calls` records each `DecisionRequest`: its `state` (the evidence) and its
`questions`.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic_ai.models.decision import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionModel,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
    UnfillableRoute,
)
from pydantic_ai.usage import RequestUsage

import app.llm.decisions as decisions_module

FAKE_MODEL_NAME = "jev-fake"
PROMPT_TOKENS = 21
COMPLETION_TOKENS = 4


@dataclass
class Delay:
    seconds: float


def hand_off() -> UnfillableRoute:
    """The hand-off a decision model raises for a step it cannot answer."""
    return UnfillableRoute(FAKE_MODEL_NAME, "output", 1.0)


def _answer(question: Any, value: Any) -> Any:
    if isinstance(question, NoulQuestion):
        return NoulAnswer(noul=0.95 if value else 0.05)
    if isinstance(question, ChoiceQuestion):
        assert value in question.criteria, (value, list(question.criteria))
        probabilities = {label: (1.0 if label == value else 0.0) for label in question.criteria}
        return ChoiceAnswer(choice=value, confidence=1.0, probabilities=probabilities)
    if isinstance(question, ScoreQuestion):
        levels = range(len(question.criteria))
        probabilities = {level: (1.0 if level == value else 0.0) for level in levels}
        return ScoreAnswer(score=float(value), confidence=1.0, probabilities=probabilities)
    raise AssertionError(f"unexpected question {question!r}")


class ScriptedDecisionModel(DecisionModel):
    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, fake: "FakeDecisions") -> None:
        super().__init__()
        self._fake = fake

    @property
    def model_name(self) -> str:
        return FAKE_MODEL_NAME

    @property
    def system(self) -> str:
        return "typesafe"

    @property
    def base_url(self) -> str:
        return "http://decision.fake"

    async def decide(self, request: DecisionRequest, model_settings: Any) -> DecisionResponse:
        self._fake.calls.append(request)
        if not self._fake.script:
            raise AssertionError("decision model called with nothing scripted")
        step = self._fake.script.pop(0)
        if isinstance(step, Delay):
            await asyncio.sleep(step.seconds)
            step = self._fake.script.pop(0) if self._fake.script else {}
        if isinstance(step, BaseException):
            raise step
        answers = {name: _answer(q, step[name]) for name, q in request.questions.items()}
        return DecisionResponse(
            answers=answers,
            model_name=FAKE_MODEL_NAME,
            usage=RequestUsage(input_tokens=PROMPT_TOKENS, output_tokens=COMPLETION_TOKENS),
        )


@dataclass
class FakeDecisions:
    script: list[Any] = field(default_factory=list)
    calls: list[DecisionRequest] = field(default_factory=list)
    targets: list[Any] = field(default_factory=list)
    max_choice_options: int | None = 255

    def answer(self, *steps: Any) -> None:
        self.script.extend(steps)

    def build(self, target: Any) -> ScriptedDecisionModel:
        self.targets.append(target)
        model = ScriptedDecisionModel(self)
        model.max_choice_options = self.max_choice_options  # type: ignore[misc]
        return model


@pytest.fixture
def decision_model(monkeypatch):
    """Replace `build_decision_model` with a scripted fake; returns its controller."""
    fake = FakeDecisions()
    monkeypatch.setattr(decisions_module, "build_decision_model", fake.build)
    return fake
