from flask import Blueprint, abort, render_template

from rnxcel_catalog import EXAM_PROFILE, ITEM_EXPERIENCES, RNXCEL_CATEGORIES


rnxcel_bp = Blueprint("rnxcel", __name__, url_prefix="/rnxcel")


@rnxcel_bp.route("")
@rnxcel_bp.route("/")
def landing():
    return render_template(
        "rnxcel.html", exam_profile=EXAM_PROFILE,
        experiences=ITEM_EXPERIENCES, categories=RNXCEL_CATEGORIES,
    )


@rnxcel_bp.route("/practice/<item_type>")
def practice(item_type):
    experience = next(
        (item for item in ITEM_EXPERIENCES if item.slug == item_type), None,
    )
    if experience is None:
        abort(404)
    return render_template(
        "rnxcel.html", exam_profile=EXAM_PROFILE,
        experiences=ITEM_EXPERIENCES, categories=RNXCEL_CATEGORIES,
        experience=experience,
    )
