"""Offline first-plan builder. No persistence, progress updates, or API calls."""

import re
from typing import Literal

from pydantic import Field, model_validator

from .scene import Contract, Question, SceneAnalysis, Text, allocate_id


class Requirement(Contract):
    requirement_id: Text
    kind: Literal["hard_constraint", "preference", "usage"]
    rule: Literal["forbidden_region", "required_region", "keep_region_empty", "usage_frequency", "note"]
    item_ids: list[Text]
    region_ids: list[Text]
    value: Text | None
    source_text: Text

    @model_validator(mode="after")
    def check_rule(self):
        if len(self.item_ids) != len(set(self.item_ids)) or len(self.region_ids) != len(set(self.region_ids)):
            raise ValueError("需求引用不可重複")
        if self.rule in ("forbidden_region", "required_region"):
            if self.kind not in ("hard_constraint", "preference"):
                raise ValueError("位置條件必須為硬限制或偏好")
            if not self.item_ids or not self.region_ids:
                raise ValueError("位置條件必須指定物品及區域")
            if self.rule == "required_region" and len(self.region_ids) != 1:
                raise ValueError("指定位置必須只有一個目標")
        if self.rule == "keep_region_empty":
            if self.kind != "hard_constraint" or self.item_ids or not self.region_ids:
                raise ValueError("留空條件必須指定區域且不指定物品")
        if self.rule == "usage_frequency":
            if self.kind != "usage" or not self.item_ids or self.region_ids or self.value is None:
                raise ValueError("使用頻率必須指定物品及頻率")
        if self.rule == "note" and (self.kind == "hard_constraint" or self.value is None):
            raise ValueError("不可用備註掩蓋硬限制")
        return self


class Requirements(Contract):
    source_text: Text
    entries: list[Requirement]
    questions: list[Question]

    @model_validator(mode="after")
    def check_sources(self):
        ids = [e.requirement_id for e in self.entries] + [q.question_id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("需求與問題 ID 不可重複")
        if any(e.source_text not in self.source_text for e in self.entries):
            raise ValueError("需求必須保留可追溯的原文")
        return self


class Assignment(Contract):
    item_id: Text
    region_id: Text
    reason: Text
    # SceneAnalysis has no item dimensions/capacity evidence, even if region sizes exist.
    fit_status: Literal["unknown"] = "unknown"


class PlanStep(Contract):
    step_id: Text
    instruction: Text
    item_ids: list[Text] = Field(min_length=1)
    target_region_id: Text
    status: Literal["pending"] = "pending"
    completed_at: None = None


class Plan(Contract):
    plan_id: Text
    plan_version: Literal[1] = 1
    based_on_revision: int = Field(ge=0)
    change_reason: Text
    assignments: list[Assignment]
    steps: list[PlanStep]
    pending_confirmations: list[Text]


class PlanningResult(Contract):
    """Local draft result; HTTP AgentResult/save_plan belong to later steps."""

    status: Literal["needs_input", "plan_ready"]
    message: Text
    questions: list[Question]
    plan: Plan | None

    @model_validator(mode="after")
    def check_status(self):
        if self.status == "needs_input" and (not self.questions or self.plan is not None):
            raise ValueError("需要補充時必須提供問題且不得回傳計畫")
        if self.status == "plan_ready" and (self.plan is None or self.questions):
            raise ValueError("規劃成功必須提供計畫")
        return self


def _question(text, related_ids, allocator):
    return Question(question_id=allocator("question"), text=text, related_ids=related_ids)


def _regions(scene, text):
    # Only explicit, unambiguous names; never invent a left/right subdivision.
    normalized = text.replace("左邊", "左側").replace("右邊", "右側")
    matches = [r.region_id for r in scene.regions
               if r.name.replace("左邊", "左側").replace("右邊", "右側") == normalized]
    if text == "抽屜":
        return [r.region_id for r in scene.regions if "抽屜" in r.name]
    return matches


def parse_requirements(scene: SceneAnalysis, user_message: str, *, allocator=allocate_id) -> Requirements:
    """Conservative Chinese grammar. Unsupported clauses become questions, never disappear."""
    scene = SceneAnalysis.model_validate(scene.model_dump())
    entries, questions = [], []
    context_item = None
    clauses = [s.strip() for s in re.split(r"[，,。；;\n]+", user_message) if s.strip()]
    if not clauses:
        raise ValueError("需求不可空白")
    for clause in clauses:
        item = None
        tail = clause
        matches = [i for i in scene.items if clause.startswith(i.name)]
        if matches:
            longest = max(len(i.name) for i in matches)
            matches = [i for i in matches if len(i.name) == longest]
            if len(matches) == 1:
                item = matches[0].item_id
                context_item = item
                tail = clause[len(matches[0].name):].strip()
            else:
                context_item = None
        elif re.match(r"^(不要|不能|禁止|必須|要|希望|偏好)", clause):
            item = context_item
        else:
            context_item = None

        rule, kind, regions, value = None, "hard_constraint", [], None
        empty = re.fullmatch(r"(.+?)(?:要|必須)?留空", clause)
        position = re.fullmatch(r"(不要|不能|禁止|必須|要|希望|偏好)放(?:進|入|在|到)?(.+)", tail)
        if empty:
            regions = _regions(scene, empty[1])
            item = None
            context_item = None
            if len(regions) == 1:
                rule = "keep_region_empty"
        elif item and tail in ("每天用", "每天使用", "很少用", "很少使用"):
            rule, kind = "usage_frequency", "usage"
            value = "daily" if tail.startswith("每天") else "rarely"
        elif item and position:
            regions = _regions(scene, position[2])
            negative = position[1] in ("不要", "不能", "禁止")
            if regions and (negative or len(regions) == 1):
                rule = "forbidden_region" if negative else "required_region"
                if position[1] in ("希望", "偏好"):
                    kind = "preference"
        if rule:
            entries.append(Requirement(
                requirement_id=allocator("requirement"), kind=kind, rule=rule,
                item_ids=[item] if item else [], region_ids=regions,
                value=value, source_text=clause,
            ))
        else:
            questions.append(_question(
                f"請釐清「{clause}」的物品、區域或條件；請使用已登錄名稱，位置請指定明確區域。",
                [item] if item else [], allocator,
            ))
            context_item = None
    return Requirements(source_text=user_message, entries=entries, questions=questions)


def _reference_errors(scene, requirements):
    items = {i.item_id for i in scene.items}
    regions = {r.region_id for r in scene.regions}
    errors = []
    for entry in requirements.entries:
        if not set(entry.item_ids) <= items or not set(entry.region_ids) <= regions:
            errors.append(f"需求 {entry.requirement_id} 引用不存在的物品或區域")
    for question in requirements.questions:
        if not set(question.related_ids) <= items | regions:
            errors.append("需求問題引用不存在的物品或區域")
    return errors


def _allowed(scene, requirements, item_id):
    allowed = {r.region_id for r in scene.regions if r.availability != "unavailable"}
    for entry in requirements.entries:
        if entry.kind != "hard_constraint":
            continue
        if entry.rule == "keep_region_empty":
            allowed -= set(entry.region_ids)
        elif item_id in entry.item_ids:
            if entry.rule == "forbidden_region":
                allowed -= set(entry.region_ids)
            elif entry.rule == "required_region":
                allowed &= set(entry.region_ids)
    return allowed


def validate_plan(scene: SceneAnalysis, requirements: Requirements, plan: Plan) -> list[str]:
    """Pure local validation, returning violations. Not the step-five storage tool."""
    scene = SceneAnalysis.model_validate(scene.model_dump())
    requirements = Requirements.model_validate(requirements.model_dump())
    plan = Plan.model_validate(plan.model_dump())
    errors = _reference_errors(scene, requirements)
    if not scene.confirmed or requirements.questions:
        errors.append("場景尚未確認或需求仍有待回答問題")
    items = {i.item_id: i for i in scene.items}
    regions = {r.region_id: r for r in scene.regions}
    assignments = {a.item_id: a.region_id for a in plan.assignments}
    if len(assignments) != len(plan.assignments) or set(assignments) != set(items):
        errors.append("每個已登錄物品必須恰有一個配置")
    for assignment in plan.assignments:
        if assignment.item_id not in items or assignment.region_id not in regions:
            errors.append("配置引用不存在的物品或區域")
            continue
        if assignment.region_id not in _allowed(scene, requirements, assignment.item_id):
            errors.append(f"{assignment.item_id} 違反禁用、留空或指定位置條件")
        if items[assignment.item_id].needs_confirmation or regions[assignment.region_id].needs_confirmation:
            errors.append("配置使用尚待確認的物品或區域")
        if regions[assignment.region_id].availability != "available":
            errors.append("配置區域尚未確認可用")
        warning = _fit_question(items[assignment.item_id].name, regions[assignment.region_id].name)
        if warning not in plan.pending_confirmations:
            errors.append("未知容量必須列入待確認事項")
    ids = [plan.plan_id] + [s.step_id for s in plan.steps]
    if len(set(ids)) != len(ids):
        errors.append("計畫或步驟 ID 重複")
    covered = []
    for step in plan.steps:
        covered.extend(step.item_ids)
        if any(assignments.get(item_id) != step.target_region_id for item_id in step.item_ids):
            errors.append("步驟物品或目標與配置不一致")
    if len(covered) != len(set(covered)) or set(covered) != set(items):
        errors.append("每個物品必須恰有一個整理步驟")
    return errors


def _fit_question(item_name, region_name):
    return f"請確認「{item_name}」在「{region_name}」的可用容量與實際尺寸；目前不能確定放得下。"


def create_plan(scene: SceneAnalysis, requirements: Requirements, *, based_on_revision: int = 0,
                allocator=allocate_id) -> PlanningResult:
    scene = SceneAnalysis.model_validate(scene.model_dump())
    requirements = Requirements.model_validate(requirements.model_dump())
    reference_errors = _reference_errors(scene, requirements)
    if reference_errors:
        raise ValueError("；".join(reference_errors))
    questions = list(requirements.questions)
    if not scene.confirmed:
        questions.append(_question("請先確認現況辨識結果，再建立計畫。", [], allocator))
    if not scene.items:
        questions.append(_question("尚未登錄物品，請先補充現況。", [], allocator))
    assignments, steps, pending = [], [], []
    for item in scene.items:
        if item.needs_confirmation:
            questions.append(_question(f"請先確認物品「{item.name}」。", [item.item_id], allocator))
            continue
        allowed = _allowed(scene, requirements, item.item_id)
        if not allowed:
            questions.append(_question(
                f"「{item.name}」沒有符合所有限制的區域；請確認衝突條件或補充可用區域。",
                [item.item_id], allocator))
            continue
        available = [r for r in scene.regions if r.region_id in allowed
                     and r.availability == "available" and not r.needs_confirmation]
        if not available:
            questions.append(_question(f"請確認「{item.name}」可放置區域的可用狀態。",
                                       [item.item_id] + sorted(allowed), allocator))
            continue
        preferred = {rid for e in requirements.entries if e.kind == "preference"
                     and e.rule == "required_region" and item.item_id in e.item_ids for rid in e.region_ids}
        avoided = {rid for e in requirements.entries if e.kind == "preference"
                   and e.rule == "forbidden_region" and item.item_id in e.item_ids for rid in e.region_ids}
        target = min(available, key=lambda r: (r.region_id in avoided,
                     r.region_id not in preferred, r.region_id != item.observed_region_id, r.region_id))
        reason = "符合所有硬限制，依位置偏好優先，再優先沿用觀察區域以減少搬動。"
        usage = [e.value for e in requirements.entries if e.rule == "usage_frequency" and item.item_id in e.item_ids]
        if usage:
            reason += f"使用頻率：{'、'.join(usage)}；整理時請確認取用方便。"
        assignments.append(Assignment(item_id=item.item_id, region_id=target.region_id, reason=reason))
        steps.append(PlanStep(step_id=allocator("step"), item_ids=[item.item_id],
                             target_region_id=target.region_id,
                             instruction=f"先確認「{target.name}」有足夠空間；若可容納，將「{item.name}」整理至該區域；若放不下請回報。"))
        pending.append(_fit_question(item.name, target.name))
    if questions:
        return PlanningResult(status="needs_input", message="請先補充或釐清以下條件。", questions=questions, plan=None)
    # Preserve unrelated unknowns too, without presenting them as resolved facts.
    pending.extend(q.text for q in scene.questions)
    plan = Plan(plan_id=allocator("plan"), based_on_revision=based_on_revision,
                change_reason="依已確認現況與需求建立第一版收納計畫。",
                assignments=assignments, steps=steps, pending_confirmations=pending)
    violations = validate_plan(scene, requirements, plan)
    if violations:
        raise ValueError("；".join(violations))
    return PlanningResult(status="plan_ready", message="第一版計畫已通過本地檢查，尚未保存至 session；容量仍待確認。",
                          questions=[], plan=plan)
