"""
tools.py - Help Me Tidy（空間魔法師）Agent 專用工具執行層
嚴格遵循 BACKEND_API.md 第 4、5、6 節規範：
1. 僅暴露 6 個允許 Agent 呼叫的工具，隱藏後端可信服務接口。
2. 由 AgentContext 注入 session_id、expected_revision 與穩定生成的 operation_id，禁止模型越權。
3. 統一捕捉參數格式錯誤（ValidationError），永遠回傳規格書定義的 JSON-serializable envelope。
4. 自動更新寫入後的 expected_revision，並強制控管每輪最多 10 次工具呼叫與執行期限（120 秒）。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Union

from pydantic import ValidationError

from schemas import Error, Plan, RegionUpdate, Requirements, ToolEnvelope
from storage import IDAllocator, MemoryStorage

MAX_TOOL_CALLS_PER_TURN = 10
DEFAULT_TURN_TIMEOUT_SECONDS = 120


@dataclass
class AgentContext:
    """
    由 HTTP 整合層在每輪 Agent 執行前建立並注入，
    不交由 LLM 決定 session_id、request_id、expected_revision 或 deadline。
    """

    session_id: str
    request_id: str
    expected_revision: int
    id_allocator: IDAllocator
    deadline: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
        + timedelta(seconds=DEFAULT_TURN_TIMEOUT_SECONDS)
    )
    max_tool_calls: int = MAX_TOOL_CALLS_PER_TURN
    tool_calls_used: int = 0
    _write_seq: int = 0
    _last_operation_id: Optional[str] = None

    def next_operation_id(self, retry_last: bool = False) -> str:
        """從 request_id 與操作序號穩定產生 operation_id；重試時沿用同一 ID。"""
        if retry_last and self._last_operation_id is not None:
            return self._last_operation_id
        self._write_seq += 1
        self._last_operation_id = f"{self.request_id}:op_{self._write_seq}"
        return self._last_operation_id


class AgentToolkit:
    """
    提供組員 B（Agent 模組）使用的 6 個專用工具包裝器。
    所有工具皆回傳 dict 格式：{"ok": bool, "data": ..., "error": ..., "revision": int | None}
    """

    def __init__(self, storage: MemoryStorage, context: AgentContext) -> None:
        self._storage = storage
        self.context = context

    # --------------------------------------------------------------------------
    # 內部輔助：額度／期限檢查、統一格式輸出
    # --------------------------------------------------------------------------

    def _current_known_revision(self) -> Optional[int]:
        state = self._storage.get_session_state(self.context.session_id)
        return state.revision

    def _err_dict(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        envelope = ToolEnvelope[Any](
            ok=False,
            data=None,
            error=Error(
                code=code,
                message=message,
                retryable=retryable,
                details=details or {},
            ),
            revision=self._current_known_revision(),
        )
        return envelope.model_dump(mode="json")

    def _check_execution_limits(self) -> Optional[Dict[str, Any]]:
        if datetime.now(timezone.utc) > self.context.deadline:
            return self._err_dict(
                code="MODEL_TIMEOUT",
                message="已達本輪執行時限，無法繼續呼叫工具。",
                retryable=True,
            )
        if self.context.tool_calls_used >= self.context.max_tool_calls:
            return self._err_dict(
                code="EXECUTION_LIMIT",
                message=f"已達本輪工具呼叫上限（{self.context.max_tool_calls} 次）。",
                retryable=False,
            )
        self.context.tool_calls_used += 1
        return None

    def _finalize(
        self, envelope: ToolEnvelope[Any], is_write: bool = False
    ) -> Dict[str, Any]:
        """寫入成功時自動同步 context.expected_revision，並轉為 JSON 相容字典。"""
        if is_write and envelope.ok and envelope.revision is not None:
            self.context.expected_revision = envelope.revision
        return envelope.model_dump(mode="json")

    # ==========================================================================
    # 6 個 Agent 專用工具（BACKEND_API.md 第 5 節）
    # ==========================================================================

    def get_session_state(self) -> Dict[str, Any]:
        """1. 讀取當前 session 完整狀態（唯讀，不增加 revision）。"""
        limit_err = self._check_execution_limits()
        if limit_err:
            return limit_err

        res = self._storage.get_session_state(self.context.session_id)
        return self._finalize(res, is_write=False)

    def update_requirements(
        self,
        requirements: Union[Requirements, Dict[str, Any]],
        retry_last: bool = False,
    ) -> Dict[str, Any]:
        """2. 以完整集合更新使用者需求（含硬限制、偏好、使用頻率與待確認問題）。"""
        limit_err = self._check_execution_limits()
        if limit_err:
            return limit_err

        try:
            parsed = (
                requirements
                if isinstance(requirements, Requirements)
                else Requirements.model_validate(requirements)
            )
        except ValidationError as exc:
            return self._err_dict(
                code="INVALID_REQUEST",
                message="requirements 資料格式不符規格。",
                details={"errors": exc.errors()},
            )

        op_id = self.context.next_operation_id(retry_last=retry_last)
        res = self._storage.update_requirements(
            session_id=self.context.session_id,
            operation_id=op_id,
            expected_revision=self.context.expected_revision,
            requirements=parsed,
        )
        return self._finalize(res, is_write=True)

    def validate_plan(
        self, candidate: Union[Plan, Dict[str, Any]]
    ) -> Dict[str, Any]:
        """3. 唯讀驗證候選計畫（規則違反回 ok=True, valid=False）。"""
        limit_err = self._check_execution_limits()
        if limit_err:
            return limit_err

        try:
            parsed = (
                candidate
                if isinstance(candidate, Plan)
                else Plan.model_validate(candidate)
            )
        except ValidationError as exc:
            return self._err_dict(
                code="INVALID_REQUEST",
                message="candidate 計畫格式不符規格。",
                details={"errors": exc.errors()},
            )

        res = self._storage.validate_plan(
            session_id=self.context.session_id,
            candidate=parsed,
        )
        return self._finalize(res, is_write=False)

    def save_plan(
        self,
        candidate: Union[Plan, Dict[str, Any]],
        retry_last: bool = False,
    ) -> Dict[str, Any]:
        """4. 在交易內重新驗證並保存下一版計畫（不覆寫舊版、不更新 locations）。"""
        limit_err = self._check_execution_limits()
        if limit_err:
            return limit_err

        try:
            parsed = (
                candidate
                if isinstance(candidate, Plan)
                else Plan.model_validate(candidate)
            )
        except ValidationError as exc:
            return self._err_dict(
                code="INVALID_REQUEST",
                message="candidate 計畫格式不符規格。",
                details={"errors": exc.errors()},
            )

        op_id = self.context.next_operation_id(retry_last=retry_last)
        res = self._storage.save_plan(
            session_id=self.context.session_id,
            operation_id=op_id,
            expected_revision=self.context.expected_revision,
            candidate=parsed,
        )
        return self._finalize(res, is_write=True)

    def lookup_item_location(self, item_id: str) -> Dict[str, Any]:
        """5. 唯讀查詢指定物品的最新確認位置與建議位置（只查本 session）。"""
        limit_err = self._check_execution_limits()
        if limit_err:
            return limit_err

        if not isinstance(item_id, str) or not item_id.strip():
            return self._err_dict(
                code="INVALID_REQUEST",
                message="item_id 必須為非空字串。",
            )

        res = self._storage.lookup_item_location(
            session_id=self.context.session_id,
            item_id=item_id,
        )
        return self._finalize(res, is_write=False)

    def update_regions(
        self,
        updates: List[Union[RegionUpdate, Dict[str, Any]]],
        source_text: str,
        retry_last: bool = False,
    ) -> Dict[str, Any]:
        """6. 保存使用者明確補充的區域可用性或尺寸（單件放不下請改用 update_requirements）。"""
        limit_err = self._check_execution_limits()
        if limit_err:
            return limit_err

        if not isinstance(updates, list) or not updates:
            return self._err_dict(
                code="INVALID_REQUEST",
                message="updates 必須為非空陣列。",
            )
        if not isinstance(source_text, str) or not source_text.strip():
            return self._err_dict(
                code="INVALID_REQUEST",
                message="source_text 不可為空。",
            )

        try:
            parsed_updates = [
                u if isinstance(u, RegionUpdate) else RegionUpdate.model_validate(u)
                for u in updates
            ]
        except ValidationError as exc:
            return self._err_dict(
                code="INVALID_REQUEST",
                message="RegionUpdate 資料格式不符規格。",
                details={"errors": exc.errors()},
            )

        op_id = self.context.next_operation_id(retry_last=retry_last)
        res = self._storage.update_regions(
            session_id=self.context.session_id,
            operation_id=op_id,
            expected_revision=self.context.expected_revision,
            updates=parsed_updates,
            source_text=source_text,
        )
        return self._finalize(res, is_write=True)

    # --------------------------------------------------------------------------
    # 統一工具派發入口（方便組員 B 在 LLM Tool Calling 迴圈中直接依名稱呼叫）
    # --------------------------------------------------------------------------

    def dispatch(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """依工具名稱與模型輸出的參數字典派發執行。"""
        args = arguments or {}
        tool_map: Dict[str, Callable[..., Dict[str, Any]]] = {
            "get_session_state": self.get_session_state,
            "update_requirements": self.update_requirements,
            "validate_plan": self.validate_plan,
            "save_plan": self.save_plan,
            "lookup_item_location": self.lookup_item_location,
            "update_regions": self.update_regions,
        }
        handler = tool_map.get(tool_name)
        if handler is None:
            return self._err_dict(
                code="INVALID_REQUEST",
                message=f"未知或不允許呼叫的工具名稱：{tool_name}",
            )
        try:
            return handler(**args)
        except TypeError as exc:
            return self._err_dict(
                code="INVALID_REQUEST",
                message=f"工具 '{tool_name}' 參數名稱或數量錯誤：{exc}",
            )