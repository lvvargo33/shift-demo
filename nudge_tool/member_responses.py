"""Read membership-survey responses out of the Google Sheet.

Build step 6 (Membership Surveys SPEC). Reads only. Never sends, never writes to
the sheet.

SurveyMonkey auto-exports every response into a Google Sheet; this module parses
that sheet, scores each response, joins it back to a member by `mid`, and picks
the response template. Sending is elsewhere.

WHY THE PARSING LOOKS THE WAY IT DOES
Four properties of that export drove every decision here, all confirmed against
real rows on 2026-08-21:

1. **Matrix answers arrive PACKED INTO ONE CELL**, pipe-delimited:
   `Row label: value | Row label: value | ...`. Unanswered = `-`.

2. **Q4, Q5 and Q6 have IDENTICAL heading text** ("How satisfied are you with
   each of these?..."). So a heading, and a column index, cannot tell them
   apart. This module keys on the unique ROW LABELS *inside* the packed cells
   instead, via one flat label -> metric dict. That makes it immune to column
   reordering, added columns, and the duplicate headings.

3. **Email / First Name / Last Name are ALWAYS BLANK.** They populate only on
   SurveyMonkey's Email Invitation collector and we use a web link. `mid` is the
   sole identifier, which is why survey_link refuses to build a link without it.

4. **The export is APPEND-ONLY.** Deleting a response in SurveyMonkey does NOT
   remove its row here, and the collector deliberately allows repeat submissions
   (cookie blocking was refusing household members who share a computer, and ABC
   sells family plans up to 6). So de-duplication in THIS module is the only
   duplicate guard in the whole system. Latest row per `mid` AND ROUND wins.

ONE RESPONSE PER SURVEY, NOT PER MEMBER (Block 20 R1a, 2026-10-08)
Until 2026-10-08 this module kept only the latest row per `mid`, so the moment
a member answered round 2 their round-1 answer vanished from the site (ROADMAP
Block 20 decision 18, found 10/7). Now every survey a member answered is kept:
a response is keyed by `rid` (SurveyMonkey's Respondent ID), and a "round" is
the Nth survey invite that reached the member before they answered (the same
rule the page calls a wave). Dedupe happens only INSIDE a round (one person
submitting the same survey twice keeps the latest), never across rounds.
`parse_rows` needs the send history to know the rounds; `read` supplies it
from the outreach log. Without it (sends=None) NOTHING is dropped: losing a
real answer is the worse mistake.

The free-text and 0-10 questions are matched on a distinctive PHRASE rather than
on their `Qn.` prefix, so renumbering the survey cannot silently repoint a
metric. A phrase that matches zero or several columns raises instead of quietly
scoring nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import ClientConfig

# ---------------------------------------------------------------------------
# The authoritative label -> metric mapping. Keys are lowercased row labels
# exactly as SurveyMonkey writes them inside the packed matrix cells.
# ---------------------------------------------------------------------------

# Q4: the five SELECTION metrics (drive which template goes out).
# Q5/Q6: diagnostic only. Per the 2026-08-20 decision these feed the staff-only
# internal guidance on the response page and NEVER the outgoing email.
ROW_LABEL_TO_METRIC = {
    # --- Q4, selection ---
    "how friendly our staff are": "staff",
    "the sense of community at the gym": "community",
    "the route setting": "routes",
    "the cleanliness of the gym": "cleanliness",
    "the value you get for what you pay": "value",
    # --- Q5, diagnostic ---
    "the range of climbs available at your level": "grade_range",
    "how often the climbs get reset": "reset_frequency",
    "being able to get on the climbs you want without waiting": "crowding",
    "the training and fitness area": "training_area",
    # --- Q6, diagnostic ---
    "getting help from staff when i need it": "staff_help",
    "finding out what is happening at the gym (hours, closures, events, resets)":
        "info_access",
    "feeling safe in the climbing areas": "safety",
}

DIAGNOSTIC_METRICS = ("grade_range", "reset_frequency", "crowding",
                      "training_area", "staff_help", "info_access", "safety")

# Template selection order. `remain` comes from Q3, the rest from Q4.
# `nps` is deliberately ABSENT: it is an urgency indicator on the response page,
# never a selector. That rule came from Chris's Lovable build and is preserved.
SELECTION_METRICS = ("remain", "staff", "community", "routes", "cleanliness",
                     "value")

# Values are template IDs, which are the template FILENAME STEM and therefore
# carry the _v1 suffix like every other template in this client. Dropping the
# suffix here would name files that do not exist and fail every send closed.
METRIC_TEMPLATE = {
    "remain": "member_low_remain_v1",
    "staff": "member_low_staff_v1",
    "community": "member_low_community_v1",
    "routes": "member_low_routes_v1",
    "cleanliness": "member_low_cleanliness_v1",
    "value": "member_low_value_v1",
}
POSITIVE_TEMPLATE = "member_positive_v1"

# (field, distinctive phrase, kind). Phrase is matched case-insensitively
# against the column heading and must hit EXACTLY ONE column.
_SCALAR_FIELDS = (
    ("nps", "recommend", "int"),
    ("remain", "still be a member", "int"),
    ("open_comment", "why did you give that score", "text"),
    ("improve_comment", "anything we can do to improve", "text"),
    ("staff_shout_out", "staff member you would like to recognize", "text"),
)

MID_HEADING = "mid"
_BLANK = {"", "-", "n/a", "none"}

# SurveyMonkey's fixed metadata columns. Skipped before the packed-matrix scan.
# This is NOT the heading-keying the module docstring warns against: those are
# the export's own stable columns, not the survey questions, and the questions
# are still identified purely by their row labels.
_METADATA_HEADINGS = {
    "respondent id", "collector id", "collector name", "language",
    "start date", "end date", "time spent", "email address",
    "first name", "last name",
}

# CHRIS'S RULE, ported verbatim from the Aug 20 Lovable export
# (src/lib/response-templates.ts, POSITIVE_THRESHOLD = 9). Luke chose it over
# our earlier "lowest <= 7" rule on 2026-08-21 ("use Chris's rule and what he
# has for everything"). The positive template goes out only when EVERY
# selection metric is at or above this; anything else picks the lowest metric's
# follow-up draft, so a row of 8s is a follow-up, not a thank-you. Still
# configurable via member_survey.positive_threshold.
DEFAULT_POSITIVE_THRESHOLD = 9

# Display labels, Chris's METRIC_LABEL plus our `value`.
METRIC_LABEL = {
    "remain": "Likelihood to Remain",
    "staff": "Staff Friendliness",
    "community": "Sense of Community",
    "routes": "Route Setting",
    "cleanliness": "Cleanliness",
    "value": "Value for Money",
}

# Staff-only coaching notes per template (Chris's `internalGuidance`). These are
# shown on the response page next to the draft and must NEVER be rendered into
# an outgoing email, which is why they live here and not in the .md templates
# (everything in a template file is body copy). `member_low_value` is ours;
# Chris's set has no value metric.
INTERNAL_GUIDANCE = {
    "member_positive_v1": [
        "Thank the member.",
        "Ask for a Google review.",
        "Share especially positive comments with the team.",
    ],
    "member_low_remain_v1": [
        "Try to understand what would improve their experience.",
        "Escalate recurring cancellation risks.",
    ],
    "member_low_staff_v1": [
        "Ask for additional details if they are comfortable sharing.",
        "Escalate serious concerns to management.",
    ],
    "member_low_community_v1": [
        "Invite the member to a community event if appropriate.",
        "Ask what would make the gym feel more welcoming.",
    ],
    "member_low_routes_v1": [
        "Ask if there are specific grades or styles they'd like to see more often.",
        "Mention upcoming resets if known.",
        "Share recurring feedback with the head setter.",
    ],
    "member_low_cleanliness_v1": [
        "Ask where or when they noticed the issue.",
        "Share recurring concerns with the operations team.",
    ],
    "member_low_value_v1": [
        "Ask how often they climb and whether a different plan would fit better.",
        "Mention member perks they may not be using.",
        "Escalate recurring value complaints as a pricing signal.",
    ],
}


@dataclass(frozen=True)
class Recommendation:
    """Chris's `Recommendation` shape: which template, why, and the ranking
    that drove it, so the response page can show its work."""
    template_key: str
    metric: str | None            # None on the positive path
    reason: str
    ranked: tuple                 # ((metric, score), ...) lowest first


class ResponseParseError(RuntimeError):
    """The sheet is not the shape this parser expects. Raised rather than
    guessed at: a mis-identified column would silently mis-score every member."""


@dataclass
class MemberResponse:
    mid: str
    respondent_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    nps: int | None = None            # urgency only, never a selector
    remain: int | None = None
    open_comment: str = ""
    improve_comment: str = ""
    staff_shout_out: str = ""
    metrics: dict = field(default_factory=dict)   # metric key -> 0..10
    round_no: int = 1                 # the Nth survey invite this answers (see module docstring)
    # Anything the instrument asks beyond ABC's six questions + diagnostics,
    # shown on the response page as-is (SHIFT's native member survey, A2
    # 2026-11-02: section ratings + comments, circuits, member context).
    # Always empty for ABC's SurveyMonkey rows.
    extra: dict = field(default_factory=dict)

    @property
    def answered_at(self) -> str:
        return (self.ended_at or self.started_at or "")[:10]

    @property
    def rid(self) -> str:
        """The per-SURVEY id (decision 18): SurveyMonkey's Respondent ID, which
        is unique per submission. A row without one (never seen in the live
        export, but the parser must not crash) falls back to mid + timestamp."""
        return self.respondent_id or f"{self.mid}|{self.ended_at or self.started_at}"

    @property
    def selection_scores(self) -> dict:
        """The metrics that can pick a template, those actually answered."""
        out = {}
        if self.remain is not None:
            out["remain"] = self.remain
        for k in SELECTION_METRICS:
            if k == "remain":
                continue
            if k in self.metrics:
                out[k] = self.metrics[k]
        return out

    @property
    def scoreable(self) -> bool:
        return bool(self.selection_scores)

    @property
    def has_shout_out(self) -> bool:
        return bool(self.staff_shout_out.strip())

    def ranked(self) -> tuple:
        """Selection metrics sorted lowest first, ties by SELECTION_METRICS
        order (Chris's `ranked`). `remain` first is deliberate: if someone is
        at risk of leaving AND unhappy about routes, the leaving signal is the
        one to answer."""
        scores = self.selection_scores
        order = {k: i for i, k in enumerate(SELECTION_METRICS)}
        return tuple(sorted(scores.items(), key=lambda kv: (kv[1], order[kv[0]])))

    def lowest_metric(self) -> tuple[str, int] | None:
        """The lowest-scoring selection metric, or None if nothing is scoreable."""
        r = self.ranked()
        return r[0] if r else None

    def recommend(self, positive_threshold: int = DEFAULT_POSITIVE_THRESHOLD
                  ) -> Recommendation | None:
        """Chris's `recommendTemplate`, verbatim in behaviour.

        Deterministic business logic, not AI. If EVERY answered selection
        metric is at or above `positive_threshold`, the positive template.
        Otherwise the lowest metric's template. None when nothing was
        scoreable."""
        ranked = self.ranked()
        if not ranked:
            return None
        if all(score >= positive_threshold for _, score in ranked):
            return Recommendation(
                template_key=POSITIVE_TEMPLATE, metric=None,
                reason=(f"Recommended because all satisfaction scores were "
                        f"{positive_threshold} or higher."),
                ranked=ranked)
        worst_key, _ = ranked[0]
        return Recommendation(
            template_key=METRIC_TEMPLATE[worst_key], metric=worst_key,
            reason=(f"Recommended because {METRIC_LABEL[worst_key]} received "
                    f"the lowest score."),
            ranked=ranked)

    def template_key(self, positive_threshold: int = DEFAULT_POSITIVE_THRESHOLD
                     ) -> str | None:
        """Which response template this member should get (see `recommend`)."""
        rec = self.recommend(positive_threshold)
        return rec.template_key if rec else None

    def is_detractor(self, positive_threshold: int = DEFAULT_POSITIVE_THRESHOLD
                     ) -> bool:
        """Anyone who does NOT earn the positive template. Under Chris's rule
        that is any member with at least one selection metric below the
        threshold, which is who the follow-up queue is for."""
        rec = self.recommend(positive_threshold)
        return rec is not None and rec.metric is not None


@dataclass
class ReadResult:
    responses: list = field(default_factory=list)   # one per (mid, round): latest within a round
    total_rows: int = 0
    duplicates_dropped: int = 0                     # same mid, same round, older row
    unmatched: list = field(default_factory=list)   # mids with no member
    blank_mid: int = 0
    unknown_row_labels: set = field(default_factory=set)

    @property
    def count(self) -> int:
        return len(self.responses)

    def latest_by_mid(self) -> dict:
        """mid -> the member's newest response, for callers that want one row
        per member (the old shape of `responses`)."""
        out: dict = {}
        for r in sorted(self.responses, key=lambda m: (m.ended_at or m.started_at or "",
                                                       m.respondent_id)):
            out[r.mid] = r
        return out


def _clean(v) -> str:
    s = str(v if v is not None else "").strip()
    return "" if s.lower() in _BLANK else s


def _as_int(v):
    s = _clean(v)
    if not s:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def parse_packed_matrix(cell: str) -> tuple[dict, set]:
    """`"Label: 9 | Other label: 7"` -> ({metric: 9, ...}, unknown_labels).

    Splits the VALUE off the right (rsplit) so a label containing a colon would
    still parse. Labels containing commas and parentheses already occur, e.g.
    "Finding out what is happening at the gym (hours, closures, events, resets)",
    which is why the label is never split on punctuation."""
    found, unknown = {}, set()
    text = _clean(cell)
    if not text:
        return found, unknown
    for part in text.split("|"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        label, _, value = part.rpartition(":")
        label = label.strip().lower()
        score = _as_int(value)
        if score is None:
            continue
        if not _looks_like_row_label(label):
            # A timestamp cell ("2026-08-21T02:34:47") or a duration ("01:06")
            # also contains a colon followed by digits. Without this guard they
            # parse as labels and flood `unknown_row_labels`, which is meant to
            # be the alarm for a genuinely NEW survey row. Noise there would
            # mask the one signal it exists to give.
            continue
        metric = ROW_LABEL_TO_METRIC.get(label)
        if metric:
            found[metric] = score
        else:
            unknown.add(label)
    return found, unknown


def _looks_like_row_label(label: str) -> bool:
    """A real matrix row label is prose: it has a space and letters. Every one
    of the 12 live labels qualifies; timestamps and durations do not."""
    return " " in label and any(c.isalpha() for c in label)


def _resolve_columns(headings) -> dict:
    """field -> heading, matched on a distinctive phrase. Raises if a phrase
    matches zero or several columns."""
    out = {}
    lowered = [(h, str(h or "").lower()) for h in headings]
    for fieldname, phrase, _kind in _SCALAR_FIELDS:
        hits = [h for h, low in lowered if phrase in low]
        if len(hits) != 1:
            raise ResponseParseError(
                f"expected exactly 1 column matching {phrase!r} for "
                f"'{fieldname}', found {len(hits)}: {hits}. The survey wording "
                f"probably changed; update _SCALAR_FIELDS.")
        out[fieldname] = hits[0]
    mid = [h for h, low in lowered if low == MID_HEADING]
    if len(mid) != 1:
        raise ResponseParseError(
            f"expected exactly 1 '{MID_HEADING}' column, found {len(mid)}: "
            f"{mid}. Without it no response can be matched to a member.")
    out["mid"] = mid[0]
    return out


def survey_sends(outreach_rows) -> dict:
    """Outreach rows -> {key: [sent_date, ...] sorted} for the member-survey
    trigger, live rows only. Keyed by climber_id when present, else email, so
    a response (which carries mid = climber_id) can find its sends. Lives here
    (not member_views) because the parser needs it to tell rounds apart."""
    from . import member_survey
    out: dict = {}
    for r in outreach_rows or []:
        if (r.get("trigger_name") or "").strip() != member_survey.TRIGGER_NAME:
            continue
        if (r.get("mode") or "").strip() == "test":
            continue
        d = (r.get("sent_date") or "").strip()[:10]
        if not d:
            continue
        cid = (r.get("climber_id") or "").strip()
        em = (r.get("email") or "").strip().lower()
        for key in {k for k in (cid, em) if k}:
            out.setdefault(key, []).append(d)
    for k in out:
        out[k].sort()
    return out


def round_of(sends: list, answered_at: str) -> int:
    """Round N = the Nth survey send to this person on or before the answer
    date. No send on record (a test row, or a log that has not synced) = 1."""
    day = (answered_at or "")[:10]
    n = sum(1 for d in sends or [] if not day or d <= day)
    return max(1, n)


def parse_rows(rows, sends=None) -> ReadResult:
    """Sheet rows (header -> value dicts) -> MemberResponses, one per survey.

    `sends` = survey_sends(outreach rows): the per-member send dates that tell
    rounds apart. With it, the latest row per (mid, round) wins and the older
    same-round rows count as `duplicates_dropped`. Without it (None) every row
    is kept and every response reads as round 1: the parser cannot tell a
    repeat submission from a later round, and dropping a real answer is the
    worse mistake (decision 18)."""
    res = ReadResult()
    rows = list(rows)
    if not rows:
        return res
    res.total_rows = len(rows)

    cols = _resolve_columns(rows[0].keys())
    scalar_headings = set(cols.values())

    parsed: list = []
    for r in rows:
        mid = _clean(r.get(cols["mid"]))
        if not mid:
            # Someone reached the public link without ?mid=. Real feedback, but
            # unattributable, so it is counted and skipped rather than guessed.
            res.blank_mid += 1
            continue

        mr = MemberResponse(
            mid=mid,
            respondent_id=_clean(r.get("Respondent ID")),
            started_at=_clean(r.get("Start Date")),
            ended_at=_clean(r.get("End Date")),
            nps=_as_int(r.get(cols["nps"])),
            remain=_as_int(r.get(cols["remain"])),
            open_comment=_clean(r.get(cols["open_comment"])),
            improve_comment=_clean(r.get(cols["improve_comment"])),
            staff_shout_out=_clean(r.get(cols["staff_shout_out"])),
        )
        # Every OTHER cell is a candidate packed matrix. Scanning all of them,
        # rather than three known headings, is what makes this immune to the
        # duplicate Q4/Q5/Q6 headings and to column reordering.
        for heading, cell in r.items():
            if heading in scalar_headings or heading == cols["mid"]:
                continue
            if str(heading or "").strip().lower() in _METADATA_HEADINGS:
                continue
            found, unknown = parse_packed_matrix(cell)
            mr.metrics.update(found)
            res.unknown_row_labels |= unknown
        parsed.append(mr)

    # Latest row per (mid, round) wins. Sort ascending and let later rows
    # overwrite, so the survivor is deterministic. Respondent ID breaks a
    # same-timestamp tie (SurveyMonkey issues it monotonically). A round-1
    # answer is NEVER overwritten by a round-2 answer (decision 18).
    parsed.sort(key=lambda m: (m.ended_at or m.started_at or "",
                               m.respondent_id))
    latest: dict = {}
    for mr in parsed:
        if sends is None:
            key = (mr.mid, mr.rid)          # no send history: keep everything
        else:
            mr.round_no = round_of(sends.get(mr.mid) or [], mr.answered_at)
            key = (mr.mid, mr.round_no)
        if key in latest:
            res.duplicates_dropped += 1
        latest[key] = mr

    res.responses = list(latest.values())
    return res


def read(client: ClientConfig, outreach_rows=None) -> ReadResult:
    """Read + parse the live response sheet for this client.

    Reuses survey.py's reader so the membership survey inherits the same
    service-account auth and the same transient-5xx retry/backoff that a SHIFT
    incident (S27) proved necessary: a single Google blip there aborted a whole
    live cron run.

    The outreach log (local CSV, loaded here when not passed in) supplies the
    survey send dates that tell rounds apart; see parse_rows."""
    from . import survey as _survey

    ms = client.member_survey or {}
    if str(ms.get("source", "") or "").strip() == "native":
        # SHIFT (A2, 2026-11-02): answers live in Firestore member sessions,
        # not a SurveyMonkey sheet. ABC has no `source` key.
        from . import member_responses_native
        return member_responses_native.read(client, outreach_rows=outreach_rows)
    sheet_id = str(ms.get("sheet_id", "") or "").strip()
    rng = str(ms.get("sheet_range", "") or "").strip()
    if not sheet_id or not rng:
        return ReadResult()

    if outreach_rows is None:
        from . import outreach
        try:
            outreach_rows = outreach.load_rows(client)
        except Exception:           # noqa: BLE001 - a missing log must not hide the answers
            outreach_rows = []

    shim = ClientConfig.__new__(ClientConfig)
    shim.__dict__.update(client.__dict__)
    shim.survey = {"source": "gsheet", "gsheet_id": sheet_id, "gsheet_range": rng}
    return parse_rows(_survey._read_rows(shim), sends=survey_sends(outreach_rows))


def join_members(result: ReadResult, members) -> list:
    """Pair responses with members on mid == member_id.

    Returns [(MemberResponse, Member|None)]. An unmatched response is KEPT and
    listed in result.unmatched, never dropped: it is real feedback, and a member
    who cancelled after answering still said something worth reading."""
    by_id = {m.member_id: m for m in members}
    out = []
    for r in result.responses:
        m = by_id.get(r.mid)
        if m is None:
            result.unmatched.append(r.mid)
        out.append((r, m))
    return out
