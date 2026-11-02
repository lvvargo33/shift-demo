"""Reusable optional module instruments, version 1. Wording provisional for approval.

No gym identity or delivery behavior. Reuse these same IDs, wording and scales
across gyms; revise question/module assets when semantics change.
"""
from dataclasses import dataclass
from .member_instrument import rating, text, SATISFACTION_ENDPOINTS
from .native_survey_presentation import Module, Page, Group

@dataclass(frozen=True)
class OptionalModuleInstrument:
    id: str
    version: int
    label: str
    questions: tuple

    @property
    def module(self):
        return Module(self.id, tuple(q.id for q in self.questions))

    @property
    def page(self):
        return Page(self.id, self.label, (Group(self.id + '_questions',
            'How satisfied are you with the following? (Provisional wording — pending approval)',
            self.module.questions),), module=self.id, finish=True)


def optional_module(mid, label, measures):
    return OptionalModuleInstrument(mid, 1, label, (
        *(rating(qid, wording, mid, SATISFACTION_ENDPOINTS) for qid, wording in measures),
        text(mid + '_why', 'Please tell us why.', mid),
    ))

CLIMBING_CLASSES_V1 = optional_module('climbing_classes', 'Climbing Classes', (
    ('climbing_classes_overall', 'The overall climbing class experience'),
    ('climbing_classes_instruction', 'The quality of climbing class instruction'),
))
YOGA_V1 = optional_module('yoga', 'Yoga', (
    ('yoga_overall', 'The overall yoga experience'),
    ('yoga_instruction', 'The quality of yoga instruction'),
))
CAFE_V1 = optional_module('cafe', 'Café', (
    ('cafe_overall', 'Overall Café Experience'),
    ('cafe_food_beverages', 'Quality of Food and Beverages'),
))
EVENTS_V1 = optional_module('events', 'Events', (
    ('events_overall', 'Overall Events Experience'),
    ('events_frequency', 'Frequency of Events Offered'),
))
OPTIONAL_MODULES_V1 = (CLIMBING_CLASSES_V1, YOGA_V1, CAFE_V1, EVENTS_V1)
