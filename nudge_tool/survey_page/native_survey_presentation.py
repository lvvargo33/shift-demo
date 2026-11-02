"""Controlled, versioned instrument presentation; no UI or delivery wiring."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from .native_survey import Question, SurveyError, applicability, digest, identifier

@dataclass(frozen=True)
class ExclusiveQuestion(Question):
    exclusive_options: tuple[str, ...] = ()

    def __post_init__(self):
        if (self.kind != 'multi' or not self.exclusive_options or
            len(set(self.exclusive_options)) != len(self.exclusive_options) or
            not set(self.exclusive_options) <= {o[0] for o in self.options}):
            raise SurveyError('invalid exclusive options')


def select_option(question, current, selected):
    """Selection gesture contract for future renderer; writes still validate server-side."""
    if selected not in {o[0] for o in question.options}:
        raise SurveyError('unknown option')
    exclusive = set(question.exclusive_options)
    if selected in current:
        return [v for v in current if v != selected]
    return [selected] if selected in exclusive else [v for v in current if v not in exclusive] + [selected]

@dataclass(frozen=True)
class Module:
    id: str
    questions: tuple[str, ...]

@dataclass(frozen=True)
class ModuleConfig:
    id: str
    version: int
    automatic: tuple[str, ...]
    selectable: tuple[str, ...]

    def __post_init__(self):
        identifier(self.id)
        ids = self.automatic + self.selectable
        for module_id in ids:
            identifier(module_id)
        if type(self.version) is not int or self.version < 1 or len(ids) != len(set(ids)):
            raise SurveyError('invalid module configuration')

    @property
    def fingerprint(self):
        return digest(asdict(self))

@dataclass(frozen=True)
class Group:
    id: str
    heading: str
    questions: tuple[str, ...]

@dataclass(frozen=True)
class Page:
    id: str
    title: str
    groups: tuple[Group, ...]
    module: str | None = None
    finish: bool = False

@dataclass(frozen=True)
class Presentation:
    id: str
    version: int
    pages: tuple[Page, ...]

    @property
    def fingerprint(self):
        return digest(asdict(self))

    def validate(self, definition, config, modules):
        identifier(self.id)
        if type(self.version) is not int or self.version < 1 or not self.pages:
            raise SurveyError('invalid presentation version')
        questions = {q.id: q for q in definition.questions}
        registry = {m.id: m for m in modules}
        if len(registry) != len(modules):
            raise SurveyError('duplicate module')
        module_questions = []
        for m in modules:
            identifier(m.id)
            if not m.questions or not set(m.questions) <= questions.keys():
                raise SurveyError('invalid module questions')
            module_questions.extend(m.questions)
        if len(module_questions) != len(set(module_questions)):
            raise SurveyError('overlapping module questions')
        active = config.automatic + config.selectable
        if not set(active) <= registry.keys():
            raise SurveyError('module instrument unavailable')
        selectors = {questions[qid].visible_if.question
                     for mid in config.selectable for qid in registry[mid].questions
                     if questions[qid].visible_if is not None}
        if config.selectable and len(selectors) != 1:
            raise SurveyError('one module selector required')
        selector_id = next(iter(selectors), None)
        selector = questions.get(selector_id)
        if config.selectable and (selector is None or selector.kind != 'multi' or
                not set(config.selectable) <= {o[0] for o in selector.options}):
            raise SurveyError('invalid module selector')
        for mid in active:
            for qid in registry[mid].questions:
                c = questions[qid].visible_if
                # An automatic page may ask usage first, then condition its own items.
                # Definition validation requires that parent to precede the child.
                if mid in config.automatic and c is not None and c.question not in registry[mid].questions:
                    raise SurveyError('automatic module condition must stay within module')
                if mid in config.selectable and (c is None or (c.question, c.operator, c.value) != (selector_id, 'contains', mid)):
                    raise SurveyError('invalid module condition')
        page_ids, group_ids, ordered, page_modules = [], [], [], []
        for p in self.pages:
            identifier(p.id)
            page_ids.append(p.id)
            if not p.groups:
                raise SurveyError('empty page')
            page_questions = []
            for g in p.groups:
                identifier(g.id)
                group_ids.append(g.id)
                if not g.questions:
                    raise SurveyError('empty group')
                page_questions.extend(g.questions)
            if p.module:
                if p.module not in active or tuple(page_questions) != registry[p.module].questions:
                    raise SurveyError('invalid module page')
                page_modules.append(p.module)
            ordered.extend(page_questions)
        finishes = [p.finish for p in self.pages]
        first_finish = finishes.index(True) if any(finishes) else len(finishes)
        if (len(page_ids) != len(set(page_ids)) or len(group_ids) != len(set(group_ids)) or
            ordered != [q.id for q in definition.questions] or
            len(page_modules) != len(set(page_modules)) or set(page_modules) != set(active) or
            not any(finishes) or not all(finishes[first_finish:]) or
            any(not p.module for p in self.pages[first_finish + 1:])):
            raise SurveyError('ambiguous presentation order or finish')
        return self

    def applicable_pages(self, definition, config, modules, answers):
        self.validate(definition, config, modules)
        from .native_survey import validate_answer
        for q in definition.questions:
            if q.id in answers:
                validate_answer(q, {k: v for k, v in answers[q.id].items() if k in {'status', 'value'}})
        visible = applicability(definition, answers)
        return tuple(p for p in self.pages if any(visible[q] for g in p.groups for q in g.questions))
