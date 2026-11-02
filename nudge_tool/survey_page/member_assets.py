"""Versioned ABC V4 and Shift meeting member instruments with explicit availability.

Brand names are substituted only in public display copy, never stored semantics.
Each module availability mask has separate definition/presentation/config hashes.
The original four ABC versions and seven legacy availability masks remain readable.
New allocations use Shift v3; prior instruments remain pinned.
"""
from dataclasses import dataclass, replace
import re
from .member_instrument import ABC_MEMBER_ASSETS, ABC_MEMBER_V4, ABC_PRESENTATION_V3, ABC_MODULES_V1, MODULES
from .native_survey import SurveyError

ASSETS = dict(ABC_MEMBER_ASSETS)
MODULE_IDS = tuple(m.id for m in MODULES)
for mask in range(7):
    enabled = tuple(mid for i, mid in enumerate(MODULE_IDS) if mask & (1 << i))
    disabled_questions = {qid for m in MODULES if m.id not in enabled for qid in m.questions}
    questions = tuple(q for q in ABC_MEMBER_V4.questions if q.id not in disabled_questions)
    questions = tuple(replace(q, options=tuple(o for o in q.options if o[0] == 'none' or o[0] in enabled))
                      if q.id == 'modules_used' else q for q in questions)
    definition = replace(ABC_MEMBER_V4, id='sendit_member_modules_' + str(mask), questions=questions)
    config = replace(ABC_MODULES_V1, id='sendit_modules_' + str(mask),
                     automatic=tuple(mid for mid in enabled if mid == 'bouldering'),
                     selectable=tuple(mid for mid in enabled if mid != 'bouldering'))
    pages = tuple(page for page in ABC_PRESENTATION_V3.pages if page.module is None or page.module in enabled)
    pages = tuple(replace(page, finish=True) if i == len(pages)-1 else page for i, page in enumerate(pages))
    presentation = replace(ABC_PRESENTATION_V3, id='sendit_member_pages_' + str(mask), pages=pages)
    modules = tuple(m for m in MODULES if m.id in enabled)
    presentation.validate(definition, config, modules)
    ASSETS[(definition.id, definition.version)] = (definition, presentation, config, modules)

from .shift_member_instrument import SHIFT_ASSETS_V1, SHIFT_ASSETS_V2, SHIFT_ASSETS_V3, SHIFT_MODULE_IDS, SHIFT_DEFAULT_MODULES
ASSETS.update(SHIFT_ASSETS_V1)
ASSETS.update(SHIFT_ASSETS_V2)
ASSETS.update(SHIFT_ASSETS_V3)

@dataclass(frozen=True)
class MemberExperienceConfig:
    gym_name: str = 'Shift Climbing'
    gym_short_name: str = 'Shift'
    modules: tuple | None = None
    accent: str = '#173b34'
    background: str = '#f3f1e8'
    instrument: str = 'shift_v3'

    def __post_init__(self):
        if self.instrument not in {'abc_v4', 'shift_v1', 'shift_v2', 'shift_v3'}:
            raise SurveyError('unknown member instrument')
        available = MODULE_IDS if self.instrument == 'abc_v4' else SHIFT_MODULE_IDS
        if self.modules is None:
            object.__setattr__(self, 'modules', MODULE_IDS if self.instrument == 'abc_v4' else SHIFT_DEFAULT_MODULES)
        if any(not isinstance(v, str) or not v.strip() or len(v) > 120 for v in (self.gym_name, self.gym_short_name)):
            raise SurveyError('invalid gym display identity')
        if not isinstance(self.modules, tuple) or len(set(self.modules)) != len(self.modules) or not set(self.modules) <= set(available):
            raise SurveyError('module instrument unavailable')
        if any(not re.fullmatch(r'#[a-fA-F0-9]{6}', c) for c in (self.accent, self.background)):
            raise SurveyError('invalid brand color')

    @property
    def assets(self):
        if self.instrument in {'shift_v1', 'shift_v2', 'shift_v3'}:
            mask = sum(1 << i for i, mid in enumerate(SHIFT_MODULE_IDS) if mid in self.modules)
            return ASSETS[('shift_member_modules_' + str(mask), int(self.instrument.removeprefix('shift_v')))]
        mask = sum(1 << i for i, mid in enumerate(MODULE_IDS) if mid in self.modules)
        return ASSETS[('abc_member_v1' if mask == 7 else 'sendit_member_modules_' + str(mask), 4)]

    def wording(self, wording):
        return wording.replace('Appalachian Bouldering Center', self.gym_name).replace('at ABC ', 'at ' + self.gym_short_name + ' ')


def pins(assets):
    definition, presentation, config, _ = assets
    return {kind + '_' + attr: getattr(asset, 'fingerprint' if attr == 'hash' else attr)
            for kind, asset in (('definition', definition), ('presentation', presentation), ('module_config', config))
            for attr in ('id', 'version', 'hash')}
