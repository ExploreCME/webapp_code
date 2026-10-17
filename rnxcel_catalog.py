"""RNXcel-only draft contracts. No content is published or persisted yet."""

from dataclasses import dataclass, field
from typing import Literal


EXAM_PROFILE = "NCLEX_RN_2026"


@dataclass(frozen=True)
class Category:
    id: str
    label: str


@dataclass(frozen=True)
class ItemExperience:
    slug: str
    label: str
    description: str


ITEM_EXPERIENCES = (
    ItemExperience(
        "single-best-answer", "Single-best-answer",
        "Choose one answer from a set of options.",
    ),
    ItemExperience(
        "select-all-that-apply", "Select-all-that-apply",
        "Choose multiple answers from a set of options.",
    ),
    ItemExperience(
        "unfolding-case-study", "Unfolding case studies",
        "Work through ordered stages as new case information is released.",
    ),
)


@dataclass(frozen=True)
class Option:
    id: str
    text: str


@dataclass(frozen=True)
class NursingItem:
    id: str
    category_id: str
    exam_profile: str = field(default=EXAM_PROFILE, init=False)


@dataclass(frozen=True)
class SingleBestAnswer(NursingItem):
    stem: str
    options: tuple[Option, ...]
    correct_option_id: str
    item_type: Literal["single-best-answer"] = field(
        default="single-best-answer", init=False,
    )


@dataclass(frozen=True)
class SelectAllThatApply(NursingItem):
    stem: str
    options: tuple[Option, ...]
    correct_option_ids: tuple[str, ...]
    item_type: Literal["select-all-that-apply"] = field(
        default="select-all-that-apply", init=False,
    )


@dataclass(frozen=True)
class CaseStage:
    """Only this stage's exhibits are released when its item is reached."""

    id: str
    exhibits: tuple[str, ...]
    item: SingleBestAnswer | SelectAllThatApply


@dataclass(frozen=True)
class UnfoldingCaseStudy(NursingItem):
    title: str
    stages: tuple[CaseStage, ...]
    item_type: Literal["unfolding-case-study"] = field(
        default="unfolding-case-study", init=False,
    )


# Separate from PAXcel's question bank, category normalizers, and statistics.
RNXCEL_CATEGORIES: tuple[Category, ...] = ()
RNXCEL_ITEMS: tuple[
    SingleBestAnswer | SelectAllThatApply | UnfoldingCaseStudy, ...
] = ()
