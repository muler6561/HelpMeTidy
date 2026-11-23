"""
test_tools.py - 驗證 tools.py 的工具執行層、權限隔離與執行上限
執行方式：在 HelpMeTidy/backend/ 目錄下輸入 python test_tools.py
"""

import unittest
from datetime import datetime, timedelta, timezone

from schemas import (
    Dimensions,
    Item,
    Region,
    RegionAvailability,
    SceneAnalysis,
)
from storage import MemoryStorage
from tools import AgentContext, AgentToolkit


class TestAgentToolkit(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage()
        alloc = self.storage.id_allocator

        # 建立已完成場景確認的測試 session（rev = 3）
        self.sid = self.storage.create_session("req_init_01").data.session_id
        img_id = self.storage.store_images(
            self.sid,
            "req_init_02",
            0,
            [{"mime_type": "image/jpeg", "content": b"\xff\xd8\xff\xe0test"}],
        ).data[0].image_id

        self.item_id = alloc.item_id()
        self.reg_desk = alloc.region_id()
        self.reg_shelf = alloc.region_id()

        scene = SceneAnalysis(
            image_ids=[img_id],
            items=[
                Item(
                    item_id=self.item_id,
                    name="課本",
                    observed_region_id=self.reg_desk,
                    observation="桌面上的課本",
                    needs_confirmation=False,
                )
            ],
            regions=[
                Region(
                    region_id=self.reg_desk,
                    name="桌面",
                    availability=RegionAvailability.AVAILABLE,
                    dimensions=Dimensions(),
                    needs_confirmation=False,
                ),
                Region(
                    region_id=self.reg_shelf,
                    name="書架中層",
                    availability=RegionAvailability.UNKNOWN,
                    dimensions=Dimensions(),
                    needs_confirmation=True,
                ),
            ],
            relations=[],
            questions=[],
            confirmed=False,
        )
        saved = self.storage.save_scene(self.sid, "req_init_03", 1, scene).data
        self.storage.confirm_scene(self.sid, "req_init_04", 2, saved)

        self.ctx = AgentContext(
            session_id=self.sid,
            request_id="req_turn_01",
            expected_revision=3,
            id_allocator=alloc,
        )
        self.toolkit = AgentToolkit(self.storage, self.ctx)

    # ------------------------------------------------------------------
    # 1. 測試 6 個工具正常運作，且寫入後自動同步 context.expected_revision
    # ------------------------------------------------------------------
    def test_01_all_six_tools_and_revision_sync(self) -> None:
        alloc = self.storage.id_allocator

        # 1. get_session_state（唯讀，不改 revision）
        res_state = self.toolkit.get_session_state()
        self.assertTrue(res_state["ok"])
        self.assertEqual(res_state["revision"], 3)
        self.assertEqual(self.ctx.expected_revision, 3)

        # 2. update_regions（接受原始 dict，寫入後 expected_revision 自動變為 4）
        res_reg = self.toolkit.update_regions(
            updates=[
                {"region_id": self.reg_shelf, "availability": "available"}
            ],
            source_text="書架中層可以使用",
        )
        self.assertTrue(res_reg["ok"])
        self.assertEqual(res_reg["revision"], 4)
        self.assertEqual(self.ctx.expected_revision, 4)

        # 3. update_requirements（寫入後 expected_revision 自動變為 5）
        res_req = self.toolkit.update_requirements(
            {
                "entries": [
                    {
                        "requirement_id": alloc.requirement_id(),
                        "kind": "hard_constraint",
                        "rule": "required_region",
                        "item_ids": [self.item_id],
                        "region_ids": [self.reg_shelf],
                        "value": None,
                        "source_text": "課本要放書架中層",
                    }
                ],
                "questions": [],
            }
        )
        self.assertTrue(res_req["ok"])
        self.assertEqual(res_req["revision"], 5)
        self.assertEqual(self.ctx.expected_revision, 5)

        # 4. validate_plan（唯讀，不改 revision）
        plan_dict = {
            "plan_id": alloc.plan_id(),
            "plan_version": 1,
            "based_on_revision": 5,
            "change_reason": "初次規劃",
            "assignments": [
                {
                    "item_id": self.item_id,
                    "region_id": self.reg_shelf,
                    "reason": "依需求放書架中層",
                    "fit_status": "unknown",
                }
            ],
            "steps": [
                {
                    "step_id": alloc.step_id(),
                    "instruction": "將課本移到書架中層",
                    "item_ids": [self.item_id],
                    "target_region_id": self.reg_shelf,
                    "status": "pending",
                    "completed_at": None,
                }
            ],
            "pending_confirmations": ["尚未確認書架中層容量"],
        }
        res_val = self.toolkit.validate_plan(plan_dict)
        self.assertTrue(res_val["ok"])
        self.assertTrue(res_val["data"]["valid"])
        self.assertEqual(self.ctx.expected_revision, 5)

        # 5. save_plan（寫入後 expected_revision 自動變為 6）
        res_save = self.toolkit.save_plan(plan_dict)
        self.assertTrue(res_save["ok"])
        self.assertEqual(res_save["revision"], 6)
        self.assertEqual(self.ctx.expected_revision, 6)

        # 6. lookup_item_location（唯讀，建議位置有值、確認位置為 None）
        res_loc = self.toolkit.lookup_item_location(self.item_id)
        self.assertTrue(res_loc["ok"])
        self.assertIsNone(res_loc["data"]["location"])
        self.assertEqual(res_loc["data"]["suggested_region_id"], self.reg_shelf)

    # ------------------------------------------------------------------
    # 2. 測試 retry_last=True 會沿用同一個 operation_id，不重複跳號
    # ------------------------------------------------------------------
    def test_02_retry_last_reuses_operation_id(self) -> None:
        payload = {"entries": [], "questions": []}

        res_first = self.toolkit.update_requirements(payload)
        self.assertTrue(res_first["ok"])
        self.assertEqual(res_first["revision"], 4)

        res_retry = self.toolkit.update_requirements(payload, retry_last=True)
        self.assertTrue(res_retry["ok"])
        self.assertEqual(res_retry["revision"], 4)  # 去重命中，不額外跳號

    # ------------------------------------------------------------------
    # 3. 測試模型傳入錯誤格式時回傳標準錯誤 envelope，不拋出未捕捉例外
    # ------------------------------------------------------------------
    def test_03_invalid_arguments_return_error_envelope(self) -> None:
        res_bad_req = self.toolkit.update_requirements({"entries": "not_a_list"})
        self.assertFalse(res_bad_req["ok"])
        self.assertEqual(res_bad_req["error"]["code"], "INVALID_REQUEST")

        res_empty_item = self.toolkit.lookup_item_location("   ")
        self.assertFalse(res_empty_item["ok"])
        self.assertEqual(res_empty_item["error"]["code"], "INVALID_REQUEST")

        res_empty_upd = self.toolkit.update_regions([], "空更新")
        self.assertFalse(res_empty_upd["ok"])
        self.assertEqual(res_empty_upd["error"]["code"], "INVALID_REQUEST")

    # ------------------------------------------------------------------
    # 4. 測試 dispatch 權限隔離：禁止呼叫可信服務接口或未知工具
    # ------------------------------------------------------------------
    def test_04_dispatch_blocks_privileged_and_unknown_tools(self) -> None:
        # 允許的工具可正常執行
        res_ok = self.toolkit.dispatch("get_session_state")
        self.assertTrue(res_ok["ok"])

        # 可信服務接口不可透過 dispatch 給模型呼叫
        for forbidden in ("create_session", "complete_step", "confirm_item_location"):
            res_block = self.toolkit.dispatch(forbidden)
            self.assertFalse(res_block["ok"])
            self.assertEqual(res_block["error"]["code"], "INVALID_REQUEST")

        # 參數名稱錯誤也應轉成 INVALID_REQUEST
        res_bad_kwarg = self.toolkit.dispatch("lookup_item_location", {"wrong_key": "x"})
        self.assertFalse(res_bad_kwarg["ok"])
        self.assertEqual(res_bad_kwarg["error"]["code"], "INVALID_REQUEST")

    # ------------------------------------------------------------------
    # 5. 測試單輪最多 10 次工具呼叫上限
    # ------------------------------------------------------------------
    def test_05_max_tool_calls_limit(self) -> None:
        for _ in range(10):
            res = self.toolkit.get_session_state()
            self.assertTrue(res["ok"])

        # 第 11 次必須被擋下
        res_11 = self.toolkit.get_session_state()
        self.assertFalse(res_11["ok"])
        self.assertEqual(res_11["error"]["code"], "EXECUTION_LIMIT")
        self.assertFalse(res_11["error"]["retryable"])

    # ------------------------------------------------------------------
    # 6. 測試超過執行期限（deadline）時回傳 MODEL_TIMEOUT
    # ------------------------------------------------------------------
    def test_06_deadline_timeout_enforced(self) -> None:
        expired_ctx = AgentContext(
            session_id=self.sid,
            request_id="req_expired",
            expected_revision=3,
            id_allocator=self.storage.id_allocator,
            deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        expired_toolkit = AgentToolkit(self.storage, expired_ctx)

        res = expired_toolkit.get_session_state()
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "MODEL_TIMEOUT")
        self.assertTrue(res["error"]["retryable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)