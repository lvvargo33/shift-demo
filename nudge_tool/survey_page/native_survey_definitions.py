"""Immutable reviewed assets. FTV wording supplied by Chris; Shift verification pending."""
from .native_survey import Condition, Definition, Question, UnsupportedVersion

FTV_V1 = Definition("ftv", 1, "ftv", (
    Question("q1", "How was your first visit overall?", "rating", minimum=1, maximum=5,
             options=(("1", "Awful"), ("5", "Exceptional"))),
    Question("q2", "How likely is it that climbing could become something you do regularly?", "single",
             options=(("unlikely", "Unlikely"), ("not_sure", "Not sure"), ("likely", "Likely"))),
    Question("q3", "Main issue, if any?", "single", options=(
        ("too_expensive", "Too expensive"), ("too_crowded", "Too crowded"),
        ("too_hard", "Climbing felt too hard"), ("intimidating", "It felt intimidating"),
        ("front_desk", "Front desk experience"),
        ("confusing", "Confusing / didn't know where to start"),
        ("routes_not_fun", "The routes weren't fun"), ("no_issues", "No issues"))),
    Question("q4", "Want to tell us more?", "text", required=False),
))

# Verification asset only; this is not a production member survey.
SYNTHETIC_MEMBER_V1 = Definition("synthetic_member", 1, "member", (
    Question("modules", "Synthetic services used", "multi",
             options=(("bouldering", "Bouldering"), ("yoga", "Yoga"))),
    Question("cleanliness", "Synthetic cleanliness rating", "rating"),
    Question("area", "Synthetic area needing improvement", "text", required=False,
             visible_if=Condition("cleanliness", "lte", 6)),
    Question("yoga", "Synthetic yoga rating", "rating",
             visible_if=Condition("modules", "contains", "yoga")),
    Question("recognition", "Synthetic staff recognition", "text", required=False),
))


def get_definition(definition_id: str, version: int) -> Definition:
    for definition in (FTV_V1, SYNTHETIC_MEMBER_V1):
        if (definition.id, definition.version) == (definition_id, version):
            return definition
    raise UnsupportedVersion("unsupported survey definition")
