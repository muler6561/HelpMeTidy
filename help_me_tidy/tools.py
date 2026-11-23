"""Offline tool contracts. Trusted context and user actions stay outside model arguments."""

from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from typing import Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from .scene import Contract, Question, SceneAnalysis, Text, allocate_id


class Requirement(Contract):
    requirement_id: Text
    kind: Literal['hard_constraint', 'preference', 'usage']
    rule: Literal['forbidden_region', 'required_region', 'keep_region_empty', 'usage_frequency', 'note']
    item_ids: list[Text]
    region_ids: list[Text]
    value: Text | None
    source_text: Text

    @model_validator(mode='after')
    def check_rule(self):
        if self.rule in ('forbidden_region', 'required_region'):
            if not self.item_ids or not self.region_ids:
                raise ValueError('位置規則需要物品及區域')
        if self.rule == 'required_region' and len(self.region_ids) != 1:
            raise ValueError('指定位置只允許一個區域')
        if self.rule == 'keep_region_empty' and (self.item_ids or not self.region_ids):
            raise ValueError('留空規則只能指定區域')
        if self.rule == 'usage_frequency' and (not self.item_ids or self.value is None):
            raise ValueError('使用頻率需要物品及值')
        if self.rule == 'note' and self.value is None:
            raise ValueError('備註需要值')
        return self


class Requirements(Contract):
    source_text: Text | None = None
    entries: list[Requirement]
    questions: list[Question]

    @model_validator(mode='after')
    def unique_ids(self):
        ids = [r.requirement_id for r in self.entries] + [q.question_id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError('需求及問題 ID 不可重複')
        if self.source_text is not None and any(r.source_text not in self.source_text for r in self.entries):
            raise ValueError('需求必須保留可追溯的原文')
        return self


class Assignment(Contract):
    item_id: Text
    region_id: Text
    reason: Text
    fit_status: Literal['confirmed', 'unknown']


class PlanStep(Contract):
    step_id: Text
    instruction: Text
    item_ids: list[Text] = Field(min_length=1)
    target_region_id: Text
    status: Literal['pending', 'completed']
    completed_at: Text | None

    @model_validator(mode='after')
    def check_completion(self):
        if (self.status == 'completed') != (self.completed_at is not None):
            raise ValueError('完成狀態與時間不一致')
        if self.completed_at is not None:
            if datetime.fromisoformat(self.completed_at).utcoffset() is None:
                raise ValueError('完成時間需要時區')
        if len(self.item_ids) != len(set(self.item_ids)):
            raise ValueError('步驟物品不可重複')
        return self


class Plan(Contract):
    plan_id: Text
    plan_version: int = Field(ge=1)
    based_on_revision: int = Field(ge=0)
    change_reason: Text
    assignments: list[Assignment]
    steps: list[PlanStep]
    pending_confirmations: list[Text]


class ToolContext(Contract):
    session_id: Text
    operation_id: Text | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class State(Contract):
    session_id: Text
    revision: int = 0
    images: list[dict] = Field(default_factory=list)
    scene: SceneAnalysis | None = None
    requirements: Requirements = Field(default_factory=lambda: Requirements(entries=[], questions=[]))
    current_plan: Plan | None = None
    locations: list[dict] = Field(default_factory=list)
    pending_questions: list[Question] = Field(default_factory=list)
    messages: list[dict] = Field(default_factory=list)


class Record(Contract):
    state: State
    plans: list[Plan] = Field(default_factory=list)
    requirements_history: list[Requirements] = Field(default_factory=list)
    location_history: list[dict] = Field(default_factory=list)
    completions: dict[str, str] = Field(default_factory=dict)


class ToolError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def success(data, revision):
    return {'ok': True, 'data': data, 'error': None, 'revision': revision}


def failure(code, message, revision):
    return {'ok': False, 'data': None, 'error': {
        'code': code, 'message': message, 'retryable': False, 'details': {},
    }, 'revision': revision}


class ToolBackend(Protocol):
    """Replace with C's adapter; run action and deduplication in one transaction."""

    def execute(self, context: ToolContext, name: str, payload: dict,
                write: bool, action) -> dict: ...


class MemoryBackend:
    def __init__(self):
        self._sessions: dict[str, Record] = {}
        self._operations: dict[str, tuple[dict, dict]] = {}
        self._lock = RLock()

    def create_session(self, scene: SceneAnalysis | None = None) -> dict:
        """Trusted fixture/bootstrap entry; scene must already be user-confirmed."""
        with self._lock:
            state = State(session_id=allocate_id('session'), scene=deepcopy(scene))
            self._sessions[state.session_id] = Record(state=state)
            return success(state.model_dump(mode='json'), 0)

    def execute(self, context, name, payload, write, action):
        with self._lock:
            stored = self._sessions.get(context.session_id)
            revision = stored.state.revision if stored else None
            fingerprint = {'context': context.model_dump(), 'name': name, 'payload': payload}
            if write:
                if context.operation_id is None or context.expected_revision is None:
                    return failure('INVALID_REQUEST', '寫入需要 operation_id 與 expected_revision', revision)
                previous = self._operations.get(context.operation_id)
                if previous:
                    if previous[0] != fingerprint:
                        return failure('IDEMPOTENCY_CONFLICT', '同一操作 ID 不可更換內容', revision)
                    return deepcopy(previous[1])
            if stored is None:
                return failure('SESSION_NOT_FOUND', '找不到工作階段', None)
            if write and context.expected_revision != revision:
                return failure('REVISION_CONFLICT', '請重新讀取最新狀態', revision)
            working = stored.model_copy(deep=True)
            try:
                data, changed = action(working)
            except ToolError as exc:
                return failure(exc.code, exc.message, revision)
            if write and changed:
                working.state.revision += 1
                self._sessions[context.session_id] = working
            result = success(data, working.state.revision)
            if write:
                self._operations[context.operation_id] = (deepcopy(fingerprint), deepcopy(result))
            return deepcopy(result)

    def history(self, session_id: str) -> dict:
        """Trusted inspection for tests/back-end handoff, not a model tool."""
        with self._lock:
            return self._sessions[session_id].model_dump(mode='json')


def references(state, item_ids, region_ids):
    items = {i.item_id for i in state.scene.items} if state.scene else set()
    regions = {r.region_id for r in state.scene.regions} if state.scene else set()
    if not set(item_ids) <= items or not set(region_ids) <= regions:
        raise ToolError('INVALID_REFERENCE', '引用不存在於本工作階段的物品或區域')


def validate(record: Record, candidate: Plan) -> dict:
    state = record.state
    violations, warnings = [], []

    def reject(code, message, ids=()):
        violations.append({'code': code, 'message': message, 'related_ids': list(ids)})

    if not state.scene or not state.scene.confirmed:
        reject('SCENE_UNCONFIRMED', '請先確認場景')
    if candidate.based_on_revision != state.revision:
        reject('REVISION_CONFLICT', '計畫依據不是最新狀態')
    old = state.current_plan
    if candidate.plan_version != (old.plan_version + 1 if old else 1) or (old and candidate.plan_id != old.plan_id):
        reject('PLAN_VERSION_CONFLICT', '計畫 ID 必須沿用且版本依序增加')
    assignments = {}
    regions = {r.region_id: r for r in state.scene.regions} if state.scene else {}
    for a in candidate.assignments:
        try:
            references(state, [a.item_id], [a.region_id])
        except ToolError:
            reject('INVALID_REFERENCE', '配置引用不存在的物品或區域', [a.item_id, a.region_id])
        if a.item_id in assignments:
            reject('DUPLICATE_ASSIGNMENT', '每物品最多一個配置', [a.item_id])
        assignments[a.item_id] = a.region_id
        region = regions.get(a.region_id)
        if region and region.availability == 'unavailable':
            reject('REGION_UNAVAILABLE', '不可使用禁用區域', [a.region_id])
        # No item sizes or capacity evidence exist in the current scene contract.
        if a.fit_status == 'confirmed':
            reject('UNSUPPORTED_FIT', '目前沒有容量證據，不可宣稱確定放得下', [a.item_id])
        warnings.append(f'{a.item_id} 在 {a.region_id} 的容量及適用性仍需確認')
    if warnings and not candidate.pending_confirmations:
        reject('MISSING_CONFIRMATION', '未知容量必須列入待確認事項')
    if state.requirements.questions:
        reject('UNRESOLVED_REQUIREMENTS', '需求仍有待回答問題')
    for req in state.requirements.entries:
        if req.kind != 'hard_constraint':
            continue
        if req.rule in ('note', 'usage_frequency'):
            reject('UNRESOLVED_CONSTRAINT', '硬限制無法直接驗證，請先釐清', [req.requirement_id])
        for item, region in assignments.items():
            forbidden = req.rule == 'forbidden_region' and item in req.item_ids and region in req.region_ids
            required = req.rule == 'required_region' and item in req.item_ids and region not in req.region_ids
            empty = req.rule == 'keep_region_empty' and region in req.region_ids
            if forbidden or required or empty:
                reject('CONSTRAINT_VIOLATION', '配置違反硬限制', [req.requirement_id, item, region])
    # Detect contradictory required locations even when omitted from the candidate.
    hard = [r for r in state.requirements.entries if r.kind == 'hard_constraint']
    for req in hard:
        if req.rule != 'required_region':
            continue
        target = req.region_ids[0]
        for other in hard:
            overlap = set(req.item_ids) & set(other.item_ids)
            if ((other.rule == 'required_region' and overlap and target not in other.region_ids)
                    or (other.rule == 'forbidden_region' and overlap and target in other.region_ids)
                    or (other.rule == 'keep_region_empty' and target in other.region_ids)):
                reject('CONFLICTING_REQUIREMENTS', '硬限制互相衝突，需使用者釐清', [req.requirement_id, other.requirement_id])
    historical = {s.step_id: s for p in record.plans for s in p.steps}
    seen, covered = set(), set()
    for step in candidate.steps:
        if step.step_id in seen:
            reject('DUPLICATE_STEP', '步驟 ID 重複', [step.step_id])
        seen.add(step.step_id)
        for item in step.item_ids:
            if assignments.get(item) != step.target_region_id:
                reject('STEP_TARGET_MISMATCH', '步驟與配置不一致', [step.step_id, item])
            if item in covered:
                reject('DUPLICATE_MOVE', '同一物品不可有多個移動步驟', [item])
            covered.add(item)
        prior = historical.get(step.step_id)
        if prior and (prior.instruction, set(prior.item_ids), prior.target_region_id) != (step.instruction, set(step.item_ids), step.target_region_id):
            reject('STEP_ID_REUSED', '操作改變時需配發新步驟 ID', [step.step_id])
        completed_at = record.completions.get(step.step_id)
        if step.completed_at != completed_at or step.status != ('completed' if completed_at else 'pending'):
            reject('INVALID_COMPLETION', '完成狀態只能來自使用者完成事件', [step.step_id])
    if set(assignments) - covered:
        reject('MISSING_STEP', '配置需要對應的整理步驟', set(assignments) - covered)
    if old:
        for step in old.steps:
            unchanged = all(assignments.get(i) == step.target_region_id for i in step.item_ids)
            if step.status == 'completed' and unchanged and step.step_id not in seen:
                reject('LOST_PROGRESS', '未受影響的已完成步驟必須保留', [step.step_id])
    return {'valid': not violations, 'violations': violations, 'warnings': warnings,
            'validated_revision': state.revision}


class EmptyArgs(Contract):
    pass


class RequirementsArgs(Contract):
    requirements: Requirements


class PlanArgs(Contract):
    candidate: Plan


class LookupArgs(Contract):
    item_id: Text


TOOL_SPECS = {
    'get_session_state': (EmptyArgs, False, '讀取物品、區域、需求、計畫與進度'),
    'update_requirements': (RequirementsArgs, True, '以完整集合保存使用者明確提出的需求，保留原文'),
    'validate_plan': (PlanArgs, False, '檢查計畫是否符合最新已知條件，不寫入'),
    'save_plan': (PlanArgs, True, '交易內重新驗證並保存下一版計畫，不確認位置'),
    'lookup_item_location': (LookupArgs, False, '查詢最後確認的位置與時間，另列建議位置'),
}


class Tools:
    def __init__(self, backend: ToolBackend):
        self.backend = backend

    def get_session_state(self, context):
        return self.call('get_session_state', {}, context)

    def update_requirements(self, context, requirements):
        return self.call('update_requirements', {'requirements': requirements}, context)

    def validate_plan(self, context, candidate):
        return self.call('validate_plan', {'candidate': candidate}, context)

    def save_plan(self, context, candidate):
        return self.call('save_plan', {'candidate': candidate}, context)

    def lookup_item_location(self, context, item_id):
        return self.call('lookup_item_location', {'item_id': item_id}, context)

    def definitions(self):
        return [{'name': name, 'description': desc, 'parameters': args.model_json_schema()}
                for name, (args, _, desc) in TOOL_SPECS.items()]

    def call(self, name: str, arguments: dict, context: ToolContext) -> dict:
        """Context comes from the trusted caller, never from model arguments."""
        if name not in TOOL_SPECS:
            return failure('UNKNOWN_TOOL', '工具未註冊', None)
        schema, write, _ = TOOL_SPECS[name]
        try:
            parsed = schema.model_validate(arguments)
        except ValidationError:
            def invalid(record):
                raise ToolError('INVALID_REQUEST', '工具參數格式錯誤')
            return self.backend.execute(context, name, {}, False,
                                        invalid)
        payload = parsed.model_dump(mode='json')
        return self.backend.execute(context, name, payload, write,
                                    lambda record: self._execute(name, parsed, record))

    def _execute(self, name, args, record):
        state = record.state
        if name == 'get_session_state':
            return state.model_dump(mode='json'), False
        if name == 'update_requirements':
            for req in args.requirements.entries:
                references(state, req.item_ids, req.region_ids)
            entities = ({i.item_id for i in state.scene.items} | {r.region_id for r in state.scene.regions}) if state.scene else set()
            for q in args.requirements.questions:
                if not set(q.related_ids) <= entities:
                    raise ToolError('INVALID_REFERENCE', '需求問題引用不存在的實體')
            state.requirements = args.requirements.model_copy(deep=True)
            record.requirements_history.append(state.requirements.model_copy(deep=True))
            return {'requirements': state.requirements.model_dump(mode='json')}, True
        if name in ('validate_plan', 'save_plan'):
            report = validate(record, args.candidate)
            if name == 'validate_plan':
                return report, False
            if not report['valid']:
                codes = {v['code'] for v in report['violations']}
                code = next((c for c in ('REVISION_CONFLICT', 'PLAN_VERSION_CONFLICT') if c in codes), 'PLAN_INVALID')
                raise ToolError(code, '計畫未通過驗證，請呼叫 validate_plan 查看原因')
            state.current_plan = args.candidate.model_copy(deep=True)
            record.plans.append(args.candidate.model_copy(deep=True))
            return {'plan': state.current_plan.model_dump(mode='json')}, True
        if name == 'lookup_item_location':
            if not state.scene or args.item_id not in {i.item_id for i in state.scene.items}:
                raise ToolError('ITEM_NOT_FOUND', '沒有此物品紀錄')
            location = next((l for l in state.locations if l['item_id'] == args.item_id), None)
            suggested = next((a.region_id for a in state.current_plan.assignments if a.item_id == args.item_id), None) if state.current_plan else None
            return {'item_id': args.item_id, 'location': location, 'suggested_region_id': suggested}, False
        raise ToolError('UNKNOWN_TOOL', '工具未註冊')


class UserActions:
    """Only expose to authenticated explicit user operations; never register as tools."""

    def __init__(self, backend: ToolBackend):
        self.backend = backend

    @staticmethod
    def _location(record, item_id, region_id, timestamp, step_id=None):
        location = {'item_id': item_id, 'region_id': region_id, 'confirmed_at': timestamp,
                    'source': 'step_completion' if step_id else 'user_confirmation', 'step_id': step_id}
        record.location_history.append(location.copy())
        record.state.locations = [l for l in record.state.locations if l['item_id'] != item_id] + [location]
        return location

    def confirm_item_location(self, context: ToolContext, item_id: str, region_id: str):
        def action(record):
            references(record.state, [item_id], [region_id])
            return self._location(record, item_id, region_id, datetime.now(timezone.utc).isoformat()), True
        return self.backend.execute(context, 'confirm_item_location',
                                    {'item_id': item_id, 'region_id': region_id}, True, action)

    def complete_step(self, context: ToolContext, plan_version: int, step_id: str):
        def action(record):
            plan = record.state.current_plan
            if plan is None or plan.plan_version != plan_version:
                raise ToolError('PLAN_VERSION_CONFLICT', '請使用目前計畫版本')
            step = next((s for s in plan.steps if s.step_id == step_id), None)
            if step is None:
                raise ToolError('INVALID_REFERENCE', '找不到步驟')
            if step.status == 'completed':
                return {'step': step.model_dump(), 'locations': [l for l in record.state.locations if l['item_id'] in step.item_ids]}, False
            timestamp = datetime.now(timezone.utc).isoformat()
            step.status, step.completed_at = 'completed', timestamp
            record.completions[step_id] = timestamp
            locations = [self._location(record, i, step.target_region_id, timestamp, step_id) for i in step.item_ids]
            return {'step': step.model_dump(), 'locations': locations}, True
        return self.backend.execute(context, 'complete_step',
                                    {'plan_version': plan_version, 'step_id': step_id}, True, action)
