"""
test_schemas.py - 驗證 schemas.py 是否符合 BACKEND_API.md 與 FRONTEND_API.md 規格
執行方式：在終端機輸入 python test_schemas.py
"""

import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from schemas import (
    AgentResult,
    AgentStatus,
    CompleteStepRequest,
    Dimensions,
    Error,
    HttpErrorResponse,
    LocationRecord,
    LocationSource,
    Plan,
    PlanStep,
    RegionAvailability,
    RegionUpdate,
    Requirement,
    RequirementKind,
    RequirementRule,
    SceneAnalysis,
    SessionState,
    StepStatus,
    ToolEnvelope,
)


class TestSchemas(unittest.TestCase):
    # ------------------------------------------------------------------
    # 1. 測試新 Session 初始狀態與版本號規則
    # ------------------------------------------------------------------
    def test_initial_session_state(self):
        session = SessionState(session_id="ses_01")
        self.assertEqual(session.revision, 0)
        self.assertIsNone(session.scene)
        self.assertIsNone(session.current_plan)
        self.assertEqual(session.images, [])
        self.assertEqual(session.locations, [])
        self.assertEqual(session.pending_questions, [])
        self.assertEqual(session.messages, [])
        self.assertEqual(session.requirements.entries, [])
        self.assertEqual(session.requirements.questions, [])

        # 版本號不可為負數
        with self.assertRaises(ValidationError):
            SessionState(session_id="ses_01", revision=-1)

    # ------------------------------------------------------------------
    # 2. 測試 Dimensions（允許 null，有值時須嚴格大於 0）
    # ------------------------------------------------------------------
    def test_dimensions_validation(self):
        unknown_dim = Dimensions()
        self.assertIsNone(unknown_dim.width_cm)

        valid_dim = Dimensions(width_cm=30.5, depth_cm=20, height_cm=15)
        self.assertEqual(valid_dim.width_cm, 30.5)

        with self.assertRaises(ValidationError):
            Dimensions(width_cm=0)
        with self.assertRaises(ValidationError):
            Dimensions(depth_cm=-5)

    # ------------------------------------------------------------------
    # 3. 測試 BACKEND_API.md 的 SceneAnalysis 官方範例
    # ------------------------------------------------------------------
    def test_scene_analysis_spec_example(self):
        raw_scene = {
            "image_ids": ["img_01"],
            "items": [
                {
                    "item_id": "item_01",
                    "name": "課本",
                    "observed_region_id": "region_01",
                    "observation": "桌面上的書本",
                    "needs_confirmation": False,
                }
            ],
            "regions": [
                {
                    "region_id": "region_01",
                    "name": "桌面",
                    "availability": "available",
                    "dimensions": {
                        "width_cm": None,
                        "depth_cm": None,
                        "height_cm": None,
                    },
                    "needs_confirmation": False,
                },
                {
                    "region_id": "region_02",
                    "name": "書架中層",
                    "availability": "unknown",
                    "dimensions": {
                        "width_cm": None,
                        "depth_cm": None,
                        "height_cm": None,
                    },
                    "needs_confirmation": True,
                },
            ],
            "relations": [
                {"item_id": "item_01", "region_id": "region_01", "relation": "on"}
            ],
            "questions": [
                {
                    "question_id": "q_01",
                    "text": "書架中層可以使用嗎？",
                    "related_ids": ["region_02"],
                }
            ],
            "confirmed": False,
        }
        scene = SceneAnalysis.model_validate(raw_scene)
        self.assertFalse(scene.confirmed)
        self.assertEqual(scene.regions[1].availability, RegionAvailability.UNKNOWN)

    # ------------------------------------------------------------------
    # 4. 測試 Requirement 規則驗證
    # ------------------------------------------------------------------
    def test_requirement_rules(self):
        # 正確的 required_region（只指定一個目標 region）
        req = Requirement(
            requirement_id="req_01",
            kind=RequirementKind.HARD_CONSTRAINT,
            rule=RequirementRule.REQUIRED_REGION,
            item_ids=["item_01"],
            region_ids=["region_02"],
            source_text="課本要放書架中層",
        )
        self.assertEqual(req.region_ids, ["region_02"])

        # 錯誤：required_region 指定多個目標 region
        with self.assertRaises(ValidationError):
            Requirement(
                requirement_id="req_02",
                kind=RequirementKind.HARD_CONSTRAINT,
                rule=RequirementRule.REQUIRED_REGION,
                item_ids=["item_01"],
                region_ids=["region_01", "region_02"],
                source_text="課本放這兩個地方",
            )

        # 錯誤：keep_region_empty 的 item_ids 不為空
        with self.assertRaises(ValidationError):
            Requirement(
                requirement_id="req_03",
                kind=RequirementKind.HARD_CONSTRAINT,
                rule=RequirementRule.KEEP_REGION_EMPTY,
                item_ids=["item_01"],
                region_ids=["region_01"],
                source_text="桌面清空",
            )

    # ------------------------------------------------------------------
    # 5. 測試 PlanStep 與帶時區時間驗證
    # ------------------------------------------------------------------
    def test_plan_step_and_timezone(self):
        # pending 狀態不可有 completed_at
        with self.assertRaises(ValidationError):
            PlanStep(
                step_id="step_01",
                instruction="移動課本",
                item_ids=["item_01"],
                target_region_id="region_02",
                status=StepStatus.PENDING,
                completed_at=datetime.now(timezone.utc),
            )

        # completed 狀態必須有 completed_at
        with self.assertRaises(ValidationError):
            PlanStep(
                step_id="step_01",
                instruction="移動課本",
                item_ids=["item_01"],
                target_region_id="region_02",
                status=StepStatus.COMPLETED,
                completed_at=None,
            )

        # 時間必須帶時區（不帶時區的 ISO 字串應失敗）
        with self.assertRaises(ValidationError):
            PlanStep(
                step_id="step_01",
                instruction="移動課本",
                item_ids=["item_01"],
                target_region_id="region_02",
                status=StepStatus.COMPLETED,
                completed_at="2026-11-20T10:00:00",
            )

        # 正確的 completed 步驟
        step = PlanStep(
            step_id="step_01",
            instruction="移動課本",
            item_ids=["item_01"],
            target_region_id="region_02",
            status=StepStatus.COMPLETED,
            completed_at="2026-11-20T10:00:00+08:00",
        )
        self.assertEqual(step.status, StepStatus.COMPLETED)

    # ------------------------------------------------------------------
    # 6. 測試 LocationRecord（區分步驟完成與直接確認）
    # ------------------------------------------------------------------
    def test_location_record_rules(self):
        # 直接確認位置時 step_id 必須為 None
        loc = LocationRecord(
            item_id="item_01",
            region_id="region_02",
            confirmed_at="2026-11-20T10:00:00+08:00",
            source=LocationSource.USER_CONFIRMATION,
            step_id=None,
        )
        self.assertIsNone(loc.step_id)

        with self.assertRaises(ValidationError):
            LocationRecord(
                item_id="item_01",
                region_id="region_02",
                confirmed_at="2026-11-20T10:00:00+08:00",
                source=LocationSource.USER_CONFIRMATION,
                step_id="step_01",
            )

        # 步驟完成觸發時 step_id 不可為空
        with self.assertRaises(ValidationError):
            LocationRecord(
                item_id="item_01",
                region_id="region_02",
                confirmed_at="2026-11-20T10:00:00+08:00",
                source=LocationSource.STEP_COMPLETION,
                step_id=None,
            )

    # ------------------------------------------------------------------
    # 7. 測試 FRONTEND_API.md 的四種 AgentResult 官方範例與契約防呆
    # ------------------------------------------------------------------
    def test_agent_result_spec_examples(self):
        # needs_input
        res_input = AgentResult.model_validate(
            {
                "request_id": "req_01",
                "session_id": "ses_01",
                "revision": 3,
                "status": "needs_input",
                "message": "請先補充抽屜的可用狀態。",
                "questions": [
                    {
                        "question_id": "q_01",
                        "text": "第一層抽屜目前有空間嗎？",
                        "related_ids": ["region_01"],
                    }
                ],
                "plan": None,
                "location": None,
                "error": None,
            }
        )
        self.assertEqual(res_input.status, AgentStatus.NEEDS_INPUT)

        # plan_ready
        res_plan = AgentResult.model_validate(
            {
                "request_id": "req_02",
                "session_id": "ses_01",
                "revision": 4,
                "status": "plan_ready",
                "message": "先將課本移到書架中層。",
                "questions": [],
                "plan": {
                    "plan_id": "plan_01",
                    "plan_version": 1,
                    "based_on_revision": 3,
                    "change_reason": "初次規劃",
                    "assignments": [
                        {
                            "item_id": "item_01",
                            "region_id": "region_02",
                            "reason": "方便取用",
                            "fit_status": "unknown",
                        }
                    ],
                    "steps": [
                        {
                            "step_id": "step_01",
                            "instruction": "將課本移到書架中層",
                            "item_ids": ["item_01"],
                            "target_region_id": "region_02",
                            "status": "pending",
                            "completed_at": None,
                        }
                    ],
                    "pending_confirmations": ["尚未確認書架中層是否放得下課本。"],
                },
                "location": None,
                "error": None,
            }
        )
        self.assertEqual(res_plan.plan.plan_version, 1)

        # answered
        res_ans = AgentResult.model_validate(
            {
                "request_id": "req_03",
                "session_id": "ses_01",
                "revision": 5,
                "status": "answered",
                "message": "課本最後確認放在書架中層。",
                "questions": [],
                "plan": None,
                "location": {
                    "item_id": "item_01",
                    "region_id": "region_02",
                    "confirmed_at": "2026-11-20T10:00:00+08:00",
                    "source": "step_completion",
                    "step_id": "step_01",
                },
                "error": None,
            }
        )
        self.assertEqual(res_ans.location.region_id, "region_02")

        # failed
        res_fail = AgentResult.model_validate(
            {
                "request_id": "req_04",
                "session_id": "ses_01",
                "revision": 5,
                "status": "failed",
                "message": "照片分析服務暫時無法回應，已保留進度。",
                "questions": [],
                "plan": None,
                "location": None,
                "error": {
                    "code": "MODEL_TIMEOUT",
                    "message": "已達本輪執行時限。",
                    "retryable": True,
                    "details": {},
                },
            }
        )
        self.assertTrue(res_fail.error.retryable)

    def test_agent_result_invalid_contracts(self):
        # needs_input 但沒有問題 -> 應失敗
        with self.assertRaises(ValidationError):
            AgentResult(
                request_id="req_01",
                session_id="ses_01",
                revision=1,
                status=AgentStatus.NEEDS_INPUT,
                message="需要輸入",
                questions=[],
            )

        # plan_ready 但 plan 為 None -> 應失敗
        with self.assertRaises(ValidationError):
            AgentResult(
                request_id="req_02",
                session_id="ses_01",
                revision=1,
                status=AgentStatus.PLAN_READY,
                message="計畫好了",
                plan=None,
            )

        # failed 但 error 為 None -> 應失敗
        with self.assertRaises(ValidationError):
            AgentResult(
                request_id="req_03",
                session_id="ses_01",
                revision=1,
                status=AgentStatus.FAILED,
                message="失敗了",
                error=None,
            )

    # ------------------------------------------------------------------
    # 8. 測試 ToolEnvelope、RegionUpdate 與 HTTP 錯誤外殼
    # ------------------------------------------------------------------
    def test_tool_envelope_and_helpers(self):
        # session 不存在時，失敗外殼允許 revision 為 None
        err_env = ToolEnvelope[dict](
            ok=False,
            data=None,
            error=Error(
                code="SESSION_NOT_FOUND",
                message="找不到 session",
                retryable=False,
                details={},
            ),
            revision=None,
        )
        self.assertFalse(err_env.ok)

        # ok=True 但帶了 error -> 應失敗
        with self.assertRaises(ValidationError):
            ToolEnvelope[dict](
                ok=True,
                data={},
                error=Error(code="ERR", message="不該有錯", retryable=False),
                revision=1,
            )

        # RegionUpdate 至少要給一個欄位
        with self.assertRaises(ValidationError):
            RegionUpdate(region_id="region_01")

        # CompleteStepRequest 的 plan_version 須 >= 1
        with self.assertRaises(ValidationError):
            CompleteStepRequest(
                request_id="req_01", expected_revision=1, plan_version=0
            )

        # HTTP 錯誤格式驗證
        http_err = HttpErrorResponse.model_validate(
            {
                "request_id": "req_05",
                "error": {
                    "code": "REVISION_CONFLICT",
                    "message": "資料已有更新，請重新載入。",
                    "retryable": False,
                    "details": {"current_revision": 6},
                },
            }
        )
        self.assertEqual(http_err.error.details["current_revision"], 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)