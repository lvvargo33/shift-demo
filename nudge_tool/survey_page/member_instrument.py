"""ABC member survey, dark reviewed assets. Never imported by live member callers."""
from dataclasses import replace

from .native_survey import Condition, Definition, Question
from .native_survey_presentation import ExclusiveQuestion, Module, ModuleConfig, Group, Page, Presentation


def rating(id, wording, module=None, options=()):
    return Question(id, wording, 'rating', required=False, options=options,
                    visible_if=Condition('modules_used', 'contains', module) if module else None)


def text(id, wording, module=None):
    return Question(id, wording, 'text', required=False,
                    visible_if=Condition('modules_used', 'contains', module) if module else None)


# Retained verbatim so already-pinned occurrences remain readable and verifiable.
ABC_MEMBER_V1 = Definition('abc_member_v1', 1, 'member', (
    rating('core_nps', 'How likely are you to recommend Appalachian Bouldering Center to a friend or family member?', options=(('0', 'Not at all likely'), ('10', 'Extremely likely'))),
    text('core_nps_why', 'Why did you give that score?'),
    rating('core_remain_6m', 'How likely are you to still be a member 6 months from now?'),
    rating('core_staff_friendliness', 'How friendly is our staff overall?'),
    rating('core_community', 'How satisfied are you with the sense of community at our gym?'),
    rating('core_route_setting', 'How satisfied are you with the route setting?'),
    rating('core_cleanliness', 'How satisfied are you with the cleanliness of the gym?'),
    rating('core_value', 'How satisfied are you with the value you get for what you pay?'),
    rating('experience_staff_help', 'Getting help from staff when I need it'),
    rating('experience_info_access', 'Ease of finding information when I need it'),
    rating('experience_new_climber_welcome', 'How welcome new climbers feel in our gym'),
    rating('experience_safety', 'Safety in the climbing areas'),
    rating('experience_access_climbs', 'Access to the climbs I want to do'),
    text('experience_improve', 'Is there anything we could do to improve your experience in these areas?'),
    ExclusiveQuestion('modules_used', 'Which of these have you used at ABC in the past 90 days?', 'multi', required=False,
        options=(('training_area', 'Training Area'), ('youth_programs', 'Youth Programs / Camps, as a parent or guardian'), ('none', 'None of these')), exclusive_options=('none',)),
    Question('bouldering_grade', 'What grade do you climb most often?', 'single', required=False,
        options=tuple(('V9_plus' if v == 'V9+' else v, v) for v in ('V0', 'V1', 'V2', 'V3', 'V4', 'V5', 'V6', 'V7', 'V8', 'V9+'))),
    rating('bouldering_quantity', 'The number of climbs available at your level'),
    rating('bouldering_variety', 'The variety of climbs available at your level'),
    rating('bouldering_grading_consistency', 'How consistent the grading feels'),
    rating('bouldering_reset_frequency', 'How often climbs are reset'),
    text('bouldering_why', 'Please tell us why.'),
    rating('training_overall', 'The overall training area', 'training_area'),
    rating('training_equipment_condition', 'The condition and maintenance of the training equipment', 'training_area'),
    text('training_why', 'Please tell us why.', 'training_area'),
    rating('youth_overall', 'The overall youth program experience', 'youth_programs'),
    rating('youth_staff_coaching', 'The quality of the youth program staff/coaching', 'youth_programs'),
    text('youth_why', 'Please tell us why.', 'youth_programs'),
    text('staff_recognition', "Is there a staff member you'd like to recognize?"),
))

ABC_MEMBER_V2 = replace(ABC_MEMBER_V1, version=2, questions=(
    *ABC_MEMBER_V1.questions[:12],
    Question('experience_access_climbs',
        'How satisfied are you with your ability to climb what you want when the gym is busy?',
        'rating', required=False, version=2,
        options=(('0', 'Very dissatisfied'), ('10', 'Very satisfied'))),
    *ABC_MEMBER_V1.questions[13:15],
    ABC_MEMBER_V1.questions[-1],
    *ABC_MEMBER_V1.questions[15:-1],
))

ABC_MEMBER_V3 = replace(ABC_MEMBER_V2, version=3, questions=(
    *ABC_MEMBER_V2.questions[:12],
    replace(ABC_MEMBER_V2.questions[12], version=3,
            wording='Ability to climb what you want when the gym is busy'),
    *ABC_MEMBER_V2.questions[13:],
))

SATISFACTION_ENDPOINTS = (('0', 'Very dissatisfied'), ('10', 'Very satisfied'))
REMAIN_ENDPOINTS = (('0', 'Very unlikely'), ('10', 'Very likely'))


def _member_v4_question(question):
    if question.id == 'experience_new_climber_welcome':
        return None
    wording = {
        'experience_info_access': 'Communication about things I need to know',
        'experience_safety': 'Safety in the gym',
        'experience_access_climbs': 'Crowding during the times I usually climb',
    }.get(question.id, question.wording)
    options = question.options
    if question.kind == 'rating':
        if question.id == 'core_remain_6m':
            options = REMAIN_ENDPOINTS
        elif question.id != 'core_nps':
            options = SATISFACTION_ENDPOINTS
    changed = wording != question.wording or options != question.options
    return replace(question, wording=wording, options=options,
                   version=question.version + 1 if changed else question.version)


# V1-V3 remain immutable in ABC_MEMBER_ASSETS for pinned historical occurrences.
ABC_MEMBER_V4 = replace(ABC_MEMBER_V3, version=4, questions=tuple(
    revised for question in ABC_MEMBER_V3.questions
    if (revised := _member_v4_question(question)) is not None
))

MODULES = (
    Module('bouldering', tuple(q.id for q in ABC_MEMBER_V4.questions if q.id.startswith('bouldering_'))),
    Module('training_area', tuple(q.id for q in ABC_MEMBER_V4.questions if q.id.startswith('training_'))),
    Module('youth_programs', tuple(q.id for q in ABC_MEMBER_V4.questions if q.id.startswith('youth_'))),
)
ABC_MODULES_V1 = ModuleConfig('abc_modules', 1, ('bouldering',), ('training_area', 'youth_programs'))
SATISFACTION = 'How satisfied are you with the following?'

ABC_PRESENTATION_V1 = Presentation('abc_member_pages', 1, (
    Page('core', 'Core Metrics', (Group('core_metrics', '', tuple(q.id for q in ABC_MEMBER_V1.questions[:8])),)),
    Page('experience', 'Member Experience', (
        Group('experience_ratings', SATISFACTION, tuple(q.id for q in ABC_MEMBER_V1.questions[8:13])),
        Group('experience_comment', '', ('experience_improve',)),
        Group('module_selection', '', ('modules_used',)))),
    Page('bouldering', 'Bouldering', (
        Group('grade', '', ('bouldering_grade',)),
        Group('bouldering_ratings', SATISFACTION, MODULES[0].questions[1:5]),
        Group('bouldering_comment', '', ('bouldering_why',))), module='bouldering'),
    Page('training', 'Training Area', (Group('training_questions', SATISFACTION, MODULES[1].questions),), module='training_area'),
    Page('youth', 'Youth Programs / Camps', (Group('youth_questions', SATISFACTION, MODULES[2].questions),), module='youth_programs'),
    Page('final', 'Staff recognition', (Group('recognition', '', ('staff_recognition',)),), finish=True),
)).validate(ABC_MEMBER_V1, ABC_MODULES_V1, MODULES)

ABC_PRESENTATION_V2 = Presentation('abc_member_pages', 2, (
    Page('core', 'Core Metrics', (Group('core_metrics', '', (
        'core_nps', 'core_nps_why', 'core_remain_6m', 'core_staff_friendliness',
        'core_community', 'core_route_setting', 'core_cleanliness', 'core_value')),)),
    Page('experience', 'Member Experience', (
        Group('experience_ratings', SATISFACTION, (
            'experience_staff_help', 'experience_info_access',
            'experience_new_climber_welcome', 'experience_safety',
            'experience_access_climbs')),
        Group('experience_comment', '', ('experience_improve',)),
        Group('module_selection', '', ('modules_used',)),
        Group('recognition', '', ('staff_recognition',)))),
    Page('bouldering', 'Bouldering', (
        Group('grade', '', ('bouldering_grade',)),
        Group('bouldering_ratings', SATISFACTION, MODULES[0].questions[1:5]),
        Group('bouldering_comment', '', ('bouldering_why',))), module='bouldering', finish=True),
    Page('training', 'Training Area', (Group('training_questions', SATISFACTION, MODULES[1].questions),), module='training_area', finish=True),
    Page('youth', 'Youth Programs / Camps', (Group('youth_questions', SATISFACTION, MODULES[2].questions),), module='youth_programs', finish=True),
)).validate(ABC_MEMBER_V2, ABC_MODULES_V1, MODULES)

ABC_PRESENTATION_V2.validate(ABC_MEMBER_V3, ABC_MODULES_V1, MODULES)

ABC_PRESENTATION_V3 = Presentation('abc_member_pages', 3, (
    Page('core', 'Core Metrics', (Group('core_metrics', '', (
        'core_nps', 'core_nps_why', 'core_remain_6m', 'core_staff_friendliness',
        'core_community', 'core_route_setting', 'core_cleanliness', 'core_value')),)),
    Page('experience', 'Member Experience', (
        Group('experience_ratings', SATISFACTION, (
            'experience_staff_help', 'experience_info_access',
            'experience_safety', 'experience_access_climbs')),
        Group('experience_comment', '', ('experience_improve',)),
        Group('module_selection', '', ('modules_used',)),
        Group('recognition', '', ('staff_recognition',)))),
    Page('bouldering', 'Bouldering', (
        Group('grade', '', ('bouldering_grade',)),
        Group('bouldering_ratings', SATISFACTION, MODULES[0].questions[1:5]),
        Group('bouldering_comment', '', ('bouldering_why',))), module='bouldering', finish=True),
    Page('training', 'Training Area', (Group('training_questions', SATISFACTION, MODULES[1].questions),), module='training_area', finish=True),
    Page('youth', 'Youth Programs / Camps', (Group('youth_questions', SATISFACTION, MODULES[2].questions),), module='youth_programs', finish=True),
)).validate(ABC_MEMBER_V4, ABC_MODULES_V1, MODULES)

ABC_MEMBER_ASSETS = {
    (ABC_MEMBER_V1.id, ABC_MEMBER_V1.version):
        (ABC_MEMBER_V1, ABC_PRESENTATION_V1, ABC_MODULES_V1, MODULES),
    (ABC_MEMBER_V2.id, ABC_MEMBER_V2.version):
        (ABC_MEMBER_V2, ABC_PRESENTATION_V2, ABC_MODULES_V1, MODULES),
    (ABC_MEMBER_V3.id, ABC_MEMBER_V3.version):
        (ABC_MEMBER_V3, ABC_PRESENTATION_V2, ABC_MODULES_V1, MODULES),
    (ABC_MEMBER_V4.id, ABC_MEMBER_V4.version):
        (ABC_MEMBER_V4, ABC_PRESENTATION_V3, ABC_MODULES_V1, MODULES),
}
