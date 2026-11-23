"""Validated scene contracts and explicit user confirmation (no storage side effects)."""

from collections.abc import Callable
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Size = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Dimensions(Contract):
    width_cm: Size | None
    depth_cm: Size | None
    height_cm: Size | None


class Item(Contract):
    item_id: Text
    name: Text
    observed_region_id: Text | None
    observation: Text
    needs_confirmation: bool


class Region(Contract):
    region_id: Text
    name: Text
    availability: Literal["available", "unavailable", "unknown"]
    dimensions: Dimensions
    needs_confirmation: bool


class SpatialRelation(Contract):
    item_id: Text
    region_id: Text
    relation: Literal["inside", "on", "near"]


class Question(Contract):
    question_id: Text
    text: Text
    related_ids: list[Text]


class SceneAnalysis(Contract):
    image_ids: list[Text] = Field(min_length=1)
    items: list[Item]
    regions: list[Region]
    relations: list[SpatialRelation]
    questions: list[Question]
    confirmed: bool

    @model_validator(mode="after")
    def validate_references(self):
        items = {x.item_id for x in self.items}
        regions = {x.region_id for x in self.regions}
        ids = self.image_ids + [x.item_id for x in self.items] + [
            x.region_id for x in self.regions
        ] + [x.question_id for x in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("場景 ID 不可重複")
        for item in self.items:
            if item.observed_region_id is not None and item.observed_region_id not in regions:
                raise ValueError("物品引用不存在的區域")
        for relation in self.relations:
            if relation.item_id not in items or relation.region_id not in regions:
                raise ValueError("空間關係引用不存在的物品或區域")
        for question in self.questions:
            if not set(question.related_ids) <= items | regions:
                raise ValueError("問題引用不存在的物品或區域")
        for region in self.regions:
            if region.availability == "unknown" and not region.needs_confirmation:
                raise ValueError("未知可用狀態必須標示需確認")
        uncertain = {x.item_id for x in self.items if x.needs_confirmation} | {
            x.region_id for x in self.regions if x.needs_confirmation
        }
        covered = {ref for q in self.questions for ref in q.related_ids}
        if not uncertain <= covered:
            raise ValueError("需確認的物品或區域必須有相關問題")
        return self


def allocate_id(kind: str) -> str:
    return f"{kind}_{uuid4().hex}"


def materialize_scene(raw: str, image_ids: list[str],
                      allocator: Callable[[str], str] = allocate_id) -> SceneAnalysis:
    """Model IDs are temporary references only; never persist them as entity IDs."""
    draft = SceneAnalysis.model_validate_json(raw)
    if draft.confirmed:
        raise ValueError("模型不可代替使用者確認場景")
    for region in draft.regions:
        if any(value is not None for value in region.dimensions.model_dump().values()):
            raise ValueError("照片分析不得提供公分尺寸；請由使用者確認時補充")
    mapping = {x.item_id: allocator("item") for x in draft.items}
    mapping.update({x.region_id: allocator("region") for x in draft.regions})
    data = draft.model_dump()
    data["image_ids"] = image_ids
    for item in data["items"]:
        item["item_id"] = mapping[item["item_id"]]
        if item["observed_region_id"] is not None:
            item["observed_region_id"] = mapping[item["observed_region_id"]]
    for region in data["regions"]:
        region["region_id"] = mapping[region["region_id"]]
    for relation in data["relations"]:
        relation["item_id"] = mapping[relation["item_id"]]
        relation["region_id"] = mapping[relation["region_id"]]
    for question in data["questions"]:
        question["question_id"] = allocator("question")
        question["related_ids"] = [mapping[x] for x in question["related_ids"]]
    return SceneAnalysis.model_validate(data)


def confirm_scene(original: SceneAnalysis, edited: SceneAnalysis) -> SceneAnalysis:
    """Caller must obtain explicit user action; unknown values remain unknown."""
    original = SceneAnalysis.model_validate(original.model_dump())
    edited = SceneAnalysis.model_validate(edited.model_dump())
    if original.image_ids != edited.image_ids:
        raise ValueError("確認時不可替換照片 ID")
    if {x.item_id for x in original.items} != {x.item_id for x in edited.items}:
        raise ValueError("確認時必須保留原有物品 ID，不可新增或刪除物品")
    if {x.region_id for x in original.regions} != {x.region_id for x in edited.regions}:
        raise ValueError("確認時必須保留原有區域 ID，不可新增或刪除區域")
    data = edited.model_dump()
    data["confirmed"] = True
    return SceneAnalysis.model_validate(data)
