"""
schemas.py - Help Me Tidy（空間魔法師）資料模型與規格定義
遵循 Pydantic v2 規範，整合 BACKEND_API.md 與 FRONTEND_API.md 之定義。
"""

from enum import Enum
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

# ==============================================================================
# 1. 全域錯誤模型與通用回應外殼 (ApiResponse Envelope)
# ==============================================================================


class ApiError(BaseModel):
    """標準化 API 錯誤細節模型"""

    code: str = Field(
        ...,
        description="機器可判讀之錯誤代碼，如 REVISION_CONFLICT, IDEMPOTENCY_CONFLICT 等",
    )
    message: str = Field(..., description="人類可讀之錯誤訊息")
    details: Optional[Dict[str, Any]] = Field(
        default=None, description="可選之附加錯誤資訊"
    )


T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """全域通用 API 回應包裝外殼 (Envelope)"""

    ok: bool = Field(default=True, description="請求是否成功")
    data: Optional[T] = Field(
        default=None, description="業務資料 payload，失敗時為 None"
    )
    error: Optional[ApiError] = Field(
        default=None, description="錯誤結構，成功時為 None"
    )
    revision: int = Field(..., description="當前 Session 之版本編號")
    request_id: Optional[str] = Field(default=None, description="請求去重追蹤識別碼")


# ==============================================================================
# 2. 狀態與列舉 (Enums)
# ==============================================================================


class Phase(str, Enum):
    """Session 所處之整體生命週期階段"""

    COLLECTING = "COLLECTING"  # 初始狀態：等待照片上傳
    ANALYZING = "ANALYZING"  # 正在進行多模態場景分析
    CONFIRMING_SCENE = "CONFIRMING_SCENE"  # 等待使用者確認物品與區域
    PLANNING = "PLANNING"  # Agent 規劃整理計畫中
    EXECUTING = "EXECUTING"  # 步驟執行中
    COMPLETED = "COMPLETED"  # 所有步驟已完成


class AgentStatus(str, Enum):
    """Agent 回應時所代表之業務語意狀態"""

    NEED_INFO = "NEED_INFO"  # 資訊不足，需向使用者提出反問
    PLAN_READY = "PLAN_READY"  # 計畫已生成完畢
    ANSWER = "ANSWER"  # 針對使用者問題的直接回答（如位置查詢）
    FAILED = "FAILED"  # Agent 內部規劃失敗


class StepStatus(str, Enum):
    """整理步驟之執行狀態"""

    PENDING = "PENDING"  # 待執行
    DONE = "DONE"  # 已完成
    BLOCKED = "BLOCKED"  # 被阻擋（如抽屜已滿）


class RegionStatus(str, Enum):
    """收納區域之狀態"""

    AVAILABLE = "AVAILABLE"  # 可用
    FULL = "FULL"  # 已滿
    EXCLUDED = "EXCLUDED"  # 使用者排除禁用


# ==============================================================================
# 3. 核心領域模型 (Domain Models)
# ==============================================================================


class Item(BaseModel):
    """辨識出的待整理物品"""

    item_id: str = Field(..., description="後端分配之唯一物品識別碼")
    name: str = Field(..., description="物品名稱")
    category: Optional[str] = Field(
        default=None, description="物品類別，如文具、衣物"
    )
    image_id: Optional[str] = Field(default=None, description="關聯之原始圖片識別碼")
    confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="辨識信心度"
    )
    confirmed: bool = Field(default=False, description="使用者是否已確認此物品")


class Region(BaseModel):
    """收納或放置區域"""

    region_id: str = Field(..., description="後端分配之唯一區域識別碼")
    name: str = Field(..., description="區域名稱，如書架第一層、書桌右側抽屜")
    status: RegionStatus = Field(
        default=RegionStatus.AVAILABLE, description="區域狀態"
    )
    notes: Optional[str] = Field(default=None, description="使用者或 Agent 補充備註")


class PlanStep(BaseModel):
    """整理計畫中的單一執行步驟"""

    step_id: str = Field(..., description="後端分配之唯一步驟識別碼")
    order: int = Field(..., ge=1, description="執行序號，從 1 開始遞增")
    item_id: str = Field(..., description="關聯之物品識別碼")
    item_name: str = Field(..., description="物品名稱快照")
    target_region_id: str = Field(..., description="目標放置區域識別碼")
    target_region_name: str = Field(..., description="目標區域名稱快照")
    instruction: str = Field(..., description="具體操作指引文字")
    status: StepStatus = Field(default=StepStatus.PENDING, description="步驟當前狀態")


class Plan(BaseModel):
    """完整整理計畫"""

    plan_id: str = Field(..., description="後端分配之唯一計畫識別碼")
    created_at: str = Field(..., description="建立時間之 ISO 格式字串")
    steps: List[PlanStep] = Field(default_factory=list, description="有序步驟列表")
    summary: Optional[str] = Field(default=None, description="整理建議總結")


class LocationRecord(BaseModel):
    """物品確認放置歷史紀錄（不可隨重新規劃被洗掉）"""

    record_id: str = Field(..., description="後端分配之位置紀錄識別碼")
    item_id: str = Field(..., description="物品識別碼")
    item_name: str = Field(..., description="物品名稱")
    region_id: str = Field(..., description="最終放置區域識別碼")
    region_name: str = Field(..., description="區域名稱")
    confirmed_at: str = Field(..., description="確認完成時間之 ISO 格式字串")
    source_step_id: Optional[str] = Field(
        default=None, description="觸發此確認的步驟 ID（若由步驟完成觸發）"
    )


class ImageMetadata(BaseModel):
    """已上傳照片之詮釋資料"""

    image_id: str = Field(..., description="圖片唯一識別碼")
    filename: str = Field(..., description="原始檔案名稱")
    uploaded_at: str = Field(..., description="上傳時間")
    size_bytes: int = Field(..., description="檔案大小位元組")


class SessionState(BaseModel):
    """單一整理會話之完整狀態聚合模型"""

    session_id: str = Field(..., description="會話識別碼")
    phase: Phase = Field(default=Phase.COLLECTING, description="當前會話階段")
    revision: int = Field(default=0, ge=0, description="樂觀鎖版本控制號")
    images: List[ImageMetadata] = Field(
        default_factory=list, description="上傳之照片清單"
    )
    items: List[Item] = Field(
        default_factory=list, description="識別與確認後之物品清單"
    )
    regions: List[Region] = Field(
        default_factory=list, description="可用與配置之收納區域清單"
    )
    active_plan: Optional[Plan] = Field(
        default=None, description="目前生效中之整理計畫"
    )
    locations: List[LocationRecord] = Field(
        default_factory=list, description="不可變之物品確認位置歷史紀錄"
    )
    requirements: Dict[str, Any] = Field(
        default_factory=dict, description="使用者整理偏好與額外限制"
    )


# ==============================================================================
# 4. API 請求資料模型 (Request DTOs)
# ==============================================================================


class CreateSessionRequest(BaseModel):
    """POST /sessions 建立會話請求"""

    user_notes: Optional[str] = Field(default=None, description="使用者初始補充需求說明")


class SceneConfirmationRequest(BaseModel):
    """PUT /sessions/{sid}/scene/confirmation 使用者確認場景請求"""

    items: List[Item] = Field(..., description="使用者編輯確認後的物品清單")
    regions: List[Region] = Field(..., description="使用者編輯確認後的區域清單")
    expected_revision: int = Field(..., description="前端當前持有的版本號")
    request_id: str = Field(..., description="請求去重追蹤碼")


class PlanCreationRequest(BaseModel):
    """POST /sessions/{sid}/plans 請求生成整理計畫"""

    requirements: Optional[Dict[str, Any]] = Field(
        default=None, description="整理偏好與硬限制"
    )
    expected_revision: int = Field(..., description="前端當前持有的版本號")
    request_id: str = Field(..., description="請求去重追蹤碼")


class StepCompletionRequest(BaseModel):
    """POST /sessions/{sid}/steps/{step_id}/completion 回報步驟完成"""

    expected_revision: int = Field(..., description="前端當前持有的版本號")
    request_id: str = Field(..., description="請求去重追蹤碼")


class LocationConfirmationRequest(BaseModel):
    """POST /sessions/{sid}/locations/confirmations 手動確認物品放置位置"""

    item_id: str = Field(..., description="物品識別碼")
    region_id: str = Field(..., description="放置區域識別碼")
    expected_revision: int = Field(..., description="前端當前持有的版本號")
    request_id: str = Field(..., description="請求去重追蹤碼")


class SendMessageRequest(BaseModel):
    """POST /sessions/{sid}/messages 對話互動請求"""

    message: str = Field(..., min_length=1, description="使用者輸入訊息")
    expected_revision: int = Field(..., description="前端當前持有的版本號")
    request_id: str = Field(..., description="請求去重追蹤碼")


# ==============================================================================
# 5. Agent 互動回應資料模型 (Agent Result DTOs)
# ==============================================================================


class AgentResult(BaseModel):
    """Agent 推理完成後回傳給後端 HTTP 層之結構"""

    status: AgentStatus = Field(..., description="Agent 決策語意狀態")
    message: str = Field(..., description="準備呈現給使用者的對話文字")
    suggested_actions: Optional[List[str]] = Field(
        default=None, description="建議的快捷選項"
    )
    plan: Optional[Plan] = Field(
        default=None, description="生成的計畫（若 status 為 PLAN_READY）"
    )
    tool_calls_count: int = Field(default=0, description="本次推論呼叫工具之次數統計")