"""RNXcel editorial metadata and legacy immutable authoring contracts."""

from dataclasses import dataclass, field
from typing import Literal


EXAM_PROFILE = "NCLEX_RN_2026"
BLUEPRINT_VERSION = "2026-2029"


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    target_weight: int = 0


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


# Proposed editorial bank allocations, not exact exam percentages or quiz quotas.
RNXCEL_CATEGORIES = (
    Category("MOC", "Management of Care", 18),
    Category("SIC", "Safety and Infection Prevention and Control", 13),
    Category("HPM", "Health Promotion and Maintenance", 9),
    Category("PSI", "Psychosocial Integrity", 9),
    Category("BCC", "Basic Care and Comfort", 9),
    Category("PPT", "Pharmacological and Parenteral Therapies", 16),
    Category("RRP", "Reduction of Risk Potential", 12),
    Category("PAD", "Physiological Adaptation", 14),
)

# Draft/editorial clusters, not official exhaustive NCSBN learning objectives.
OBJECTIVE_CLUSTERS = {
    "MOC": (
        "Prioritization/workload", "Delegation/supervision", "Assignments/staffing",
        "Consent/ethics/rights", "Transitions/coordination", "Handoff/documentation",
        "Quality/accountability", "Advocacy/access",
    ),
    "SIC": (
        "Precautions/exposure prevention", "Asepsis/device infection prevention",
        "Falls/environment", "Identification/procedural safeguards",
        "Restraints/least-restrictive care", "Equipment/workplace safety",
        "Unsafe practice/immediate protection", "Emergency/disaster safety",
    ),
    "HPM": (
        "Prenatal health", "Postpartum/newborn wellness", "Childhood development",
        "Screening/prevention", "Adult/older-adult wellness", "Sexual/reproductive health",
        "Readiness/health literacy", "Family risk/community resources",
    ),
    "PSI": (
        "Therapeutic communication", "Suicide/self-harm safety",
        "Mood/anxiety/psychotic experiences", "Substance use/recovery",
        "Abuse/neglect/trauma", "Grief/end-of-life support",
        "Cognitive/behavioral support", "Coping/caregiver strain",
    ),
    "BCC": (
        "Mobility/positioning", "Nutrition/hydration/feeding", "Elimination",
        "Hygiene/skin protection", "Pain/nonpharmacological comfort", "Sleep",
        "Sensory/functional assistance", "Palliative comfort/dignity",
    ),
    "PPT": (
        "Order review/reconciliation", "Dose/infusion calculations",
        "High-risk medication monitoring", "Routes/techniques", "IV access/infusions",
        "Blood products/parenteral nutrition", "Therapeutic effects/adverse reactions",
        "Medication education/home use",
    ),
    "RRP": (
        "Laboratory trends", "Diagnostic preparation/recovery", "Perioperative surveillance",
        "Early deterioration", "Devices/tubes/drains", "Targeted monitoring",
        "Treatment complications", "Risk-focused education",
    ),
    "PAD": (
        "Respiratory compromise", "Cardiac/perfusion compromise",
        "Fluid/electrolyte/acid-base imbalance", "Renal/urinary adaptation",
        "Endocrine/metabolic crisis", "Neurological instability",
        "Hematologic/bleeding/shock", "GI/hepatic instability",
    ),
}
OBJECTIVES = {
    f"{category}-{index:02d}": label
    for category, labels in OBJECTIVE_CLUSTERS.items()
    for index, label in enumerate(labels, 1)
}
POPULATIONS = ("adult", "older-adult", "pediatric", "maternal-newborn", "multisystem")
SETTINGS = ("acute-care", "community", "long-term-care", "home", "outpatient")
CLINICAL_JUDGMENT = (
    "recognize-cues", "analyze-cues", "prioritize-hypotheses",
    "generate-solutions", "take-action", "evaluate-outcomes",
)


def validate_filters(values):
    """Validate nursing-only setup dimensions; never normalize through PA labels."""
    allowed = {
        "category": tuple(category.id for category in RNXCEL_CATEGORIES),
        "objective": tuple(OBJECTIVES),
        "experience": tuple(item.slug for item in ITEM_EXPERIENCES),
        "population": POPULATIONS,
        "setting": SETTINGS,
        "clinical_judgment": CLINICAL_JUDGMENT,
        "mode": ("practice",),
    }
    result = {}
    for name, choices in allowed.items():
        value = values.get(name, "")
        if value and value not in choices:
            raise ValueError(f"Invalid {name.replace('_', ' ')}.")
        result[name] = value
    if result["category"] and result["objective"] and not result["objective"].startswith(result["category"] + "-"):
        raise ValueError("The objective must belong to the selected category.")
    try:
        result["count"] = int(values.get("count", "10"))
    except (TypeError, ValueError):
        raise ValueError("Choose a whole number of items.") from None
    if not 1 <= result["count"] <= 50:
        raise ValueError("Choose between 1 and 50 scored items.")
    return result


def eligible_versions(versions, filters):
    """Cases match only when all six items meet every chosen dimension."""
    def matches(item):
        if filters["category"] and item["category_id"] != filters["category"]:
            return False
        if filters["objective"] and item["objective_id"] != filters["objective"]:
            return False
        tags = item.get("tags", {})
        return all(
            not filters[name] or filters[name] in tags.get(name, [])
            for name in ("population", "setting", "clinical_judgment")
        )

    result = []
    for version in versions:
        if filters["experience"] and version["type"] != filters["experience"]:
            continue
        items = (
            [stage["item"] for stage in version["stages"]]
            if version["type"] == "unfolding-case-study" else [version]
        )
        if all(matches(item) for item in items):
            result.append(version)
    return result


# Production content remains empty; published records live only in nursing storage.
RNXCEL_ITEMS: tuple[
    SingleBestAnswer | SelectAllThatApply | UnfoldingCaseStudy, ...
] = ()
