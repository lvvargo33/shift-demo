"""Shift meeting configuration v1, derived additively from immutable ABC V4.

New wording is provisional pending Shift approval. Circuit identity is separate
from ABC V-grades; no implicit conversion or benchmark equivalence is defined.
"""
from dataclasses import replace
from .member_instrument import (ABC_MEMBER_V4, ABC_PRESENTATION_V3, MODULES,
    SATISFACTION, SATISFACTION_ENDPOINTS)
from .member_module_extensions import OPTIONAL_MODULES_V1
from .native_survey import Condition, Definition, Question
from .native_survey_presentation import Module, ModuleConfig, Presentation, Page, Group

SHIFT_MODULE_IDS = ('bouldering', 'training_area', 'climbing_classes', 'yoga', 'cafe', 'events', 'youth_programs')
SHIFT_DEFAULT_MODULES = SHIFT_MODULE_IDS[:-1]
CIRCUITS = ('Yellow', 'Orange', 'Red', 'Pink', 'Purple', 'Blue', 'Black', 'White')
PROVISIONAL = 'Provisional wording — pending Shift approval'
BOULDERING_USED = Question('bouldering_used',
    'Have you used the bouldering facilities in the past 90 days?', 'single', required=False,
    options=(('used', 'Yes'), ('not_used', "I haven’t used the bouldering facilities")))
BOULDERING_CIRCUIT = Question('bouldering_circuit',
    'Which circuit do you climb most often?', 'single', required=False,
    options=tuple((c.lower(), c) for c in CIRCUITS),
    visible_if=Condition('bouldering_used', 'eq', 'used'))
MOVEMENT_QUALITY = Question('shift_bouldering_movement_quality',
    'The quality of the movements on the boulders', 'rating', required=False,
    options=SATISFACTION_ENDPOINTS, visible_if=Condition('bouldering_used', 'eq', 'used'))
BOULDERING_QUESTIONS = (BOULDERING_USED, BOULDERING_CIRCUIT,
    *(replace(q, version=q.version + 1, visible_if=Condition('bouldering_used', 'eq', 'used'))
      for q in ABC_MEMBER_V4.questions if q.id.startswith('bouldering_') and q.id != 'bouldering_grade'),
    MOVEMENT_QUALITY)
BOULDERING_MODULE = Module('bouldering', tuple(q.id for q in BOULDERING_QUESTIONS))
BOULDERING_PAGE = Page('bouldering', 'Bouldering', (
    Group('bouldering_usage', PROVISIONAL, ('bouldering_used',)),
    Group('circuit', PROVISIONAL, ('bouldering_circuit',)),
    Group('bouldering_ratings', SATISFACTION, tuple(q.id for q in BOULDERING_QUESTIONS[2:6])),
    Group('bouldering_comment', '', ('bouldering_why',)),
    Group('movement_quality', SATISFACTION + ' (' + PROVISIONAL + ')', (MOVEMENT_QUALITY.id,)),
), module='bouldering', finish=True)


def build_shift_assets(enabled):
    """All 128 availability masks have explicit immutable v1 identities/pins."""
    mask = sum(1 << i for i, mid in enumerate(SHIFT_MODULE_IDS) if mid in enabled)
    suffix = str(mask)
    modules = {'bouldering': BOULDERING_MODULE, **{m.id: m for m in MODULES[1:]},
               **{m.id: m.module for m in OPTIONAL_MODULES_V1}}
    pages = {p.module: p for p in ABC_PRESENTATION_V3.pages if p.module}
    pages.update({m.id: m.page for m in OPTIONAL_MODULES_V1})
    pages['bouldering'] = BOULDERING_PAGE
    questions = {m.id: tuple(q for q in ABC_MEMBER_V4.questions if q.id in m.questions) for m in MODULES[1:]}
    questions.update({m.id: m.questions for m in OPTIONAL_MODULES_V1})
    questions['bouldering'] = BOULDERING_QUESTIONS
    labels = {'training_area': 'Training Area', 'youth_programs': 'Youth Programs / Camps, as a parent or guardian',
              **{m.id: m.label for m in OPTIONAL_MODULES_V1}}
    core = tuple(replace(q, version=q.version + 1,
                 options=tuple((mid, labels[mid]) for mid in SHIFT_MODULE_IDS if mid != 'bouldering' and mid in enabled) + (('none', 'None of these'),))
                 if q.id == 'modules_used' else q
                 for q in ABC_MEMBER_V4.questions if not any(q.id in m.questions for m in MODULES))
    active = tuple(mid for mid in SHIFT_MODULE_IDS if mid in enabled)
    definition = Definition('shift_member_modules_' + suffix, 1, 'member',
        core + tuple(q for mid in active for q in questions[mid]))
    config = ModuleConfig('shift_modules_' + suffix, 1,
        tuple(mid for mid in active if mid == 'bouldering'), tuple(mid for mid in active if mid != 'bouldering'))
    selected_pages = list(ABC_PRESENTATION_V3.pages[:2]) + [pages[mid] for mid in active]
    selected_pages[-1] = replace(selected_pages[-1], finish=True)
    presentation = Presentation('shift_member_pages_' + suffix, 1, tuple(selected_pages))
    selected_modules = tuple(modules[mid] for mid in active)
    presentation.validate(definition, config, selected_modules)
    return definition, presentation, config, selected_modules

SHIFT_ASSETS_V1 = {}
for mask in range(1 << len(SHIFT_MODULE_IDS)):
    assets = build_shift_assets(tuple(mid for i, mid in enumerate(SHIFT_MODULE_IDS) if mask & (1 << i)))
    SHIFT_ASSETS_V1[(assets[0].id, assets[0].version)] = assets


# Approved instrument. Legacy v1 above remains byte-for-byte pinned.
from dataclasses import dataclass
from .native_survey_presentation import ExclusiveQuestion

@dataclass(frozen=True)
class CircuitQuestion(ExclusiveQuestion):
    max_selections: int = 2

CIRCUIT_V2 = CircuitQuestion('bouldering_circuit',
    'Which circuits do you climb most often?', 'multi', required=False, version=2,
    options=tuple((c.lower(), c) for c in CIRCUITS) +
            (('not_used', "I haven't bouldered in the past 90 days"),),
    exclusive_options=('not_used',))


def build_shift_assets_v2(enabled):
    old_d, old_p, old_c, old_m = build_shift_assets(enabled)
    gate = Condition('bouldering_circuit', 'not_contains', 'not_used')
    wording = {
        'climbing_classes_instruction': 'The quality of climbing class staff',
        'yoga_instruction': 'The quality of yoga staff',
        'cafe_overall': 'The overall cafe experience',
        'cafe_food_beverages': 'The quality of food and beverages',
        'events_overall': 'The overall events experience',
        'events_frequency': 'The frequency of events offered',
    }
    questions = []
    for q in old_d.questions:
        if q.id == 'bouldering_used':
            continue
        if q.id == 'bouldering_circuit':
            q = CIRCUIT_V2
        elif q.id.startswith('bouldering_') or q.id == MOVEMENT_QUALITY.id:
            # Same satisfaction meanings and history; only the entry gate changes.
            q = replace(q, visible_if=gate)
        elif q.id in wording:
            q = replace(q, wording=wording[q.id], version=q.version +
                (1 if q.id in {'climbing_classes_instruction', 'yoga_instruction'} else 0))
        elif q.id == 'modules_used':
            q = replace(q, options=tuple((v, label.replace('Café', 'Cafe')) for v, label in q.options))
        questions.append(q)
    pages = []
    for page in old_p.pages:
        if page.module == 'bouldering':
            page = replace(page, groups=(
                Group('circuit', 'Select up to two circuits.', ('bouldering_circuit',)),
                Group('bouldering_ratings', SATISFACTION,
                    ('bouldering_quantity', 'bouldering_variety', 'bouldering_grading_consistency',
                     'bouldering_reset_frequency', MOVEMENT_QUALITY.id)),
                Group('bouldering_comment', '', ('bouldering_why',)),
            ))
        else:
            page = replace(page, title=page.title.replace('Café', 'Cafe'), groups=tuple(
                replace(g, heading=g.heading.split(' (Provisional wording')[0]) for g in page.groups))
        pages.append(page)
    by_id = {q.id: q for q in questions}
    ordered_ids = tuple(qid for page in pages for group in page.groups for qid in group.questions)
    definition = replace(old_d, version=2, questions=tuple(by_id[qid] for qid in ordered_ids))
    presentation = replace(old_p, version=2, pages=tuple(pages))
    config = replace(old_c, version=2)
    modules = tuple(replace(m, questions=tuple(qid for page in pages if page.module == m.id for group in page.groups for qid in group.questions)) for m in old_m)
    presentation.validate(definition, config, modules)
    return definition, presentation, config, modules

SHIFT_ASSETS_V2 = {}
for mask in range(1 << len(SHIFT_MODULE_IDS)):
    assets = build_shift_assets_v2(tuple(mid for i, mid in enumerate(SHIFT_MODULE_IDS) if mask & (1 << i)))
    SHIFT_ASSETS_V2[(assets[0].id, assets[0].version)] = assets


# Final wording is additive: historical definitions and presentation pins stay frozen.
@dataclass(frozen=True)
class CircuitHelpGroup(Group):
    helper_text: str = 'Select up to two.'


def build_shift_assets_v3(enabled):
    old_d, old_p, config, modules = build_shift_assets_v2(enabled)
    wording = {
        'experience_info_access': 'How well the gym communicates important information',
        'shift_bouldering_movement_quality': 'The quality of movement on the boulders',
    }
    definition = replace(old_d, version=3, questions=tuple(
        replace(q, wording=wording[q.id], version=q.version + 1) if q.id in wording else q
        for q in old_d.questions))
    presentation = replace(old_p, version=3, pages=tuple(
        replace(page, groups=tuple(
            CircuitHelpGroup(g.id, '', g.questions) if g.id == 'circuit' else g
            for g in page.groups)) if page.module == 'bouldering' else page
        for page in old_p.pages))
    presentation.validate(definition, config, modules)
    return definition, presentation, config, modules


SHIFT_ASSETS_V3 = {}
for mask in range(1 << len(SHIFT_MODULE_IDS)):
    assets = build_shift_assets_v3(tuple(mid for i, mid in enumerate(SHIFT_MODULE_IDS) if mask & (1 << i)))
    SHIFT_ASSETS_V3[(assets[0].id, assets[0].version)] = assets
