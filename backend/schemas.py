"""
schemas.py - Help Me Tidy（空間魔法師）資料模型與規格定義
嚴格遵循 BACKEND_API.md 與 FRONTEND_API.md 規範（Pydantic v2）。
"""

from enum import Enum
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import AwareDatetime, BaseModel, Field, model_validator


# ==============================================================================
# 1. 列舉定義 (Enums) - 全部採用小寫 snake_case
# ==============================================================================


class RegionAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class SpatialRelationType(str, Enum):
    INSIDE = "inside"
    ON = "on"
    NEAR = "near"


class RequirementKind(str, Enum):
    HARD_CONSTRAINT = "hard_constraint"
    PREFERENCE = "preference"
    USAGE = "usage"


class RequirementRule(str, Enum):
    FORBIDDEN_REGION = "forbidden_region"
    REQUIRED_REGION = "required_region"
    KEEP_REGION_EMPTY = "keep_region_empty"
    USAGE_FREQUENCY = "usage_frequency"
    NOTE = "note"


class FitStatus(str, Enum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


class StepStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"


class LocationSource(str, Enum):
    STEP_COMPLETION = "step_completion"
    USER_CONFIRMATION = "user_confirmation"


class AgentStatus(str, Enum):
    NEEDS_INPUT = "needs_input"
    PLAN_READY = "plan_ready"
    ANSWERED = "answered"
    FAILED = "failed"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


# ==============================================================================
# 2. 共用錯誤與內部工具回傳外殼 (Error & Tool Envelope)
# ==============================================================================


class Error(BaseModel):
    """標準錯誤結構（不得包含金鑰、原始堆疊或模型隱藏推理）"""

    code: str
    message: str
    retryable: bool
    details: Dict[str, Any] = Field(default_factory=dict)


T = TypeVar("T")


class ToolEnvelope(BaseModel, Generic[T]):
    """Agent 工具與後端可信服務接口的統一回傳外殼"""

    ok: bool
    data: Optional[T] = None
    error: Optional[Error] = None
    revision: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_envelope_consistency(self) -> "ToolEnvelope[T]":
        if self.ok and self.error is not None:
            raise ValueError("ok 為 true 時，error 必須為 null")
        if not self.ok:
            if self.data is not None:
                raise ValueError("ok 為 false 時，data 必須為 null")
            if self.error is None:
                raise ValueError("ok 為 false 時，error 不可為 null")
        return self


# ==============================================================================
# 3. 核心領域模型 (BACKEND_API.md 第 2 節)
# ==============================================================================


class ImageRef(BaseModel):
    """已上傳且屬於該 session 的圖片引用（不含本機路徑或 base64）"""

    image_id: str
    mime_type: str
    size_bytes: int = Field(..., ge=0)


class Dimensions(BaseModel):
    """空間尺寸（有值時必須嚴格大於 0）"""

    width_cm: Optional[float] = Field(default=None, gt=0)
    depth_cm: Optional[float] = Field(default=None, gt=0)
    height_cm: Optional[float] = Field(default=None, gt=0)


class Item(BaseModel):
    item_id: str
    name: str
    observed_region_id: Optional[str] = None
    observation: str
    needs_confirmation: bool


class Region(BaseModel):
    region_id: str
    name: str
    availability: RegionAvailability
    dimensions: Dimensions = Field(default_factory=Dimensions)
    needs_confirmation: bool


class SpatialRelation(BaseModel):
    """僅表達照片觀察，並非使用者確認位置"""

    item_id: str
    region_id: str
    relation: SpatialRelationType


class Question(BaseModel):
    question_id: str
    text: str
    related_ids: List[str] = Field(default_factory=list)


class SceneAnalysis(BaseModel):
    image_ids: List[str]
    items: List[Item]
    regions: List[Region]
    relations: List[SpatialRelation]
    questions: List[Question]
    confirmed: bool


class Requirement(BaseModel):
    requirement_id: str
    kind: RequirementKind
    rule: RequirementRule
    item_ids: List[str] = Field(default_factory=list)
    region_ids: List[str] = Field(default_factory=list)
    value: Optional[str] = None
    source_text: str

    @model_validator(mode="after")
    def check_requirement_rules(self) -> "Requirement":
        if self.rule in (
            RequirementRule.FORBIDDEN_REGION,
            RequirementRule.REQUIRED_REGION,
        ):
            if not self.item_ids or not self.region_ids:
                raise ValueError(f"{self.rule.value} 至少須指定一個 item 與一個 region")
            if self.rule == RequirementRule.REQUIRED_REGION and len(self.region_ids) != 1:
                raise ValueError("required_region 只允許指定一個目標 region")
        elif self.rule == RequirementRule.KEEP_REGION_EMPTY:
            if not self.region_ids:
                raise ValueError("keep_region_empty 必須指定至少一個 region")
            if self.item_ids:
                raise ValueError("keep_region_empty 的 item_ids 必須為空")
        elif self.rule == RequirementRule.USAGE_FREQUENCY:
            if not self.item_ids:
                raise ValueError("usage_frequency 必須指定至少一個 item")
        return self


class Requirements(BaseModel):
    entries: List[Requirement] = Field(default_factory=list)
    questions: List[Question] = Field(default_factory=list)


class Assignment(BaseModel):
    item_id: str
    region_id: str
    reason: str
    fit_status: FitStatus


class PlanStep(BaseModel):
    step_id: str
    instruction: str
    item_ids: List[str] = Field(..., min_length=1)
    target_region_id: str
    status: StepStatus
    completed_at: Optional[AwareDatetime] = None

    @model_validator(mode="after")
    def check_completion_timestamp(self) -> "PlanStep":
        if self.status == StepStatus.COMPLETED and self.completed_at is None:
            raise ValueError("status 為 completed 時，completed_at 必須有值")
        if self.status == StepStatus.PENDING and self.completed_at is not None:
            raise ValueError("status 為 pending 時，completed_at 必須為 null")
        return self


class Plan(BaseModel):
    plan_id: str
    plan_version: int = Field(..., ge=1)
    based_on_revision: int = Field(..., ge=0)
    change_reason: str
    assignments: List[Assignment]
    steps: List[PlanStep]
    pending_confirmations: List[str] = Field(default_factory=list)


class LocationRecord(BaseModel):
    item_id: str
    region_id: str
    confirmed_at: AwareDatetime
    source: LocationSource
    step_id: Optional[str] = None

    @model_validator(mode="after")
    def check_source_step_id(self) -> "LocationRecord":
        if self.source == LocationSource.USER_CONFIRMATION and self.step_id is not None:
            raise ValueError("直接確認位置 (user_confirmation) 時，step_id 必須為 null")
        if self.source == LocationSource.STEP_COMPLETION and not self.step_id:
            raise ValueError("步驟完成確認 (step_completion) 時，step_id 不可為空")
        return self


class Message(BaseModel):
    message_id: str
    role: MessageRole
    text: str
    created_at: AwareDatetime
    request_id: str


class AgentResult(BaseModel):
    request_id: str
    session_id: str
    revision: int = Field(..., ge=0)
    status: AgentStatus
    message: str
    questions: List[Question] = Field(default_factory=list)
    plan: Optional[Plan] = None
    location: Optional[LocationRecord] = None
    error: Optional[Error] = None

    @model_validator(mode="after")
    def check_status_contract(self) -> "AgentResult":
        if self.status == AgentStatus.FAILED:
            if self.error is None:
                raise ValueError("status 為 failed 時，error 不可為 null")
        else:
            if self.error is not None:
                raise ValueError("非 failed 狀態時，error 必須為 null")

        if self.status == AgentStatus.NEEDS_INPUT and not self.questions:
            raise ValueError("status 為 needs_input 時，questions 至少須包含一題")
        if self.status == AgentStatus.PLAN_READY and self.plan is None:
            raise ValueError("status 為 plan_ready 時，必須提供已保存的 plan")
        return self


class SessionState(BaseModel):
    session_id: str
    revision: int = Field(default=0, ge=0)
    images: List[ImageRef] = Field(default_factory=list)
    scene: Optional[SceneAnalysis] = None
    requirements: Requirements = Field(default_factory=Requirements)
    current_plan: Optional[Plan] = None
    locations: List[LocationRecord] = Field(default_factory=list)
    pending_questions: List[Question] = Field(default_factory=list)
    messages: List[Message] = Field(default_factory=list)


# ==============================================================================
# 4. Agent 工具專用輔助模型 (BACKEND_API.md 第 5 節)
# ==============================================================================


class RegionUpdate(BaseModel):
    region_id: str
    availability: Optional[RegionAvailability] = None
    dimensions: Optional[Dimensions] = None

    @model_validator(mode="after")
    def check_at_least_one_field(self) -> "RegionUpdate":
        if self.availability is None and self.dimensions is None:
            raise ValueError("RegionUpdate 至少須提供 availability 或 dimensions 其中之一")
        return self


class Violation(BaseModel):
    code: str
    message: str
    related_ids: List[str] = Field(default_factory=list)


class PlanValidationData(BaseModel):
    valid: bool
    violations: List[Violation] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    validated_revision: int = Field(..., ge=0)


class LocationLookupData(BaseModel):
    item_id: str
    location: Optional[LocationRecord] = None
    suggested_region_id: Optional[str] = None


# ==============================================================================
# 5. 前端 HTTP API 請求與回應模型 (FRONTEND_API.md 第 3、5 節)
# ==============================================================================


class HttpErrorResponse(BaseModel):
    """非 2xx HTTP 回應的固定錯誤格式"""

    request_id: str
    error: Error


class CreateSessionRequest(BaseModel):
    request_id: str


class CreateSessionResponse(BaseModel):
    request_id: str
    session_id: str
    revision: int = Field(default=0, ge=0)


class UploadImagesResponse(BaseModel):
    request_id: str
    session_id: str
    revision: int = Field(..., ge=0)
    images: List[ImageRef]


class AnalyzeSceneRequest(BaseModel):
    request_id: str
    expected_revision: int = Field(..., ge=0)
    image_ids: List[str] = Field(..., min_length=1)
    user_message: str


class ConfirmSceneRequest(BaseModel):
    request_id: str
    expected_revision: int = Field(..., ge=0)
    scene: SceneAnalysis


class SceneResponse(BaseModel):
    """POST /analysis 與 PUT /scene/confirmation 的成功回傳格式"""

    request_id: str
    session_id: str
    revision: int = Field(..., ge=0)
    scene: SceneAnalysis


class CreatePlanRequest(BaseModel):
    request_id: str
    expected_revision: int = Field(..., ge=0)
    user_message: str


class SendMessageRequest(BaseModel):
    request_id: str
    expected_revision: int = Field(..., ge=0)
    user_message: str


class CompleteStepRequest(BaseModel):
    request_id: str
    expected_revision: int = Field(..., ge=0)
    plan_version: int = Field(..., ge=1)


class CompleteStepResponse(BaseModel):
    request_id: str
    session_id: str
    revision: int = Field(..., ge=0)
    step: PlanStep
    locations: List[LocationRecord]


class ConfirmLocationRequest(BaseModel):
    request_id: str
    expected_revision: int = Field(..., ge=0)
    item_id: str
    region_id: str


class ConfirmLocationResponse(BaseModel):
    request_id: str
    session_id: str
    revision: int = Field(..., ge=0)
    location: LocationRecord


class ItemLocationResponse(BaseModel):
    """GET /sessions/{sid}/items/{item_id}/location 的成功回傳格式"""

    session_id: str
    revision: int = Field(..., ge=0)
    item_id: str
    location: Optional[LocationRecord] = None
    suggested_region_id: Optional[str] = None