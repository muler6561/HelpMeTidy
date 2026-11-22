"""
storage.py - Help Me Tidy（空間魔法師）記憶體儲存層與工具實作
嚴格遵循 BACKEND_API.md 與 FRONTEND_API.md 規範，與 schemas.py 搭配使用。

涵蓋內容：
1. IDAllocator：統一配發 9 種實體 ID
2. 防護機制：冪等去重（深拷貝）、revision 樂觀鎖、單一 session 寫入鎖
3. 6 個 Agent 工具接口（供 Agent 呼叫）
4. 8 個後端可信服務接口（供 FastAPI HTTP 層呼叫）
"""

import copy
import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from schemas import (
    Error,
    FitStatus,
    ImageRef,
    LocationLookupData,
    LocationRecord,
    LocationSource,
    Message,
    MessageRole,
    Plan,
    PlanStep,
    PlanValidationData,
    Question,
    Region,
    RegionAvailability,
    RegionUpdate,
    RequirementRule,
    Requirements,
    SceneAnalysis,
    SessionState,
    StepStatus,
    ToolEnvelope,
    Violation,
)

# 圖片預設限制（FRONTEND_API.md 第 3 節）
MAX_IMAGES_PER_UPLOAD = 5
MIN_IMAGES_PER_UPLOAD = 1
MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB
ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png"}


# ==============================================================================
# 1. ID 配發器（可注入給 AgentContext 共用）
# ==============================================================================


class IDAllocator:
    """統一配發所有正式 ID，禁止由外部或模型任意捏造。"""

    @staticmethod
    def _gen(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:10]}"

    def session_id(self) -> str:
        return self._gen("ses")

    def image_id(self) -> str:
        return self._gen("img")

    def item_id(self) -> str:
        return self._gen("item")

    def region_id(self) -> str:
        return self._gen("reg")

    def requirement_id(self) -> str:
        return self._gen("req")

    def plan_id(self) -> str:
        return self._gen("plan")

    def step_id(self) -> str:
        return self._gen("step")

    def question_id(self) -> str:
        return self._gen("q")

    def message_id(self) -> str:
        return self._gen("msg")


# ==============================================================================
# 2. 記憶體儲存庫與工具層 (MemoryStorage)
# ==============================================================================


class MemoryStorage:
    def __init__(self, id_allocator: Optional[IDAllocator] = None) -> None:
        self.id_allocator = id_allocator or IDAllocator()

        # 主要狀態：{session_id: SessionState}
        self._sessions: Dict[str, SessionState] = {}

        # 計畫歷史版本（不覆寫舊版）：{session_id: [Plan v1, Plan v2, ...]}
        self._plan_history: Dict[str, List[Plan]] = {}

        # 步驟完成事件（與計畫版本分開保存）：{session_id: {step_id: completed_at}}
        self._step_completions: Dict[str, Dict[str, datetime]] = {}

        # 完整位置歷史紀錄（只追加不抹除）：{session_id: [LocationRecord, ...]}
        self._location_history: Dict[str, List[LocationRecord]] = {}

        # 原始圖片位元組（供可信圖片載入器讀取）：{session_id: {image_id: bytes}}
        self._image_blobs: Dict[str, Dict[str, bytes]] = {}

        # 去重快取：{dedup_key: {"payload_hash": str, "status": str, "result": ToolEnvelope}}
        self._idempotency_cache: Dict[str, Dict[str, Any]] = {}

        # 並行控制鎖：保護全域快取與單一 session 寫入
        self._global_lock = threading.Lock()
        self._session_locks: Dict[str, threading.Lock] = {}

    # --------------------------------------------------------------------------
    # 內部輔助方法
    # --------------------------------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _ok(data: Any, revision: int) -> ToolEnvelope[Any]:
        return ToolEnvelope(
            ok=True, data=copy.deepcopy(data), error=None, revision=revision
        )

    @staticmethod
    def _err(
        code: str,
        message: str,
        revision: Optional[int],
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> ToolEnvelope[Any]:
        return ToolEnvelope(
            ok=False,
            data=None,
            error=Error(
                code=code,
                message=message,
                retryable=retryable,
                details=details or {},
            ),
            revision=revision,
        )

    @staticmethod
    def _hash_payload(payload: Any) -> str:
        serialized = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._global_lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def _check_idempotency(
        self, dedup_key: str, payload: Any, current_revision: Optional[int]
    ) -> Tuple[Optional[ToolEnvelope[Any]], str]:
        """
        先查去重紀錄，再做後續檢查。
        回傳 (已存在或衝突的回應 or None, payload_hash)。
        """
        payload_hash = self._hash_payload(payload)
        with self._global_lock:
            entry = self._idempotency_cache.get(dedup_key)
            if entry is not None:
                if entry["payload_hash"] != payload_hash:
                    return (
                        self._err(
                            code="IDEMPOTENCY_CONFLICT",
                            message="相同的請求識別碼被用於不同內容。",
                            revision=current_revision,
                        ),
                        payload_hash,
                    )
                if entry["status"] == "in_progress":
                    return (
                        self._err(
                            code="REQUEST_IN_PROGRESS",
                            message="本次操作仍在執行中，請稍後以原 ID 重試。",
                            revision=current_revision,
                            retryable=True,
                        ),
                        payload_hash,
                    )
                return copy.deepcopy(entry["result"]), payload_hash

            self._idempotency_cache[dedup_key] = {
                "payload_hash": payload_hash,
                "status": "in_progress",
                "result": None,
            }
        return None, payload_hash

    def _finish_idempotency(
        self, dedup_key: str, payload_hash: str, result: ToolEnvelope[Any]
    ) -> ToolEnvelope[Any]:
        """僅快取成功結果；若寫入失敗則清除 in_progress 標記，避免鎖死。"""
        with self._global_lock:
            if result.ok:
                self._idempotency_cache[dedup_key] = {
                    "payload_hash": payload_hash,
                    "status": "completed",
                    "result": copy.deepcopy(result),
                }
            else:
                self._idempotency_cache.pop(dedup_key, None)
        return result

    def _get_known_ids(self, session: SessionState) -> Tuple[Set[str], Set[str]]:
        if not session.scene:
            return set(), set()
        item_ids = {i.item_id for i in session.scene.items}
        region_ids = {r.region_id for r in session.scene.regions}
        return item_ids, region_ids

    def _compute_latest_locations(self, session_id: str) -> List[LocationRecord]:
        """從完整位置歷史中取出每個物品最新一筆確認位置。"""
        latest_by_item: Dict[str, LocationRecord] = {}
        for rec in self._location_history.get(session_id, []):
            latest_by_item[rec.item_id] = rec
        return [copy.deepcopy(r) for r in latest_by_item.values()]

    def _hydrate_plan(self, session_id: str, plan: Optional[Plan]) -> Optional[Plan]:
        """讀取計畫時，將獨立保存的步驟完成事件套用上去。"""
        if plan is None:
            return None
        hydrated = plan.model_copy(deep=True)
        completions = self._step_completions.get(session_id, {})
        for step in hydrated.steps:
            if step.step_id in completions:
                step.status = StepStatus.COMPLETED
                step.completed_at = completions[step.step_id]
        return hydrated

    def _build_session_snapshot(self, session_id: str) -> SessionState:
        session = self._sessions[session_id]
        snapshot = session.model_copy(deep=True)
        snapshot.current_plan = self._hydrate_plan(session_id, session.current_plan)
        snapshot.locations = self._compute_latest_locations(session_id)
        return snapshot

    def _validate_scene_references(
        self, session: SessionState, scene: SceneAnalysis
    ) -> Optional[str]:
        session_img_ids = {img.image_id for img in session.images}
        for img_id in scene.image_ids:
            if img_id not in session_img_ids:
                return f"圖片 ID '{img_id}' 不屬於此 session。"

        item_ids = {i.item_id for i in scene.items}
        region_ids = {r.region_id for r in scene.regions}
        all_entity_ids = item_ids | region_ids | session_img_ids

        if len(item_ids) != len(scene.items):
            return "scene.items 包含重複的 item_id。"
        if len(region_ids) != len(scene.regions):
            return "scene.regions 包含重複的 region_id。"

        for item in scene.items:
            if item.observed_region_id and item.observed_region_id not in region_ids:
                return f"物品 '{item.item_id}' 引用了不存在的區域 '{item.observed_region_id}'。"

        for rel in scene.relations:
            if rel.item_id not in item_ids or rel.region_id not in region_ids:
                return f"空間關係引用了不存在的實體 ({rel.item_id}, {rel.region_id})。"

        for q in scene.questions:
            for rid in q.related_ids:
                if rid not in all_entity_ids:
                    return f"問題 '{q.question_id}' 引用了不存在的實體 '{rid}'。"
        return None

    def _validate_plan_internal(
        self, session: SessionState, candidate: Plan
    ) -> PlanValidationData:
        """計畫驗證核心邏輯（供 validate_plan 與 save_plan 交易內共用）。"""
        violations: List[Violation] = []
        warnings: List[str] = []

        if session.scene is None or not session.scene.confirmed:
            violations.append(
                Violation(
                    code="SCENE_NOT_CONFIRMED",
                    message="場景尚未確認，無法建立或保存計畫。",
                    related_ids=[],
                )
            )
            return PlanValidationData(
                valid=False,
                violations=violations,
                warnings=warnings,
                validated_revision=session.revision,
            )

        item_ids, region_ids = self._get_known_ids(session)
        region_map = {r.region_id: r for r in session.scene.regions}
        history = self._plan_history.get(session.session_id, [])
        completions = self._step_completions.get(session.session_id, {})

        # 1. 檢查 plan_id 與 plan_version
        expected_version = len(history) + 1
        if history and candidate.plan_id != history[0].plan_id:
            violations.append(
                Violation(
                    code="PLAN_ID_MISMATCH",
                    message="同一 session 必須沿用相同的 plan_id。",
                    related_ids=[candidate.plan_id, history[0].plan_id],
                )
            )
        if candidate.plan_version != expected_version:
            violations.append(
                Violation(
                    code="PLAN_VERSION_CONFLICT",
                    message=f"預期的 plan_version 為 {expected_version}，實際收到 {candidate.plan_version}。",
                    related_ids=[candidate.plan_id],
                )
            )

        # 2. 檢查 based_on_revision
        if candidate.based_on_revision != session.revision:
            violations.append(
                Violation(
                    code="REVISION_CONFLICT",
                    message=f"計畫依據版本 ({candidate.based_on_revision}) 與目前版本 ({session.revision}) 不符。",
                    related_ids=[],
                )
            )

        # 3. 檢查 assignments：ID 存在、每物品最多一個配置、不可用區域、容量證據
        assigned_map: Dict[str, str] = {}
        for assign in candidate.assignments:
            if assign.item_id not in item_ids:
                violations.append(
                    Violation(
                        code="INVALID_REFERENCE",
                        message=f"配置引用了不存在的物品 '{assign.item_id}'。",
                        related_ids=[assign.item_id],
                    )
                )
                continue
            if assign.region_id not in region_ids:
                violations.append(
                    Violation(
                        code="INVALID_REFERENCE",
                        message=f"配置引用了不存在的區域 '{assign.region_id}'。",
                        related_ids=[assign.region_id],
                    )
                )
                continue
            if assign.item_id in assigned_map:
                violations.append(
                    Violation(
                        code="DUPLICATE_ASSIGNMENT",
                        message=f"物品 '{assign.item_id}' 不可有多個收納配置。",
                        related_ids=[assign.item_id],
                    )
                )
            assigned_map[assign.item_id] = assign.region_id

            region = region_map[assign.region_id]
            if region.availability == RegionAvailability.UNAVAILABLE:
                violations.append(
                    Violation(
                        code="UNAVAILABLE_REGION",
                        message=f"區域 '{region.name}' ({region.region_id}) 不可使用。",
                        related_ids=[assign.item_id, assign.region_id],
                    )
                )

            has_dimensions = any(
                v is not None
                for v in (
                    region.dimensions.width_cm,
                    region.dimensions.depth_cm,
                    region.dimensions.height_cm,
                )
            )
            if assign.fit_status == FitStatus.CONFIRMED and not has_dimensions:
                violations.append(
                    Violation(
                        code="UNVERIFIED_FIT_STATUS",
                        message=f"區域 '{region.name}' 缺乏尺寸證據，不可將 fit_status 標為 confirmed。",
                        related_ids=[assign.item_id, assign.region_id],
                    )
                )
            if assign.fit_status == FitStatus.UNKNOWN:
                warnings.append(
                    f"物品 '{assign.item_id}' 放入區域 '{region.name}' 的容量尚未確認。"
                )

        # 4. 檢查硬限制（forbidden_region / required_region / keep_region_empty）
        for req in session.requirements.entries:
            if req.rule == RequirementRule.FORBIDDEN_REGION:
                for iid in req.item_ids:
                    if assigned_map.get(iid) in req.region_ids:
                        violations.append(
                            Violation(
                                code="FORBIDDEN_REGION_VIOLATION",
                                message=f"違反禁用區域限制：{req.source_text}",
                                related_ids=[iid, assigned_map[iid], req.requirement_id],
                            )
                        )
            elif req.rule == RequirementRule.REQUIRED_REGION:
                target_reg = req.region_ids[0]
                for iid in req.item_ids:
                    if iid in assigned_map and assigned_map[iid] != target_reg:
                        violations.append(
                            Violation(
                                code="REQUIRED_REGION_VIOLATION",
                                message=f"違反指定區域限制：{req.source_text}",
                                related_ids=[iid, target_reg, req.requirement_id],
                            )
                        )
            elif req.rule == RequirementRule.KEEP_REGION_EMPTY:
                for iid, rid in assigned_map.items():
                    if rid in req.region_ids:
                        violations.append(
                            Violation(
                                code="KEEP_REGION_EMPTY_VIOLATION",
                                message=f"違反保留空區限制：{req.source_text}",
                                related_ids=[iid, rid, req.requirement_id],
                            )
                        )

        # 5. 檢查步驟與配置目標一致，以及已完成步驟不可被改名挪用
        historical_steps: Dict[str, PlanStep] = {}
        for old_plan in history:
            for old_step in old_plan.steps:
                historical_steps[old_step.step_id] = old_step

        seen_step_ids: Set[str] = set()
        for step in candidate.steps:
            if step.step_id in seen_step_ids:
                violations.append(
                    Violation(
                        code="DUPLICATE_STEP_ID",
                        message=f"計畫中包含重複的 step_id '{step.step_id}'。",
                        related_ids=[step.step_id],
                    )
                )
            seen_step_ids.add(step.step_id)

            if step.target_region_id not in region_ids:
                violations.append(
                    Violation(
                        code="INVALID_REFERENCE",
                        message=f"步驟 '{step.step_id}' 引用了不存在的目標區域 '{step.target_region_id}'。",
                        related_ids=[step.step_id, step.target_region_id],
                    )
                )

            for iid in step.item_ids:
                if iid not in item_ids:
                    violations.append(
                        Violation(
                            code="INVALID_REFERENCE",
                            message=f"步驟 '{step.step_id}' 引用了不存在的物品 '{iid}'。",
                            related_ids=[step.step_id, iid],
                        )
                    )
                elif iid in assigned_map and assigned_map[iid] != step.target_region_id:
                    violations.append(
                        Violation(
                            code="STEP_ASSIGNMENT_MISMATCH",
                            message=f"步驟 '{step.step_id}' 的目標區域與物品 '{iid}' 的配置不一致。",
                            related_ids=[step.step_id, iid, step.target_region_id],
                        )
                    )

            # 已完成步驟的保留規則：不得把已完成步驟改換物品或目標當作新任務
            if step.step_id in completions and step.step_id in historical_steps:
                prev = historical_steps[step.step_id]
                if (
                    set(prev.item_ids) != set(step.item_ids)
                    or prev.target_region_id != step.target_region_id
                ):
                    violations.append(
                        Violation(
                            code="COMPLETED_STEP_MUTATED",
                            message=f"已完成步驟 '{step.step_id}' 不可變更物品或目標區域，請配發新的 step_id。",
                            related_ids=[step.step_id],
                        )
                    )

        return PlanValidationData(
            valid=len(violations) == 0,
            violations=violations,
            warnings=warnings,
            validated_revision=session.revision,
        )

    # --------------------------------------------------------------------------
    # 可信圖片讀取輔助（供 Agent analyze_scene 使用）
    # --------------------------------------------------------------------------

    def get_image_bytes(self, session_id: str, image_id: str) -> Optional[bytes]:
        return self._image_blobs.get(session_id, {}).get(image_id)

    def get_location_history(self, session_id: str) -> List[LocationRecord]:
        """取得完整位置歷史（只讀，供測試或稽核用）。"""
        return copy.deepcopy(self._location_history.get(session_id, []))

    # ==========================================================================
    # 3. Agent 工具接口（6 個，BACKEND_API.md 第 5 節）
    # ==========================================================================

    def get_session_state(self, session_id: str) -> ToolEnvelope[SessionState]:
        """唯讀：取得完整 SessionState（不增加 revision）。"""
        if session_id not in self._sessions:
            return self._err(
                code="SESSION_NOT_FOUND",
                message=f"找不到 session '{session_id}'。",
                revision=None,
            )
        snapshot = self._build_session_snapshot(session_id)
        return self._ok(snapshot, snapshot.revision)

    def update_requirements(
        self,
        session_id: str,
        operation_id: str,
        expected_revision: int,
        requirements: Requirements,
    ) -> ToolEnvelope[Dict[str, Requirements]]:
        """以完整集合更新需求，驗證所有實體引用並更新 revision。"""
        session = self._sessions.get(session_id)
        if not session:
            return self._err("SESSION_NOT_FOUND", f"找不到 session '{session_id}'。", None)

        dedup_key = f"op:{session_id}:{operation_id}"
        payload = {"op": "update_requirements", "requirements": requirements.model_dump(mode="json")}
        cached, payload_hash = self._check_idempotency(dedup_key, payload, session.revision)
        if cached is not None:
            return cached

        lock = self._get_session_lock(session_id)
        if not lock.acquire(blocking=False):
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._err("REQUEST_IN_PROGRESS", "目前有其他寫入正在執行。", session.revision, retryable=True),
            )

        try:
            if session.revision != expected_revision:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "REVISION_CONFLICT",
                        "資料已有更新，請重新載入。",
                        session.revision,
                        details={"current_revision": session.revision},
                    ),
                )

            item_ids, region_ids = self._get_known_ids(session)
            all_ids = item_ids | region_ids

            for entry in requirements.entries:
                if any(iid not in item_ids for iid in entry.item_ids):
                    return self._finish_idempotency(
                        dedup_key,
                        payload_hash,
                        self._err("INVALID_REFERENCE", f"需求 '{entry.requirement_id}' 包含不存在的物品 ID。", session.revision),
                    )
                if any(rid not in region_ids for rid in entry.region_ids):
                    return self._finish_idempotency(
                        dedup_key,
                        payload_hash,
                        self._err("INVALID_REFERENCE", f"需求 '{entry.requirement_id}' 包含不存在的區域 ID。", session.revision),
                    )

            for q in requirements.questions:
                if any(rid not in all_ids for rid in q.related_ids):
                    return self._finish_idempotency(
                        dedup_key,
                        payload_hash,
                        self._err("INVALID_REFERENCE", f"需求問題 '{q.question_id}' 包含不存在的實體 ID。", session.revision),
                    )

            session.requirements = requirements.model_copy(deep=True)
            session.revision += 1
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._ok({"requirements": session.requirements}, session.revision),
            )
        finally:
            lock.release()

    def validate_plan(
        self, session_id: str, candidate: Plan
    ) -> ToolEnvelope[PlanValidationData]:
        """唯讀驗證計畫；規則違反時回 ok=True, valid=False。"""
        session = self._sessions.get(session_id)
        if not session:
            return self._err("SESSION_NOT_FOUND", f"找不到 session '{session_id}'。", None)

        validation = self._validate_plan_internal(session, candidate)
        return self._ok(validation, session.revision)

    def save_plan(
        self,
        session_id: str,
        operation_id: str,
        expected_revision: int,
        candidate: Plan,
    ) -> ToolEnvelope[Dict[str, Plan]]:
        """在鎖內重新驗證並保存下一版計畫（不覆寫歷史、不更新 locations）。"""
        session = self._sessions.get(session_id)
        if not session:
            return self._err("SESSION_NOT_FOUND", f"找不到 session '{session_id}'。", None)

        dedup_key = f"op:{session_id}:{operation_id}"
        payload = {"op": "save_plan", "candidate": candidate.model_dump(mode="json")}
        cached, payload_hash = self._check_idempotency(dedup_key, payload, session.revision)
        if cached is not None:
            return cached

        lock = self._get_session_lock(session_id)
        if not lock.acquire(blocking=False):
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._err("REQUEST_IN_PROGRESS", "目前有其他寫入正在執行。", session.revision, retryable=True),
            )

        try:
            if session.revision != expected_revision:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "REVISION_CONFLICT",
                        "資料已有更新，請重新載入。",
                        session.revision,
                        details={"current_revision": session.revision},
                    ),
                )

            expected_version = len(self._plan_history.get(session_id, [])) + 1
            if candidate.plan_version != expected_version:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "PLAN_VERSION_CONFLICT",
                        f"預期的 plan_version 為 {expected_version}，收到 {candidate.plan_version}。",
                        session.revision,
                        details={"expected_plan_version": expected_version},
                    ),
                )

            validation = self._validate_plan_internal(session, candidate)
            if not validation.valid:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "PLAN_INVALID",
                        "計畫未通過規則驗證，無法保存。",
                        session.revision,
                        details={"violations": [v.model_dump() for v in validation.violations]},
                    ),
                )

            saved = candidate.model_copy(deep=True)
            self._plan_history.setdefault(session_id, []).append(saved)
            session.current_plan = saved
            session.revision += 1

            hydrated = self._hydrate_plan(session_id, saved)
            return self._finish_idempotency(
                dedup_key, payload_hash, self._ok({"plan": hydrated}, session.revision)
            )
        finally:
            lock.release()

    def lookup_item_location(
        self, session_id: str, item_id: str
    ) -> ToolEnvelope[LocationLookupData]:
        """唯讀查詢指定物品的最新確認位置與建議位置。"""
        session = self._sessions.get(session_id)
        if not session:
            return self._err("SESSION_NOT_FOUND", f"找不到 session '{session_id}'。", None)

        item_ids, _ = self._get_known_ids(session)
        if item_id not in item_ids:
            return self._err("ITEM_NOT_FOUND", f"找不到物品 '{item_id}'。", session.revision)

        latest_loc: Optional[LocationRecord] = None
        for rec in reversed(self._location_history.get(session_id, [])):
            if rec.item_id == item_id:
                latest_loc = rec.model_copy(deep=True)
                break

        suggested_region_id: Optional[str] = None
        if session.current_plan:
            for assign in session.current_plan.assignments:
                if assign.item_id == item_id:
                    suggested_region_id = assign.region_id
                    break

        data = LocationLookupData(
            item_id=item_id,
            location=latest_loc,
            suggested_region_id=suggested_region_id,
        )
        return self._ok(data, session.revision)

    def update_regions(
        self,
        session_id: str,
        operation_id: str,
        expected_revision: int,
        updates: List[RegionUpdate],
        source_text: str,
    ) -> ToolEnvelope[Dict[str, List[Region]]]:
        """保存使用者明確補充的區域可用性或尺寸（提供 dimensions 時完整替換三個尺寸）。"""
        session = self._sessions.get(session_id)
        if not session:
            return self._err("SESSION_NOT_FOUND", f"找不到 session '{session_id}'。", None)

        dedup_key = f"op:{session_id}:{operation_id}"
        payload = {
            "op": "update_regions",
            "updates": [u.model_dump(mode="json") for u in updates],
            "source_text": source_text,
        }
        cached, payload_hash = self._check_idempotency(dedup_key, payload, session.revision)
        if cached is not None:
            return cached

        lock = self._get_session_lock(session_id)
        if not lock.acquire(blocking=False):
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._err("REQUEST_IN_PROGRESS", "目前有其他寫入正在執行。", session.revision, retryable=True),
            )

        try:
            if session.revision != expected_revision:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "REVISION_CONFLICT",
                        "資料已有更新，請重新載入。",
                        session.revision,
                        details={"current_revision": session.revision},
                    ),
                )

            if not session.scene:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err("INVALID_REFERENCE", "尚未建立場景，無法更新區域。", session.revision),
                )

            region_map = {r.region_id: r for r in session.scene.regions}
            for upd in updates:
                if upd.region_id not in region_map:
                    return self._finish_idempotency(
                        dedup_key,
                        payload_hash,
                        self._err("INVALID_REFERENCE", f"找不到區域 '{upd.region_id}'。", session.revision),
                    )

            for upd in updates:
                target = region_map[upd.region_id]
                if upd.availability is not None:
                    target.availability = upd.availability
                if upd.dimensions is not None:
                    target.dimensions = upd.dimensions.model_copy(deep=True)
                target.needs_confirmation = False

            session.revision += 1
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._ok({"regions": session.scene.regions}, session.revision),
            )
        finally:
            lock.release()

    # ==========================================================================
    # 4. 可信服務接口（8 個，供 HTTP 整合層使用，不暴露給模型）
    # ==========================================================================

    def create_session(self, request_id: str) -> ToolEnvelope[SessionState]:
        """建立全新 session（初始 revision 為 0），支援 request_id 去重。"""
        dedup_key = f"req:{request_id}"
        payload = {"op": "create_session"}
        cached, payload_hash = self._check_idempotency(dedup_key, payload, None)
        if cached is not None:
            return cached

        sid = self.id_allocator.session_id()
        session = SessionState(session_id=sid, revision=0)
        with self._global_lock:
            self._sessions[sid] = session
            self._plan_history[sid] = []
            self._step_completions[sid] = {}
            self._location_history[sid] = []
            self._image_blobs[sid] = {}
            self._session_locks[sid] = threading.Lock()

        return self._finish_idempotency(
            dedup_key, payload_hash, self._ok(session, session.revision)
        )

    def store_images(
        self,
        session_id: str,
        request_id: str,
        expected_revision: int,
        files: List[Dict[str, Any]],
    ) -> ToolEnvelope[List[ImageRef]]:
        """
        驗證圖片數量、MIME 型別與大小，統一配發 image_id 並保存。
        files 每項格式：{"mime_type": str, "content": bytes}（或可選帶 "size_bytes"）。
        """
        session = self._sessions.get(session_id)
        if not session:
            return self._err("SESSION_NOT_FOUND", f"找不到 session '{session_id}'。", None)

        dedup_key = f"req:{request_id}"
        file_fingerprints = [
            {
                "mime_type": f.get("mime_type"),
                "size_bytes": len(f.get("content", b"")),
                "sha256": hashlib.sha256(f.get("content", b"")).hexdigest(),
            }
            for f in files
        ]
        payload = {"op": "store_images", "session_id": session_id, "files": file_fingerprints}
        cached, payload_hash = self._check_idempotency(dedup_key, payload, session.revision)
        if cached is not None:
            return cached

        lock = self._get_session_lock(session_id)
        if not lock.acquire(blocking=False):
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._err("REQUEST_IN_PROGRESS", "目前有其他寫入正在執行。", session.revision, retryable=True),
            )

        try:
            if session.revision != expected_revision:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "REVISION_CONFLICT",
                        "資料已有更新，請重新載入。",
                        session.revision,
                        details={"current_revision": session.revision},
                    ),
                )

            if not (MIN_IMAGES_PER_UPLOAD <= len(files) <= MAX_IMAGES_PER_UPLOAD):
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "INVALID_REQUEST",
                        f"每次上傳圖片數量須介於 {MIN_IMAGES_PER_UPLOAD} 至 {MAX_IMAGES_PER_UPLOAD} 張。",
                        session.revision,
                    ),
                )

            new_refs: List[ImageRef] = []
            new_blobs: Dict[str, bytes] = {}
            for f in files:
                mime_type = f.get("mime_type", "")
                content: bytes = f.get("content", b"")
                size_bytes = len(content)

                if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
                    return self._finish_idempotency(
                        dedup_key,
                        payload_hash,
                        self._err(
                            "UNSUPPORTED_IMAGE_TYPE",
                            f"不支援的圖片格式：{mime_type}（僅接受 JPEG/PNG）。",
                            session.revision,
                        ),
                    )
                if size_bytes <= 0 or size_bytes > MAX_IMAGE_SIZE_BYTES:
                    return self._finish_idempotency(
                        dedup_key,
                        payload_hash,
                        self._err(
                            "IMAGE_TOO_LARGE",
                            f"圖片大小超過上限（最大 {MAX_IMAGE_SIZE_BYTES} bytes）。",
                            session.revision,
                        ),
                    )

                img_id = self.id_allocator.image_id()
                new_refs.append(
                    ImageRef(image_id=img_id, mime_type=mime_type, size_bytes=size_bytes)
                )
                new_blobs[img_id] = content

            session.images.extend(new_refs)
            self._image_blobs[session_id].update(new_blobs)
            session.revision += 1
            return self._finish_idempotency(
                dedup_key, payload_hash, self._ok(new_refs, session.revision)
            )
        finally:
            lock.release()

    def save_scene(
        self,
        session_id: str,
        request_id: str,
        expected_revision: int,
        scene: SceneAnalysis,
    ) -> ToolEnvelope[SceneAnalysis]:
        """保存初始分析結果，confirmed 強制為 False；僅允許於初始計畫前呼叫。"""
        session = self._sessions.get(session_id)
        if not session:
            return self._err("SESSION_NOT_FOUND", f"找不到 session '{session_id}'。", None)

        dedup_key = f"req:{request_id}"
        payload = {"op": "save_scene", "session_id": session_id, "scene": scene.model_dump(mode="json")}
        cached, payload_hash = self._check_idempotency(dedup_key, payload, session.revision)
        if cached is not None:
            return cached

        lock = self._get_session_lock(session_id)
        if not lock.acquire(blocking=False):
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._err("REQUEST_IN_PROGRESS", "目前有其他寫入正在執行。", session.revision, retryable=True),
            )

        try:
            if session.revision != expected_revision:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "REVISION_CONFLICT",
                        "資料已有更新，請重新載入。",
                        session.revision,
                        details={"current_revision": session.revision},
                    ),
                )

            if session.current_plan is not None:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err("PLAN_ALREADY_EXISTS", "已建立計畫，不可再重新保存初始場景。", session.revision),
                )

            ref_err = self._validate_scene_references(session, scene)
            if ref_err:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err("INVALID_REFERENCE", ref_err, session.revision),
                )

            saved_scene = scene.model_copy(deep=True)
            saved_scene.confirmed = False
            session.scene = saved_scene
            session.revision += 1
            return self._finish_idempotency(
                dedup_key, payload_hash, self._ok(saved_scene, session.revision)
            )
        finally:
            lock.release()

    def confirm_scene(
        self,
        session_id: str,
        request_id: str,
        expected_revision: int,
        scene: SceneAnalysis,
    ) -> ToolEnvelope[SceneAnalysis]:
        """使用者確認或修正辨識結果；保留既有 ID，成功後強制設 confirmed=True。"""
        session = self._sessions.get(session_id)
        if not session:
            return self._err("SESSION_NOT_FOUND", f"找不到 session '{session_id}'。", None)

        dedup_key = f"req:{request_id}"
        payload = {"op": "confirm_scene", "session_id": session_id, "scene": scene.model_dump(mode="json")}
        cached, payload_hash = self._check_idempotency(dedup_key, payload, session.revision)
        if cached is not None:
            return cached

        lock = self._get_session_lock(session_id)
        if not lock.acquire(blocking=False):
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._err("REQUEST_IN_PROGRESS", "目前有其他寫入正在執行。", session.revision, retryable=True),
            )

        try:
            if session.revision != expected_revision:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "REVISION_CONFLICT",
                        "資料已有更新，請重新載入。",
                        session.revision,
                        details={"current_revision": session.revision},
                    ),
                )

            if session.current_plan is not None:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err("PLAN_ALREADY_EXISTS", "已建立計畫，不可再修改初始場景。", session.revision),
                )

            if session.scene is None:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err("INVALID_SCENE", "尚未進行照片分析，無法確認場景。", session.revision),
                )

            # v1 規則：確認時不可任意新增、刪除或更換既有物品與區域 ID
            existing_item_ids, existing_region_ids = self._get_known_ids(session)
            incoming_item_ids = {i.item_id for i in scene.items}
            incoming_region_ids = {r.region_id for r in scene.regions}
            if incoming_item_ids != existing_item_ids or incoming_region_ids != existing_region_ids:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "INVALID_SCENE",
                        "確認場景時必須保留原有物品與區域 ID，v1 不支援新增或刪除實體。",
                        session.revision,
                    ),
                )

            ref_err = self._validate_scene_references(session, scene)
            if ref_err:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err("INVALID_REFERENCE", ref_err, session.revision),
                )

            confirmed_scene = scene.model_copy(deep=True)
            confirmed_scene.confirmed = True
            session.scene = confirmed_scene
            session.revision += 1
            return self._finish_idempotency(
                dedup_key, payload_hash, self._ok(confirmed_scene, session.revision)
            )
        finally:
            lock.release()

    def complete_step(
        self,
        session_id: str,
        request_id: str,
        expected_revision: int,
        plan_version: int,
        step_id: str,
    ) -> ToolEnvelope[Dict[str, Any]]:
        """
        標記步驟完成並原子寫入每個物品的位置歷史。
        若步驟已完成，採無副作用成功（不重寫 completed_at，不覆蓋後來的新位置）。
        """
        session = self._sessions.get(session_id)
        if not session:
            return self._err("SESSION_NOT_FOUND", f"找不到 session '{session_id}'。", None)

        dedup_key = f"req:{request_id}"
        payload = {
            "op": "complete_step",
            "session_id": session_id,
            "plan_version": plan_version,
            "step_id": step_id,
        }
        cached, payload_hash = self._check_idempotency(dedup_key, payload, session.revision)
        if cached is not None:
            return cached

        lock = self._get_session_lock(session_id)
        if not lock.acquire(blocking=False):
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._err("REQUEST_IN_PROGRESS", "目前有其他寫入正在執行。", session.revision, retryable=True),
            )

        try:
            if session.revision != expected_revision:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "REVISION_CONFLICT",
                        "資料已有更新，請重新載入。",
                        session.revision,
                        details={"current_revision": session.revision},
                    ),
                )

            if not session.current_plan or session.current_plan.plan_version != plan_version:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "PLAN_VERSION_CONFLICT",
                        "計畫版本不符或目前無生效計畫。",
                        session.revision,
                    ),
                )

            target_step = next(
                (s for s in session.current_plan.steps if s.step_id == step_id), None
            )
            if target_step is None:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err("INVALID_REFERENCE", f"找不到步驟 '{step_id}'。", session.revision),
                )

            completions = self._step_completions[session_id]

            # 若該步驟先前已完成：無副作用成功，不重寫時間、不新增位置、不跳號
            if step_id in completions:
                completed_step = target_step.model_copy(
                    update={"status": StepStatus.COMPLETED, "completed_at": completions[step_id]}
                )
                existing_locs = [
                    rec.model_copy(deep=True)
                    for rec in self._location_history[session_id]
                    if rec.step_id == step_id
                ]
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._ok({"step": completed_step, "locations": existing_locs}, session.revision),
                )

            # 原子寫入完成事件與位置歷史
            now = self._now()
            completions[step_id] = now

            new_locations: List[LocationRecord] = []
            for iid in target_step.item_ids:
                rec = LocationRecord(
                    item_id=iid,
                    region_id=target_step.target_region_id,
                    confirmed_at=now,
                    source=LocationSource.STEP_COMPLETION,
                    step_id=step_id,
                )
                self._location_history[session_id].append(rec)
                new_locations.append(rec)

            session.locations = self._compute_latest_locations(session_id)
            session.revision += 1

            completed_step = target_step.model_copy(
                update={"status": StepStatus.COMPLETED, "completed_at": now}
            )
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._ok({"step": completed_step, "locations": new_locations}, session.revision),
            )
        finally:
            lock.release()

    def confirm_item_location(
        self,
        session_id: str,
        request_id: str,
        expected_revision: int,
        item_id: str,
        region_id: str,
    ) -> ToolEnvelope[LocationRecord]:
        """直接確認物品位置並追加位置歷史（不自動完成任何步驟）。"""
        session = self._sessions.get(session_id)
        if not session:
            return self._err("SESSION_NOT_FOUND", f"找不到 session '{session_id}'。", None)

        dedup_key = f"req:{request_id}"
        payload = {
            "op": "confirm_item_location",
            "session_id": session_id,
            "item_id": item_id,
            "region_id": region_id,
        }
        cached, payload_hash = self._check_idempotency(dedup_key, payload, session.revision)
        if cached is not None:
            return cached

        lock = self._get_session_lock(session_id)
        if not lock.acquire(blocking=False):
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._err("REQUEST_IN_PROGRESS", "目前有其他寫入正在執行。", session.revision, retryable=True),
            )

        try:
            if session.revision != expected_revision:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "REVISION_CONFLICT",
                        "資料已有更新，請重新載入。",
                        session.revision,
                        details={"current_revision": session.revision},
                    ),
                )

            item_ids, region_ids = self._get_known_ids(session)
            if item_id not in item_ids:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err("ITEM_NOT_FOUND", f"找不到物品 '{item_id}'。", session.revision),
                )
            if region_id not in region_ids:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err("INVALID_REFERENCE", f"找不到區域 '{region_id}'。", session.revision),
                )

            rec = LocationRecord(
                item_id=item_id,
                region_id=region_id,
                confirmed_at=self._now(),
                source=LocationSource.USER_CONFIRMATION,
                step_id=None,
            )
            self._location_history[session_id].append(rec)
            session.locations = self._compute_latest_locations(session_id)
            session.revision += 1

            return self._finish_idempotency(
                dedup_key, payload_hash, self._ok(rec, session.revision)
            )
        finally:
            lock.release()

    def append_message(
        self,
        session_id: str,
        request_id: str,
        expected_revision: int,
        role: MessageRole,
        text: str,
        operation_key: Optional[str] = None,
    ) -> ToolEnvelope[Message]:
        """
        保存可見對話訊息。
        若同一 HTTP request_id 需寫入多則訊息（例如使用者訊息 + Agent 回覆），
        可透過 operation_key 區分去重鍵，Message.request_id 仍保留原始 HTTP request_id。
        """
        session = self._sessions.get(session_id)
        if not session:
            return self._err("SESSION_NOT_FOUND", f"找不到 session '{session_id}'。", None)

        dedup_key = f"req:{operation_key or request_id}"
        payload = {
            "op": "append_message",
            "session_id": session_id,
            "role": role.value,
            "text": text,
        }
        cached, payload_hash = self._check_idempotency(dedup_key, payload, session.revision)
        if cached is not None:
            return cached

        lock = self._get_session_lock(session_id)
        if not lock.acquire(blocking=False):
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._err("REQUEST_IN_PROGRESS", "目前有其他寫入正在執行。", session.revision, retryable=True),
            )

        try:
            if session.revision != expected_revision:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "REVISION_CONFLICT",
                        "資料已有更新，請重新載入。",
                        session.revision,
                        details={"current_revision": session.revision},
                    ),
                )

            msg = Message(
                message_id=self.id_allocator.message_id(),
                role=role,
                text=text,
                created_at=self._now(),
                request_id=request_id,
            )
            session.messages.append(msg)
            session.revision += 1
            return self._finish_idempotency(
                dedup_key, payload_hash, self._ok(msg, session.revision)
            )
        finally:
            lock.release()

    def set_pending_questions(
        self,
        session_id: str,
        request_id: str,
        expected_revision: int,
        questions: List[Question],
        operation_key: Optional[str] = None,
    ) -> ToolEnvelope[List[Question]]:
        """替換目前待回答問題集合。"""
        session = self._sessions.get(session_id)
        if not session:
            return self._err("SESSION_NOT_FOUND", f"找不到 session '{session_id}'。", None)

        dedup_key = f"req:{operation_key or request_id}"
        payload = {
            "op": "set_pending_questions",
            "session_id": session_id,
            "questions": [q.model_dump(mode="json") for q in questions],
        }
        cached, payload_hash = self._check_idempotency(dedup_key, payload, session.revision)
        if cached is not None:
            return cached

        lock = self._get_session_lock(session_id)
        if not lock.acquire(blocking=False):
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._err("REQUEST_IN_PROGRESS", "目前有其他寫入正在執行。", session.revision, retryable=True),
            )

        try:
            if session.revision != expected_revision:
                return self._finish_idempotency(
                    dedup_key,
                    payload_hash,
                    self._err(
                        "REVISION_CONFLICT",
                        "資料已有更新，請重新載入。",
                        session.revision,
                        details={"current_revision": session.revision},
                    ),
                )

            item_ids, region_ids = self._get_known_ids(session)
            all_ids = item_ids | region_ids | {img.image_id for img in session.images}
            for q in questions:
                if any(rid not in all_ids for rid in q.related_ids):
                    return self._finish_idempotency(
                        dedup_key,
                        payload_hash,
                        self._err(
                            "INVALID_REFERENCE",
                            f"問題 '{q.question_id}' 引用了不存在的實體 ID。",
                            session.revision,
                        ),
                    )

            session.pending_questions = [q.model_copy(deep=True) for q in questions]
            session.revision += 1
            return self._finish_idempotency(
                dedup_key,
                payload_hash,
                self._ok(session.pending_questions, session.revision),
            )
        finally:
            lock.release()