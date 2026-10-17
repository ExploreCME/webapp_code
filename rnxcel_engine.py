"""Validated, deterministic nursing assessment content and learner projections."""

from copy import deepcopy
import json

from rnxcel_catalog import CLINICAL_JUDGMENT, POPULATIONS, SETTINGS


EXAM_PROFILE = "NCLEX_RN_2026"
CATEGORIES = frozenset(("MOC", "SIC", "HPM", "PSI", "BCC", "PPT", "RRP", "PAD"))
ITEM_TYPES = frozenset(("single-best-answer", "select-all-that-apply"))
TAG_DIMENSIONS = (
    "population", "setting", "specialty", "organ_topics", "nursing_tasks",
    "clinical_judgment",
)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _strings(value, name, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{name} must be a list")
    for entry in value:
        _text(entry, name)
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must be unique")


def _provenance(authored):
    value = authored.get("provenance")
    if "provenance" in authored and (
        not isinstance(value, dict) or any(not isinstance(key, str) for key in value)
    ):
        raise ValueError("provenance must be an object with string keys")


def _item(item):
    if (not isinstance(item, dict) or not isinstance(item.get("type"), str)
            or item["type"] not in ITEM_TYPES):
        raise ValueError("Invalid item type")
    category = item.get("category_id")
    if not isinstance(category, str) or category not in CATEGORIES:
        raise ValueError("Invalid category_id")
    if not isinstance(item.get("objective_id"), str) or item["objective_id"] not in {
        f"{category}-{number:02d}" for number in range(1, 9)
    }:
        raise ValueError("Invalid objective_id")
    _text(item.get("stem"), "stem")
    options = item.get("options")
    if not isinstance(options, list) or len(options) < 2:
        raise ValueError("options must contain at least two choices")
    ids = []
    for option in options:
        if not isinstance(option, dict):
            raise ValueError("Invalid option")
        _text(option.get("id"), "option id")
        _text(option.get("text"), "option text")
        ids.append(option["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Option IDs must be unique")
    keys = item.get("correct_option_ids")
    _strings(keys, "correct_option_ids", nonempty=True)
    if not set(keys).issubset(ids):
        raise ValueError("Unknown correct option ID")
    single = item["type"] == "single-best-answer"
    if single and len(keys) != 1:
        raise ValueError("Single-best-answer requires exactly one key")
    if item.get("scoring") != ("single" if single else "plus-minus"):
        raise ValueError("Invalid scoring rule")
    _text(item.get("rationale"), "rationale")
    tags = item.get("tags")
    if not isinstance(tags, dict):
        raise ValueError("tags must be an object")
    for dimension in TAG_DIMENSIONS:
        _strings(tags.get(dimension), dimension)
    for dimension, allowed in (
        ("population", POPULATIONS), ("setting", SETTINGS),
        ("clinical_judgment", CLINICAL_JUDGMENT),
    ):
        if not set(tags[dimension]).issubset(allowed):
            raise ValueError(f"Invalid {dimension} tag")
    if "official_activity_refs" in tags:
        _strings(tags["official_activity_refs"], "official_activity_refs")
        if tags["official_activity_refs"] and tags.get("official_activity_reviewed") is not True:
            raise ValueError("Official activity references require editorial review")
    if ("official_activity_reviewed" in tags
            and type(tags["official_activity_reviewed"]) is not bool):
        raise ValueError("official_activity_reviewed must be a boolean")
    _provenance(item)
    if "exhibits" in item:
        _exhibits(item["exhibits"])


def _exhibits(exhibits):
    if not isinstance(exhibits, list):
        raise ValueError("exhibits must be a list")
    for exhibit in exhibits:
        if not isinstance(exhibit, dict):
            raise ValueError("Invalid exhibit")
        _text(exhibit.get("timestamp"), "exhibit timestamp")
        _text(exhibit.get("text"), "exhibit text")


def validate_version(authored):
    """Validate a private authored version; return a detached JSON snapshot."""
    if not isinstance(authored, dict):
        raise ValueError("Version must be an object")
    _text(authored.get("version_id"), "version_id")
    if len(authored["version_id"]) > 128:
        raise ValueError("version_id is too long")
    if (not authored["version_id"].isascii()
            or authored["version_id"] != authored["version_id"].strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in authored["version_id"])):
        raise ValueError("version_id must be a printable ASCII identifier")
    if authored.get("exam_profile") != EXAM_PROFILE:
        raise ValueError("Invalid exam_profile")
    _provenance(authored)
    if authored.get("type") == "unfolding-case-study":
        _text(authored.get("title"), "title")
        stages = authored.get("stages")
        if not isinstance(stages, list) or len(stages) != 6:
            raise ValueError("A case requires six stages")
        stage_ids = []
        for stage in stages:
            if not isinstance(stage, dict):
                raise ValueError("Invalid case stage")
            _text(stage.get("id"), "stage id")
            stage_ids.append(stage["id"])
            _exhibits(stage.get("exhibits"))
            _item(stage.get("item"))
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("Stage IDs must be unique")
    else:
        _item(authored)
    try:
        return json.loads(json.dumps(authored, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Version must contain JSON values") from exc


def flatten(snapshot):
    """Return scored items with private keys and cumulative case exhibits."""
    if not isinstance(snapshot, list) or not snapshot:
        raise ValueError("Snapshot must be a nonempty list")
    result = []
    for raw in snapshot:
        version = validate_version(raw)
        if version["type"] != "unfolding-case-study":
            result.append(version)
            continue
        exhibits = []
        for stage_index, stage in enumerate(version["stages"]):
            exhibits.extend(
                {"timestamp": entry["timestamp"], "text": entry["text"]}
                for entry in stage["exhibits"]
            )
            item = deepcopy(stage["item"])
            item.update(
                version_id=version["version_id"], exam_profile=EXAM_PROFILE,
                case_title=version["title"], stage_id=stage["id"],
                stage_index=stage_index, stage_count=6,
                exhibits=deepcopy(exhibits),
            )
            result.append(item)
    return result


def _public(item):
    result = {
        name: deepcopy(item[name]) for name in (
            "version_id", "exam_profile", "type", "category_id", "objective_id",
            "stem", "case_title", "stage_id", "stage_index", "stage_count",
        ) if name in item
    }
    result["options"] = [
        {"id": option["id"], "text": option["text"]} for option in item["options"]
    ]
    result["tags"] = {
        name: deepcopy(item["tags"][name])
        for name in (*TAG_DIMENSIONS, "official_activity_refs")
        if name in item["tags"]
    }
    result["exhibits"] = [
        {"timestamp": entry["timestamp"], "text": entry["text"]}
        for entry in item.get("exhibits", [])
    ]
    return result


def learner_item(snapshot, index):
    items = flatten(snapshot)
    if type(index) is not int or not 0 <= index < len(items):
        raise ValueError("Invalid item index")
    return _public(items[index])


def score(item, selections):
    """Score a nonempty selection of stable option IDs without side effects."""
    _item(item)
    _strings(selections, "selections", nonempty=True)
    options = {entry["id"] for entry in item["options"]}
    if not set(selections).issubset(options):
        raise ValueError("Unknown selected option ID")
    keys = set(item["correct_option_ids"])
    if item["scoring"] == "single":
        if len(selections) != 1:
            raise ValueError("Select exactly one answer")
        return int(selections[0] in keys), 1
    selected = set(selections)
    return max(0, len(selected & keys) - len(selected - keys)), len(keys)


def review(snapshot, responses):
    """Reveal keys and rationales only for a complete, sequential response set."""
    items = flatten(snapshot)
    if not isinstance(responses, list) or len(responses) != len(items):
        raise ValueError("Review requires a completed attempt")
    result = []
    for index, (item, response) in enumerate(zip(items, responses)):
        if (not isinstance(response, dict)
                or type(response.get("index")) is not int
                or response["index"] != index):
            raise ValueError("Invalid review response sequence")
        selections = response.get("selections")
        earned, possible = score(item, selections)
        public = _public(item)
        public.update(
            index=index, correct_option_ids=deepcopy(item["correct_option_ids"]),
            rationale=item["rationale"], selections=deepcopy(selections),
            earned=earned, possible=possible,
        )
        result.append(public)
    return result
