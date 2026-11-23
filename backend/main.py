"""
main.py - Help Me Tidy（空間魔法師）FastAPI HTTP 整合層
嚴格遵循 FRONTEND_API.md 與 BACKEND_API.md 規範：
1. Base Path 為 /api/v1，實作全部 10 條前端接口。
2. 圖片上傳驗證實際檔案標頭（JPEG/PNG magic bytes）、數量（1-5 張）與大小（<= 10 MiB）。
3. 統一將儲存層與驗證錯誤轉換為規格書規定的 HTTP 狀態碼與 HttpErrorResponse。
4. 內建 MockAgent（透過 AgentToolkit 操作 storage），方便在組員 B 完成前以 /docs 獨立驗收。
"""

import copy
import hashlib
import json
import threading
from typing import Any, Dict, List, Optional, Protocol

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from schemas import (
    AgentResult,
    AgentStatus,
    AnalyzeSceneRequest,
    Assignment,
    CompleteStepRequest,
    CompleteStepResponse,
    ConfirmLocationRequest,
    ConfirmLocationResponse,
    ConfirmSceneRequest,
    CreatePlanRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    Dimensions,
    Error,
    FitStatus,
    HttpErrorResponse,
    ImageRef,
    Item,
    ItemLocationResponse,
    LocationRecord,
    MessageRole,
    Plan,
    PlanStep,
    Question,
    Region,
    RegionAvailability,
    RegionUpdate,
    Requirement,
    RequirementKind,
    RequirementRule,
    Requirements,
    SceneAnalysis,
    SceneResponse,
    SendMessageRequest,
    SessionState,
    SpatialRelation,
    SpatialRelationType,
    StepStatus,
    ToolEnvelope,
    UploadImagesResponse,
)
from storage import MemoryStorage
from tools import AgentContext, AgentToolkit

# ==============================================================================
# 1. HTTP 狀態碼映射與統一錯誤回應工具（FRONTEND_API.md 第 5 節）
# ==============================================================================

ERROR_CODE_TO_HTTP_STATUS: Dict[str, int] = {
    "INVALID_REQUEST": 400,
    "SESSION_NOT_FOUND": 404,
    "ITEM_NOT_FOUND": 404,
    "REVISION_CONFLICT": 409,
    "PLAN_VERSION_CONFLICT": 409,
    "SCENE_NOT_CONFIRMED": 409,
    "PLAN_ALREADY_EXISTS": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "REQUEST_IN_PROGRESS": 409,
    "IMAGE_TOO_LARGE": 413,
    "UNSUPPORTED_IMAGE_TYPE": 415,
    "INVALID_SCENE": 422,
    "INVALID_REFERENCE": 422,
    "PLAN_INVALID": 422,
    "RATE_LIMITED": 429,
    "SERVICE_UNAVAILABLE": 503,
    "MODEL_UNAVAILABLE": 503,
    "MODEL_TIMEOUT": 503,
}


def http_error(
    request_id: str,
    code: str,
    message: str,
    retryable: bool = False,
    details: Optional[Dict[str, Any]] = None,
    status_code: Optional[int] = None,
) -> JSONResponse:
    code_status = status_code or ERROR_CODE_TO_HTTP_STATUS.get(code, 500)
    body = HttpErrorResponse(
        request_id=request_id,
        error=Error(
            code=code,
            message=message,
            retryable=retryable,
            details=details or {},
        ),
    )
    return JSONResponse(status_code=code_status, content=body.model_dump(mode="json"))


def envelope_error_to_http(request_id: str, env: ToolEnvelope[Any]) -> JSONResponse:
    err = env.error or Error(
        code="SERVICE_UNAVAILABLE", message="儲存服務發生未知錯誤。", retryable=True
    )
    return http_error(
        request_id=request_id,
        code=err.code,
        message=err.message,
        retryable=err.retryable,
        details=err.details,
    )


def detect_real_image_mime(content: bytes) -> Optional[str]:
    """驗證實際檔案內容標頭，不單純信任前端傳入的 Content-Type。"""
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


# ==============================================================================
# 2. HTTP 層複合請求去重快取（供 /analysis、/plans、/messages 使用）
# ==============================================================================


class HttpIdempotencyStore:
    """確保多步驟寫入端點重送同一個 request_id 時，直接原樣回傳結果。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _hash(payload: Any) -> str:
        raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def begin(
        self, request_id: str, payload: Any
    ) -> tuple[Optional[JSONResponse], str]:
        p_hash = self._hash(payload)
        with self._lock:
            entry = self._cache.get(request_id)
            if entry is not None:
                if entry["hash"] != p_hash:
                    return (
                        http_error(
                            request_id,
                            "IDEMPOTENCY_CONFLICT",
                            "相同的 request_id 被用於不同請求內容。",
                        ),
                        p_hash,
                    )
                if entry["status"] == "in_progress":
                    return (
                        http_error(
                            request_id,
                            "REQUEST_IN_PROGRESS",
                            "本次操作仍在執行中，請稍後以原 request_id 重試。",
                            retryable=True,
                        ),
                        p_hash,
                    )
                return (
                    JSONResponse(
                        status_code=entry["status_code"],
                        content=copy.deepcopy(entry["content"]),
                    ),
                    p_hash,
                )
            self._cache[request_id] = {"hash": p_hash, "status": "in_progress"}
        return None, p_hash

    def commit(
        self, request_id: str, p_hash: str, status_code: int, content: Dict[str, Any]
    ) -> None:
        with self._lock:
            self._cache[request_id] = {
                "hash": p_hash,
                "status": "completed",
                "status_code": status_code,
                "content": copy.deepcopy(content),
            }

    def abort(self, request_id: str) -> None:
        with self._lock:
            self._cache.pop(request_id, None)


# ==============================================================================
# 3. Agent 介面契約與 MockAgent（待組員 B 完成後直接替換）
# ==============================================================================


class AgentModuleProtocol(Protocol):
    def analyze_scene(
        self, context: AgentContext, images: List[ImageRef], user_message: str
    ) -> SceneAnalysis: ...

    def create_plan(
        self, toolkit: AgentToolkit, scene: SceneAnalysis, user_message: str
    ) -> AgentResult: ...

    def handle_message(
        self, toolkit: AgentToolkit, user_message: str
    ) -> AgentResult: ...


class MockAgent:
    """
    符合 BACKEND_API.md 與 FRONTEND_API.md 首次展示劇本的假 Agent，
    所有讀寫都透過 AgentToolkit 呼叫，方便驗證完整流程。
    """

    def analyze_scene(
        self, context: AgentContext, images: List[ImageRef], user_message: str
    ) -> SceneAnalysis:
        alloc = context.id_allocator
        item_book = alloc.item_id()
        item_pen = alloc.item_id()
        reg_desk = alloc.region_id()
        reg_drawer = alloc.region_id()
        reg_shelf = alloc.region_id()
        q_shelf = alloc.question_id()

        return SceneAnalysis(
            image_ids=[img.image_id for img in images],
            items=[
                Item(
                    item_id=item_book,
                    name="課本",
                    observed_region_id=reg_desk,
                    observation="桌面上的課本",
                    needs_confirmation=False,
                ),
                Item(
                    item_id=item_pen,
                    name="文具盒",
                    observed_region_id=reg_desk,
                    observation="桌面上的文具盒",
                    needs_confirmation=False,
                ),
            ],
            regions=[
                Region(
                    region_id=reg_desk,
                    name="桌面",
                    availability=RegionAvailability.AVAILABLE,
                    dimensions=Dimensions(),
                    needs_confirmation=False,
                ),
                Region(
                    region_id=reg_drawer,
                    name="抽屜",
                    availability=RegionAvailability.AVAILABLE,
                    dimensions=Dimensions(),
                    needs_confirmation=False,
                ),
                Region(
                    region_id=reg_shelf,
                    name="書架中層",
                    availability=RegionAvailability.UNKNOWN,
                    dimensions=Dimensions(),
                    needs_confirmation=True,
                ),
            ],
            relations=[
                SpatialRelation(
                    item_id=item_book,
                    region_id=reg_desk,
                    relation=SpatialRelationType.ON,
                ),
                SpatialRelation(
                    item_id=item_pen,
                    region_id=reg_desk,
                    relation=SpatialRelationType.ON,
                ),
            ],
            questions=[
                Question(
                    question_id=q_shelf,
                    text="書架中層目前可以使用嗎？",
                    related_ids=[reg_shelf],
                )
            ],
            confirmed=False,
        )

    def create_plan(
        self, toolkit: AgentToolkit, scene: SceneAnalysis, user_message: str
    ) -> AgentResult:
        ctx = toolkit.context
        alloc = ctx.id_allocator

        # 1. 將初始需求保存（保留原文）
        req_entry = Requirement(
            requirement_id=alloc.requirement_id(),
            kind=RequirementKind.PREFERENCE,
            rule=RequirementRule.NOTE,
            item_ids=[],
            region_ids=[],
            value=user_message,
            source_text=user_message,
        )
        req_res = toolkit.update_requirements(Requirements(entries=[req_entry], questions=[]))
        if not req_res["ok"]:
            return AgentResult(
                request_id=ctx.request_id,
                session_id=ctx.session_id,
                revision=ctx.expected_revision,
                status=AgentStatus.FAILED,
                message="保存需求失敗。",
                error=Error.model_validate(req_res["error"]),
            )

        # 2. 建立第一版計畫（假設前兩個可用區域）
        avail_regions = [
            r for r in scene.regions if r.availability == RegionAvailability.AVAILABLE
        ]
        if not avail_regions:
            q = Question(
                question_id=alloc.question_id(),
                text="目前沒有標示為可用的收納區域，請說明哪些區域可以使用？",
                related_ids=[r.region_id for r in scene.regions],
            )
            return AgentResult(
                request_id=ctx.request_id,
                session_id=ctx.session_id,
                revision=ctx.expected_revision,
                status=AgentStatus.NEEDS_INPUT,
                message="請先補充可用的收納區域。",
                questions=[q],
            )

        target_primary = avail_regions[-1]
        target_fallback = avail_regions[0]
        assignments: List[Assignment] = []
        steps: List[PlanStep] = []
        pending_conf: List[str] = []

        for idx, item in enumerate(scene.items):
            target = target_primary if idx == 0 else target_fallback
            assignments.append(
                Assignment(
                    item_id=item.item_id,
                    region_id=target.region_id,
                    reason=f"將{item.name}移至{target.name}以保持整潔",
                    fit_status=FitStatus.UNKNOWN,
                )
            )
            steps.append(
                PlanStep(
                    step_id=alloc.step_id(),
                    instruction=f"將{item.name}移到{target.name}",
                    item_ids=[item.item_id],
                    target_region_id=target.region_id,
                    status=StepStatus.PENDING,
                    completed_at=None,
                )
            )
            pending_conf.append(f"尚未確認{target.name}是否放得下{item.name}。")

        candidate = Plan(
            plan_id=alloc.plan_id(),
            plan_version=1,
            based_on_revision=ctx.expected_revision,
            change_reason="初次規劃",
            assignments=assignments,
            steps=steps,
            pending_confirmations=pending_conf,
        )

        val_res = toolkit.validate_plan(candidate)
        if not val_res["ok"] or not val_res["data"]["valid"]:
            return AgentResult(
                request_id=ctx.request_id,
                session_id=ctx.session_id,
                revision=ctx.expected_revision,
                status=AgentStatus.FAILED,
                message="生成的計畫未通過驗證。",
                error=Error(
                    code="PLAN_INVALID",
                    message="計畫驗證失敗。",
                    retryable=False,
                    details=val_res.get("data") or {},
                ),
            )

        save_res = toolkit.save_plan(candidate)
        if not save_res["ok"]:
            return AgentResult(
                request_id=ctx.request_id,
                session_id=ctx.session_id,
                revision=ctx.expected_revision,
                status=AgentStatus.FAILED,
                message="保存計畫失敗。",
                error=Error.model_validate(save_res["error"]),
            )

        saved_plan = Plan.model_validate(save_res["data"]["plan"])
        return AgentResult(
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            revision=ctx.expected_revision,
            status=AgentStatus.PLAN_READY,
            message="已為您建立第一版整理計畫。",
            questions=[],
            plan=saved_plan,
        )

    def handle_message(
        self, toolkit: AgentToolkit, user_message: str
    ) -> AgentResult:
        ctx = toolkit.context
        alloc = ctx.id_allocator
        state_res = toolkit.get_session_state()
        if not state_res["ok"]:
            return AgentResult(
                request_id=ctx.request_id,
                session_id=ctx.session_id,
                revision=ctx.expected_revision,
                status=AgentStatus.FAILED,
                message="無法讀取會話狀態。",
                error=Error.model_validate(state_res["error"]),
            )

        state = SessionState.model_validate(state_res["data"])

        # 場景尚未確認：不得開始規劃，要求先確認場景（BACKEND_API.md 第 4 節）
        if state.scene is None or not state.scene.confirmed:
            return AgentResult(
                request_id=ctx.request_id,
                session_id=ctx.session_id,
                revision=ctx.expected_revision,
                status=AgentStatus.ANSWERED,
                message="請先完成照片辨識結果的確認，再開始進行收納規劃。",
            )

        # 情境 A：位置查詢（透過 lookup_item_location 工具查詢）
        if "在哪" in user_message or "位置" in user_message:
            target_item = next(
                (i for i in state.scene.items if i.name in user_message),
                state.scene.items[0] if state.scene.items else None,
            )
            if target_item is None:
                return AgentResult(
                    request_id=ctx.request_id,
                    session_id=ctx.session_id,
                    revision=ctx.expected_revision,
                    status=AgentStatus.ANSWERED,
                    message="目前場景中沒有任何物品紀錄。",
                )
            loc_res = toolkit.lookup_item_location(target_item.item_id)
            loc_data = loc_res["data"]
            if loc_data["location"] is not None:
                loc = LocationRecord.model_validate(loc_data["location"])
                reg_name = next(
                    (r.name for r in state.scene.regions if r.region_id == loc.region_id),
                    loc.region_id,
                )
                return AgentResult(
                    request_id=ctx.request_id,
                    session_id=ctx.session_id,
                    revision=ctx.expected_revision,
                    status=AgentStatus.ANSWERED,
                    message=f"{target_item.name}最後確認放在{reg_name}。",
                    location=loc,
                )
            return AgentResult(
                request_id=ctx.request_id,
                session_id=ctx.session_id,
                revision=ctx.expected_revision,
                status=AgentStatus.ANSWERED,
                message=f"目前沒有{target_item.name}的已確認位置紀錄。",
                location=None,
            )

        # 情境 B：回饋「放不下」（保留未受影響步驟，更新區域與限制並產出新版計畫）
        if "放不下" in user_message and state.current_plan is not None:
            # 1. 若提到未知區域可用，先呼叫 update_regions
            unknown_regs = [
                r for r in state.scene.regions if r.availability == RegionAvailability.UNKNOWN
            ]
            if unknown_regs and ("可用" in user_message or "可以" in user_message):
                toolkit.update_regions(
                    updates=[
                        RegionUpdate(
                            region_id=unknown_regs[0].region_id,
                            availability=RegionAvailability.AVAILABLE,
                        )
                    ],
                    source_text=user_message,
                )

            # 2. 重新讀取最新狀態並替第一個未完成步驟的物品改配新區域
            fresh = SessionState.model_validate(toolkit.get_session_state()["data"])
            old_plan = fresh.current_plan
            assert old_plan is not None

            target_step = next(
                (s for s in old_plan.steps if s.status == StepStatus.PENDING), None
            )
            if target_step is None:
                return AgentResult(
                    request_id=ctx.request_id,
                    session_id=ctx.session_id,
                    revision=ctx.expected_revision,
                    status=AgentStatus.ANSWERED,
                    message="目前所有步驟皆已完成，無須重新規劃。",
                )

            affected_item_id = target_step.item_ids[0]
            old_reg_id = target_step.target_region_id
            alt_region = next(
                (
                    r
                    for r in fresh.scene.regions
                    if r.availability == RegionAvailability.AVAILABLE
                    and r.region_id != old_reg_id
                ),
                None,
            )
            if alt_region is None:
                q = Question(
                    question_id=alloc.question_id(),
                    text="目前沒有其他可用的收納區域，請問還有哪個區域可以放置？",
                    related_ids=[r.region_id for r in fresh.scene.regions],
                )
                return AgentResult(
                    request_id=ctx.request_id,
                    session_id=ctx.session_id,
                    revision=ctx.expected_revision,
                    status=AgentStatus.NEEDS_INPUT,
                    message="需要您指定其他可用區域才能重新規劃。",
                    questions=[q],
                )

            # 新增單件禁用限制（不把整個區域設為不可用）
            new_entries = list(fresh.requirements.entries)
            new_entries.append(
                Requirement(
                    requirement_id=alloc.requirement_id(),
                    kind=RequirementKind.HARD_CONSTRAINT,
                    rule=RequirementRule.FORBIDDEN_REGION,
                    item_ids=[affected_item_id],
                    region_ids=[old_reg_id],
                    source_text=user_message,
                )
            )
            toolkit.update_requirements(Requirements(entries=new_entries, questions=[]))

            new_assignments: List[Assignment] = []
            for a in old_plan.assignments:
                if a.item_id == affected_item_id:
                    new_assignments.append(
                        Assignment(
                            item_id=a.item_id,
                            region_id=alt_region.region_id,
                            reason=f"原區域放不下，改放至{alt_region.name}",
                            fit_status=FitStatus.UNKNOWN,
                        )
                    )
                else:
                    new_assignments.append(a.model_copy(deep=True))

            new_steps: List[PlanStep] = []
            for s in old_plan.steps:
                if s.step_id == target_step.step_id:
                    item_name = next(
                        (i.name for i in fresh.scene.items if i.item_id == affected_item_id),
                        affected_item_id,
                    )
                    new_steps.append(
                        PlanStep(
                            step_id=alloc.step_id(),  # 變更目標須配發新 step_id
                            instruction=f"將{item_name}移到{alt_region.name}",
                            item_ids=[affected_item_id],
                            target_region_id=alt_region.region_id,
                            status=StepStatus.PENDING,
                            completed_at=None,
                        )
                    )
                else:
                    # 未受影響的步驟保留 step_id（含已完成進度）
                    new_steps.append(s.model_copy(deep=True))

            new_plan = Plan(
                plan_id=old_plan.plan_id,
                plan_version=old_plan.plan_version + 1,
                based_on_revision=ctx.expected_revision,
                change_reason=f"依回饋調整：{user_message}",
                assignments=new_assignments,
                steps=new_steps,
                pending_confirmations=[f"尚未確認{alt_region.name}是否放得下。"],
            )

            save_res = toolkit.save_plan(new_plan)
            if not save_res["ok"]:
                return AgentResult(
                    request_id=ctx.request_id,
                    session_id=ctx.session_id,
                    revision=ctx.expected_revision,
                    status=AgentStatus.FAILED,
                    message="重新規劃保存失敗。",
                    error=Error.model_validate(save_res["error"]),
                )

            saved_plan = Plan.model_validate(save_res["data"]["plan"])
            return AgentResult(
                request_id=ctx.request_id,
                session_id=ctx.session_id,
                revision=ctx.expected_revision,
                status=AgentStatus.PLAN_READY,
                message=f"已更新計畫（第 {saved_plan.plan_version} 版），並保留您已完成的進度。",
                plan=saved_plan,
            )

        # 情境 C：若尚未有計畫，建立第一版；否則一般回覆
        if state.current_plan is None:
            return self.create_plan(toolkit, state.scene, user_message)

        return AgentResult(
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            revision=ctx.expected_revision,
            status=AgentStatus.ANSWERED,
            message=f"已收到您的訊息：「{user_message}」。如需調整收納位置或查詢物品位置，請直接告訴我。",
        )


# ==============================================================================
# 4. FastAPI 應用程式初始化與全域例外處理
# ==============================================================================

app = FastAPI(
    title="Help Me Tidy API",
    description="空間魔法師：多模態 AI 物品整理與定位助手後端 API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

storage = MemoryStorage()
http_idempotency = HttpIdempotencyStore()
agent: AgentModuleProtocol = MockAgent()


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """將 FastAPI 參數驗證錯誤轉成規格書要求的 400 INVALID_REQUEST 格式。"""
    req_id = "unknown"
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("request_id"), str):
            req_id = body["request_id"]
    except Exception:
        pass
    return http_error(
        request_id=req_id,
        code="INVALID_REQUEST",
        message="請求欄位缺少或格式錯誤。",
        details={"errors": exc.errors()},
        status_code=400,
    )


# ==============================================================================
# 5. 前端 10 條核心 API 路由（Base path: /api/v1）
# ==============================================================================


@app.post("/api/v1/sessions", status_code=201, response_model=CreateSessionResponse)
def create_session(req: CreateSessionRequest) -> Any:
    """1. 開始一次整理對話"""
    res = storage.create_session(request_id=req.request_id)
    if not res.ok:
        return envelope_error_to_http(req.request_id, res)
    state: SessionState = res.data
    return CreateSessionResponse(
        request_id=req.request_id,
        session_id=state.session_id,
        revision=state.revision,
    )


@app.post(
    "/api/v1/sessions/{sid}/images",
    status_code=201,
    response_model=UploadImagesResponse,
)
async def upload_images(
    sid: str,
    request_id: str = Form(...),
    expected_revision: int = Form(..., ge=0),
    files: List[UploadFile] = File(...),
) -> Any:
    """2. 上傳供本次 session 分析的圖片（multipart）"""
    parsed_files: List[Dict[str, Any]] = []
    for f in files:
        content = await f.read()
        real_mime = detect_real_image_mime(content)
        if real_mime is None:
            return http_error(
                request_id=request_id,
                code="UNSUPPORTED_IMAGE_TYPE",
                message=f"檔案 '{f.filename}' 不是有效的 JPEG 或 PNG 圖片。",
            )
        parsed_files.append({"mime_type": real_mime, "content": content})

    res = storage.store_images(
        session_id=sid,
        request_id=request_id,
        expected_revision=expected_revision,
        files=parsed_files,
    )
    if not res.ok:
        return envelope_error_to_http(request_id, res)
    return UploadImagesResponse(
        request_id=request_id,
        session_id=sid,
        revision=res.revision,
        images=res.data,
    )


@app.post("/api/v1/sessions/{sid}/analysis", response_model=SceneResponse)
def analyze_scene(sid: str, req: AnalyzeSceneRequest) -> Any:
    """3. 分析指定圖片及原始需求，取得待確認辨識結果"""
    payload = {"route": "analysis", "sid": sid, "body": req.model_dump(mode="json")}
    cached, p_hash = http_idempotency.begin(req.request_id, payload)
    if cached is not None:
        return cached

    try:
        state_res = storage.get_session_state(sid)
        if not state_res.ok:
            http_idempotency.abort(req.request_id)
            return envelope_error_to_http(req.request_id, state_res)

        state: SessionState = state_res.data
        if state.revision != req.expected_revision:
            http_idempotency.abort(req.request_id)
            return http_error(
                req.request_id,
                "REVISION_CONFLICT",
                "資料已有更新，請重新載入。",
                details={"current_revision": state.revision},
            )
        if state.current_plan is not None:
            http_idempotency.abort(req.request_id)
            return http_error(
                req.request_id,
                "PLAN_ALREADY_EXISTS",
                "已建立計畫，不可再重新執行初始分析。",
            )

        img_map = {img.image_id: img for img in state.images}
        missing = [iid for iid in req.image_ids if iid not in img_map]
        if missing:
            http_idempotency.abort(req.request_id)
            return http_error(
                req.request_id,
                "INVALID_REFERENCE",
                f"圖片 ID 不存在或不屬於此 session：{missing}",
            )
        selected_images = [img_map[iid] for iid in req.image_ids]

        # 先保存使用者文字
        msg_res = storage.append_message(
            session_id=sid,
            request_id=req.request_id,
            expected_revision=req.expected_revision,
            role=MessageRole.USER,
            text=req.user_message,
            operation_key=f"{req.request_id}:user_msg",
        )
        if not msg_res.ok:
            http_idempotency.abort(req.request_id)
            return envelope_error_to_http(req.request_id, msg_res)
        cur_rev = msg_res.revision

        # 呼叫 Agent 分析場景（分析失敗回 503，不回 AgentResult）
        ctx = AgentContext(
            session_id=sid,
            request_id=req.request_id,
            expected_revision=cur_rev,
            id_allocator=storage.id_allocator,
        )
        try:
            analyzed_scene = agent.analyze_scene(ctx, selected_images, req.user_message)
        except Exception:
            http_idempotency.abort(req.request_id)
            return http_error(
                req.request_id,
                "SERVICE_UNAVAILABLE",
                "照片分析服務暫時無法回應，請稍後重試。",
                retryable=True,
                status_code=503,
            )

        save_res = storage.save_scene(
            session_id=sid,
            request_id=req.request_id,
            expected_revision=cur_rev,
            scene=analyzed_scene,
        )
        if not save_res.ok:
            http_idempotency.abort(req.request_id)
            return envelope_error_to_http(req.request_id, save_res)
        cur_rev = save_res.revision
        saved_scene: SceneAnalysis = save_res.data

        if saved_scene.questions:
            q_res = storage.set_pending_questions(
                session_id=sid,
                request_id=req.request_id,
                expected_revision=cur_rev,
                questions=saved_scene.questions,
                operation_key=f"{req.request_id}:pending_q",
            )
            if q_res.ok and q_res.revision is not None:
                cur_rev = q_res.revision

        resp = SceneResponse(
            request_id=req.request_id,
            session_id=sid,
            revision=cur_rev,
            scene=saved_scene,
        )
        resp_dict = resp.model_dump(mode="json")
        http_idempotency.commit(req.request_id, p_hash, 200, resp_dict)
        return resp
    except Exception:
        http_idempotency.abort(req.request_id)
        return http_error(
            req.request_id,
            "SERVICE_UNAVAILABLE",
            "伺服器處理分析請求時發生異常。",
            retryable=True,
        )


@app.put("/api/v1/sessions/{sid}/scene/confirmation", response_model=SceneResponse)
def confirm_scene(sid: str, req: ConfirmSceneRequest) -> Any:
    """4. 確認或修正辨識；保留原有物品與區域 ID"""
    res = storage.confirm_scene(
        session_id=sid,
        request_id=req.request_id,
        expected_revision=req.expected_revision,
        scene=req.scene,
    )
    if not res.ok:
        return envelope_error_to_http(req.request_id, res)
    return SceneResponse(
        request_id=req.request_id,
        session_id=sid,
        revision=res.revision,
        scene=res.data,
    )


@app.post("/api/v1/sessions/{sid}/plans", response_model=AgentResult)
def create_plan(sid: str, req: CreatePlanRequest) -> Any:
    """5. 從已確認現況及需求建立第一版計畫"""
    payload = {"route": "plans", "sid": sid, "body": req.model_dump(mode="json")}
    cached, p_hash = http_idempotency.begin(req.request_id, payload)
    if cached is not None:
        return cached

    try:
        state_res = storage.get_session_state(sid)
        if not state_res.ok:
            http_idempotency.abort(req.request_id)
            return envelope_error_to_http(req.request_id, state_res)

        state: SessionState = state_res.data
        if state.revision != req.expected_revision:
            http_idempotency.abort(req.request_id)
            return http_error(
                req.request_id,
                "REVISION_CONFLICT",
                "資料已有更新，請重新載入。",
                details={"current_revision": state.revision},
            )
        if state.scene is None or not state.scene.confirmed:
            http_idempotency.abort(req.request_id)
            return http_error(
                req.request_id,
                "SCENE_NOT_CONFIRMED",
                "場景尚未確認，無法建立計畫。",
            )
        if state.current_plan is not None:
            http_idempotency.abort(req.request_id)
            return http_error(
                req.request_id,
                "PLAN_ALREADY_EXISTS",
                "第一版計畫已存在，後續調整請使用 /messages 接口。",
            )

        msg_res = storage.append_message(
            session_id=sid,
            request_id=req.request_id,
            expected_revision=req.expected_revision,
            role=MessageRole.USER,
            text=req.user_message,
            operation_key=f"{req.request_id}:user_msg",
        )
        if not msg_res.ok:
            http_idempotency.abort(req.request_id)
            return envelope_error_to_http(req.request_id, msg_res)
        cur_rev = msg_res.revision

        ctx = AgentContext(
            session_id=sid,
            request_id=req.request_id,
            expected_revision=cur_rev,
            id_allocator=storage.id_allocator,
        )
        toolkit = AgentToolkit(storage, ctx)

        try:
            result = agent.create_plan(toolkit, state.scene, req.user_message)
        except Exception:
            result = AgentResult(
                request_id=req.request_id,
                session_id=sid,
                revision=ctx.expected_revision,
                status=AgentStatus.FAILED,
                message="規劃服務暫時發生異常，已保留先前進度。",
                error=Error(
                    code="MODEL_UNAVAILABLE",
                    message="Agent 執行發生未預期錯誤。",
                    retryable=True,
                ),
            )

        cur_rev = ctx.expected_revision
        reply_res = storage.append_message(
            session_id=sid,
            request_id=req.request_id,
            expected_revision=cur_rev,
            role=MessageRole.ASSISTANT,
            text=result.message,
            operation_key=f"{req.request_id}:assistant_msg",
        )
        if reply_res.ok and reply_res.revision is not None:
            cur_rev = reply_res.revision

        q_res = storage.set_pending_questions(
            session_id=sid,
            request_id=req.request_id,
            expected_revision=cur_rev,
            questions=result.questions,
            operation_key=f"{req.request_id}:pending_q",
        )
        if q_res.ok and q_res.revision is not None:
            cur_rev = q_res.revision

        final_result = result.model_copy(update={"revision": cur_rev})
        http_idempotency.commit(
            req.request_id, p_hash, 200, final_result.model_dump(mode="json")
        )
        return final_result
    except Exception:
        http_idempotency.abort(req.request_id)
        return http_error(
            req.request_id,
            "SERVICE_UNAVAILABLE",
            "伺服器處理規劃請求時發生異常。",
            retryable=True,
        )


@app.post("/api/v1/sessions/{sid}/messages", response_model=AgentResult)
def send_message(sid: str, req: SendMessageRequest) -> Any:
    """6. 補充回答、修改需求、回報放不下或詢問位置"""
    payload = {"route": "messages", "sid": sid, "body": req.model_dump(mode="json")}
    cached, p_hash = http_idempotency.begin(req.request_id, payload)
    if cached is not None:
        return cached

    try:
        state_res = storage.get_session_state(sid)
        if not state_res.ok:
            http_idempotency.abort(req.request_id)
            return envelope_error_to_http(req.request_id, state_res)

        state: SessionState = state_res.data
        if state.revision != req.expected_revision:
            http_idempotency.abort(req.request_id)
            return http_error(
                req.request_id,
                "REVISION_CONFLICT",
                "資料已有更新，請重新載入。",
                details={"current_revision": state.revision},
            )

        msg_res = storage.append_message(
            session_id=sid,
            request_id=req.request_id,
            expected_revision=req.expected_revision,
            role=MessageRole.USER,
            text=req.user_message,
            operation_key=f"{req.request_id}:user_msg",
        )
        if not msg_res.ok:
            http_idempotency.abort(req.request_id)
            return envelope_error_to_http(req.request_id, msg_res)
        cur_rev = msg_res.revision

        ctx = AgentContext(
            session_id=sid,
            request_id=req.request_id,
            expected_revision=cur_rev,
            id_allocator=storage.id_allocator,
        )
        toolkit = AgentToolkit(storage, ctx)

        try:
            result = agent.handle_message(toolkit, req.user_message)
        except Exception:
            result = AgentResult(
                request_id=req.request_id,
                session_id=sid,
                revision=ctx.expected_revision,
                status=AgentStatus.FAILED,
                message="對話服務暫時發生異常，已保留先前進度。",
                error=Error(
                    code="MODEL_UNAVAILABLE",
                    message="Agent 執行發生未預期錯誤。",
                    retryable=True,
                ),
            )

        cur_rev = ctx.expected_revision
        reply_res = storage.append_message(
            session_id=sid,
            request_id=req.request_id,
            expected_revision=cur_rev,
            role=MessageRole.ASSISTANT,
            text=result.message,
            operation_key=f"{req.request_id}:assistant_msg",
        )
        if reply_res.ok and reply_res.revision is not None:
            cur_rev = reply_res.revision

        q_res = storage.set_pending_questions(
            session_id=sid,
            request_id=req.request_id,
            expected_revision=cur_rev,
            questions=result.questions,
            operation_key=f"{req.request_id}:pending_q",
        )
        if q_res.ok and q_res.revision is not None:
            cur_rev = q_res.revision

        final_result = result.model_copy(update={"revision": cur_rev})
        http_idempotency.commit(
            req.request_id, p_hash, 200, final_result.model_dump(mode="json")
        )
        return final_result
    except Exception:
        http_idempotency.abort(req.request_id)
        return http_error(
            req.request_id,
            "SERVICE_UNAVAILABLE",
            "伺服器處理對話請求時發生異常。",
            retryable=True,
        )


@app.post(
    "/api/v1/sessions/{sid}/steps/{step_id}/completion",
    response_model=CompleteStepResponse,
)
def complete_step(sid: str, step_id: str, req: CompleteStepRequest) -> Any:
    """7. 使用者明確確認步驟完成；同步確認該移動步驟的目的位置"""
    res = storage.complete_step(
        session_id=sid,
        request_id=req.request_id,
        expected_revision=req.expected_revision,
        plan_version=req.plan_version,
        step_id=step_id,
    )
    if not res.ok:
        return envelope_error_to_http(req.request_id, res)
    return CompleteStepResponse(
        request_id=req.request_id,
        session_id=sid,
        revision=res.revision,
        step=res.data["step"],
        locations=res.data["locations"],
    )


@app.post(
    "/api/v1/sessions/{sid}/locations/confirmations",
    response_model=ConfirmLocationResponse,
)
def confirm_item_location(sid: str, req: ConfirmLocationRequest) -> Any:
    """8. 不透過計畫，直接確認物品目前位置"""
    res = storage.confirm_item_location(
        session_id=sid,
        request_id=req.request_id,
        expected_revision=req.expected_revision,
        item_id=req.item_id,
        region_id=req.region_id,
    )
    if not res.ok:
        return envelope_error_to_http(req.request_id, res)
    return ConfirmLocationResponse(
        request_id=req.request_id,
        session_id=sid,
        revision=res.revision,
        location=res.data,
    )


@app.get("/api/v1/sessions/{sid}", response_model=SessionState)
def get_session(sid: str) -> Any:
    """9. 重新載入、斷線後恢復畫面"""
    res = storage.get_session_state(sid)
    if not res.ok:
        return envelope_error_to_http("get_session", res)
    return res.data


@app.get(
    "/api/v1/sessions/{sid}/items/{item_id}/location",
    response_model=ItemLocationResponse,
)
def get_item_location(sid: str, item_id: str) -> Any:
    """10. 精確查詢一件物品最後確認的位置"""
    res = storage.lookup_item_location(sid, item_id)
    if not res.ok:
        return envelope_error_to_http("get_item_location", res)
    return ItemLocationResponse(
        session_id=sid,
        revision=res.revision,
        item_id=res.data.item_id,
        location=res.data.location,
        suggested_region_id=res.data.suggested_region_id,
    )