"""
test_storage.py - 驗證 storage.py 是否通過 BACKEND_API.md 第 7 節的 7 項後端驗收案例
執行方式：在終端機輸入 python test_storage.py
"""

import unittest
from typing import Dict, Tuple

from schemas import (
    Assignment,
    Dimensions,
    FitStatus,
    Item,
    LocationSource,
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
    SpatialRelation,
    SpatialRelationType,
    StepStatus,
)
from storage import MemoryStorage


class TestMemoryStorageAcceptance(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage()

    # ------------------------------------------------------------------
    # 共用測試前製輔助：建立 session -> 上傳圖片 -> 保存並確認場景 (rev=3)
    # ------------------------------------------------------------------
    def _setup_confirmed_session(
        self, prefix: str = "a"
    ) -> Tuple[str, Dict[str, str], SceneAnalysis, int]:
        alloc = self.storage.id_allocator

        res_sess = self.storage.create_session(request_id=f"{prefix}_req_01")
        self.assertTrue(res_sess.ok)
        sid = res_sess.data.session_id
        self.assertEqual(res_sess.revision, 0)

        res_img = self.storage.store_images(
            session_id=sid,
            request_id=f"{prefix}_req_02",
            expected_revision=0,
            files=[{"mime_type": "image/jpeg", "content": b"\xff\xd8\xff\xe0fakejpeg"}],
        )
        self.assertTrue(res_img.ok)
        img_id = res_img.data[0].image_id
        self.assertEqual(res_img.revision, 1)

        ids = {
            "img_01": img_id,
            "item_01": alloc.item_id(),  # 課本
            "item_02": alloc.item_id(),  # 文具盒
            "reg_01": alloc.region_id(),  # 桌面 (available)
            "reg_02": alloc.region_id(),  # 抽屜 (available)
            "reg_03": alloc.region_id(),  # 書架中層 (unknown)
            "q_01": alloc.question_id(),
            "plan_01": alloc.plan_id(),
        }

        scene = SceneAnalysis(
            image_ids=[img_id],
            items=[
                Item(
                    item_id=ids["item_01"],
                    name="課本",
                    observed_region_id=ids["reg_01"],
                    observation="桌面上的課本",
                    needs_confirmation=False,
                ),
                Item(
                    item_id=ids["item_02"],
                    name="文具盒",
                    observed_region_id=ids["reg_01"],
                    observation="桌面上的文具盒",
                    needs_confirmation=False,
                ),
            ],
            regions=[
                Region(
                    region_id=ids["reg_01"],
                    name="桌面",
                    availability=RegionAvailability.AVAILABLE,
                    dimensions=Dimensions(),
                    needs_confirmation=False,
                ),
                Region(
                    region_id=ids["reg_02"],
                    name="抽屜",
                    availability=RegionAvailability.AVAILABLE,
                    dimensions=Dimensions(),
                    needs_confirmation=False,
                ),
                Region(
                    region_id=ids["reg_03"],
                    name="書架中層",
                    availability=RegionAvailability.UNKNOWN,
                    dimensions=Dimensions(),
                    needs_confirmation=True,
                ),
            ],
            relations=[
                SpatialRelation(
                    item_id=ids["item_01"],
                    region_id=ids["reg_01"],
                    relation=SpatialRelationType.ON,
                )
            ],
            questions=[
                Question(
                    question_id=ids["q_01"],
                    text="書架中層可以使用嗎？",
                    related_ids=[ids["reg_03"]],
                )
            ],
            confirmed=False,
        )

        res_save = self.storage.save_scene(
            session_id=sid,
            request_id=f"{prefix}_req_03",
            expected_revision=1,
            scene=scene,
        )
        self.assertTrue(res_save.ok)
        self.assertFalse(res_save.data.confirmed)
        self.assertEqual(res_save.revision, 2)

        res_conf = self.storage.confirm_scene(
            session_id=sid,
            request_id=f"{prefix}_req_04",
            expected_revision=2,
            scene=res_save.data,
        )
        self.assertTrue(res_conf.ok)
        self.assertTrue(res_conf.data.confirmed)
        self.assertEqual(res_conf.revision, 3)

        return sid, ids, res_conf.data, 3

    # ------------------------------------------------------------------
    # 驗收案例 1：初始 scene 符合型別、未知尺寸保留 null、未確認前不得建計畫
    # ------------------------------------------------------------------
    def test_01_initial_scene_and_unknown_dimensions(self) -> None:
        alloc = self.storage.id_allocator
        sid = self.storage.create_session("req_c1_01").data.session_id
        img_id = self.storage.store_images(
            sid,
            "req_c1_02",
            0,
            [{"mime_type": "image/png", "content": b"\x89PNG\r\n\x1a\nfake"}],
        ).data[0].image_id

        item_id = alloc.item_id()
        reg_id = alloc.region_id()
        scene = SceneAnalysis(
            image_ids=[img_id],
            items=[
                Item(
                    item_id=item_id,
                    name="筆記本",
                    observed_region_id=reg_id,
                    observation="放在書桌上",
                    needs_confirmation=False,
                )
            ],
            regions=[
                Region(
                    region_id=reg_id,
                    name="書桌",
                    availability=RegionAvailability.AVAILABLE,
                    dimensions=Dimensions(),
                    needs_confirmation=False,
                )
            ],
            relations=[],
            questions=[],
            confirmed=True,  # 即使傳入 True，save_scene 也必須強制壓成 False
        )

        res_save = self.storage.save_scene(sid, "req_c1_03", 1, scene)
        self.assertTrue(res_save.ok)
        self.assertFalse(res_save.data.confirmed)

        state = self.storage.get_session_state(sid).data
        dim = state.scene.regions[0].dimensions
        self.assertIsNone(dim.width_cm)
        self.assertIsNone(dim.depth_cm)
        self.assertIsNone(dim.height_cm)

        # 場景尚未確認，validate_plan 應回 valid=False (SCENE_NOT_CONFIRMED)
        draft_plan = Plan(
            plan_id=alloc.plan_id(),
            plan_version=1,
            based_on_revision=2,
            change_reason="測試未確認場景",
            assignments=[],
            steps=[],
            pending_confirmations=[],
        )
        res_val = self.storage.validate_plan(sid, draft_plan)
        self.assertTrue(res_val.ok)
        self.assertFalse(res_val.data.valid)
        self.assertEqual(res_val.data.violations[0].code, "SCENE_NOT_CONFIRMED")

    # ------------------------------------------------------------------
    # 驗收案例 2：改物品名稱後 ID 不變；不存在或跨 session 引用被拒絕
    # ------------------------------------------------------------------
    def test_02_rename_preserves_id_and_rejects_invalid_or_cross_session_refs(self) -> None:
        sid_a, ids_a, scene_a, _ = self._setup_confirmed_session("a")
        sid_b, ids_b, _, _ = self._setup_confirmed_session("b")

        # 1. 在初始計畫前，透過 confirm_scene 改名但保留原 ID
        renamed_scene = scene_a.model_copy(deep=True)
        renamed_scene.items[0].name = "線性代數課本"
        res_rename = self.storage.confirm_scene(sid_a, "a_req_rename", 3, renamed_scene)
        self.assertTrue(res_rename.ok)
        self.assertEqual(res_rename.data.items[0].item_id, ids_a["item_01"])
        self.assertEqual(res_rename.data.items[0].name, "線性代數課本")

        # 2. 嘗試在 confirm_scene 偷換物品 ID -> 被拒絕 (INVALID_SCENE)
        bad_id_scene = renamed_scene.model_copy(deep=True)
        bad_id_scene.items[0].item_id = "item_fake_999"
        res_bad_id = self.storage.confirm_scene(sid_a, "a_req_bad_id", 4, bad_id_scene)
        self.assertFalse(res_bad_id.ok)
        self.assertEqual(res_bad_id.error.code, "INVALID_SCENE")

        # 3. 跨 session 引用（在 Session A 確認 Session B 的物品位置）-> 被拒絕
        res_cross_item = self.storage.confirm_item_location(
            sid_a, "a_req_cross_item", 4, ids_b["item_01"], ids_a["reg_01"]
        )
        self.assertFalse(res_cross_item.ok)
        self.assertEqual(res_cross_item.error.code, "ITEM_NOT_FOUND")

        # 4. 不存在的區域引用 -> 被拒絕 (INVALID_REFERENCE)
        res_bad_reg = self.storage.confirm_item_location(
            sid_a, "a_req_bad_reg", 4, ids_a["item_01"], "reg_does_not_exist"
        )
        self.assertFalse(res_bad_reg.ok)
        self.assertEqual(res_bad_reg.error.code, "INVALID_REFERENCE")

        # 5. 跨 session 引用圖片（在 Session B 使用 Session A 的 image_id）-> 被拒絕
        cross_img_scene = renamed_scene.model_copy(deep=True)
        cross_img_scene.image_ids = [ids_a["img_01"]]
        res_cross_img = self.storage.save_scene(sid_b, "b_req_cross_img", 3, cross_img_scene)
        self.assertFalse(res_cross_img.ok)
        self.assertEqual(res_cross_img.error.code, "INVALID_REFERENCE")

    # ------------------------------------------------------------------
    # 驗收案例 3：重送相同請求不重複新增計畫、完成事件或位置歷史；同 ID 不同內容被拒絕
    # ------------------------------------------------------------------
    def test_03_idempotency_and_conflict(self) -> None:
        sid, ids, _, rev = self._setup_confirmed_session("c3")
        step_1 = self.storage.id_allocator.step_id()

        plan_v1 = Plan(
            plan_id=ids["plan_01"],
            plan_version=1,
            based_on_revision=rev,
            change_reason="初次規劃",
            assignments=[
                Assignment(
                    item_id=ids["item_01"],
                    region_id=ids["reg_02"],
                    reason="收納進抽屜",
                    fit_status=FitStatus.UNKNOWN,
                )
            ],
            steps=[
                PlanStep(
                    step_id=step_1,
                    instruction="將課本放進抽屜",
                    item_ids=[ids["item_01"]],
                    target_region_id=ids["reg_02"],
                    status=StepStatus.PENDING,
                    completed_at=None,
                )
            ],
            pending_confirmations=["尚未確認抽屜是否放得下課本"],
        )

        # 第一次保存計畫 (rev 3 -> 4)
        res_p1 = self.storage.save_plan(sid, "op_plan_01", rev, plan_v1)
        self.assertTrue(res_p1.ok)
        self.assertEqual(res_p1.revision, 4)

        # 重送完全相同的 operation_id 與內容 -> 原樣回傳 rev=4，不重複新增計畫
        res_p1_replay = self.storage.save_plan(sid, "op_plan_01", rev, plan_v1)
        self.assertTrue(res_p1_replay.ok)
        self.assertEqual(res_p1_replay.revision, 4)
        self.assertEqual(len(self.storage._plan_history[sid]), 1)

        # 相同 operation_id 但不同內容 -> IDEMPOTENCY_CONFLICT
        modified_plan = plan_v1.model_copy(update={"change_reason": "偷偷改掉理由"})
        res_p1_conflict = self.storage.save_plan(sid, "op_plan_01", rev, modified_plan)
        self.assertFalse(res_p1_conflict.ok)
        self.assertEqual(res_p1_conflict.error.code, "IDEMPOTENCY_CONFLICT")

        # 第一次完成步驟 (rev 4 -> 5)
        res_comp = self.storage.complete_step(sid, "req_comp_01", 4, 1, step_1)
        self.assertTrue(res_comp.ok)
        self.assertEqual(res_comp.revision, 5)
        first_completed_at = res_comp.data["step"].completed_at

        # 重送相同 request_id -> 原樣回傳，不重複追加位置歷史
        res_comp_replay = self.storage.complete_step(sid, "req_comp_01", 4, 1, step_1)
        self.assertTrue(res_comp_replay.ok)
        self.assertEqual(res_comp_replay.revision, 5)
        self.assertEqual(len(self.storage.get_location_history(sid)), 1)

        # 對已完成步驟用新 request_id 再呼叫 -> 無副作用成功，不改時間、不跳號
        res_comp_noop = self.storage.complete_step(sid, "req_comp_02", 5, 1, step_1)
        self.assertTrue(res_comp_noop.ok)
        self.assertEqual(res_comp_noop.revision, 5)
        self.assertEqual(res_comp_noop.data["step"].completed_at, first_completed_at)
        self.assertEqual(len(self.storage.get_location_history(sid)), 1)

        # 同一個 request_id 但內容不同 -> IDEMPOTENCY_CONFLICT
        res_comp_conflict = self.storage.complete_step(sid, "req_comp_01", 4, 1, "step_other")
        self.assertFalse(res_comp_conflict.ok)
        self.assertEqual(res_comp_conflict.error.code, "IDEMPOTENCY_CONFLICT")

    # ------------------------------------------------------------------
    # 驗收案例 4：舊 revision 或 plan_version 被拒絕；save_plan 交易內再次驗證
    # ------------------------------------------------------------------
    def test_04_stale_revision_plan_version_and_revalidation_in_save_plan(self) -> None:
        sid, ids, _, rev = self._setup_confirmed_session("c4")
        alloc = self.storage.id_allocator
        step_1 = alloc.step_id()

        candidate = Plan(
            plan_id=ids["plan_01"],
            plan_version=1,
            based_on_revision=rev,
            change_reason="初次規劃",
            assignments=[
                Assignment(
                    item_id=ids["item_01"],
                    region_id=ids["reg_02"],
                    reason="放進抽屜",
                    fit_status=FitStatus.UNKNOWN,
                )
            ],
            steps=[
                PlanStep(
                    step_id=step_1,
                    instruction="將課本放進抽屜",
                    item_ids=[ids["item_01"]],
                    target_region_id=ids["reg_02"],
                    status=StepStatus.PENDING,
                    completed_at=None,
                )
            ],
            pending_confirmations=[],
        )

        # 1. 在 rev=3 時驗證通過
        self.assertTrue(self.storage.validate_plan(sid, candidate).data.valid)

        # 2. 模擬競爭寫入：在 save_plan 前，新增「課本不可放抽屜」硬限制 (rev 3 -> 4)
        reqs = Requirements(
            entries=[
                Requirement(
                    requirement_id=alloc.requirement_id(),
                    kind=RequirementKind.HARD_CONSTRAINT,
                    rule=RequirementRule.FORBIDDEN_REGION,
                    item_ids=[ids["item_01"]],
                    region_ids=[ids["reg_02"]],
                    source_text="課本不要放抽屜",
                )
            ]
        )
        res_req = self.storage.update_requirements(sid, "op_req_01", 3, reqs)
        self.assertTrue(res_req.ok)
        self.assertEqual(res_req.revision, 4)

        # 3. 拿過期的 expected_revision=3 寫入 -> REVISION_CONFLICT
        res_stale_rev = self.storage.save_plan(sid, "op_save_stale", 3, candidate)
        self.assertFalse(res_stale_rev.ok)
        self.assertEqual(res_stale_rev.error.code, "REVISION_CONFLICT")

        # 4. 拿錯誤的 plan_version=2 寫入（目前應為 1）-> PLAN_VERSION_CONFLICT
        wrong_ver = candidate.model_copy(update={"based_on_revision": 4, "plan_version": 2})
        res_wrong_ver = self.storage.save_plan(sid, "op_save_ver", 4, wrong_ver)
        self.assertFalse(res_wrong_ver.ok)
        self.assertEqual(res_wrong_ver.error.code, "PLAN_VERSION_CONFLICT")

        # 5. 更新版號至 4 但未重算配置，直接送進 save_plan -> 交易內重驗攔截為 PLAN_INVALID
        bypass_plan = candidate.model_copy(update={"based_on_revision": 4})
        res_invalid = self.storage.save_plan(sid, "op_save_invalid", 4, bypass_plan)
        self.assertFalse(res_invalid.ok)
        self.assertEqual(res_invalid.error.code, "PLAN_INVALID")
        violation_codes = [v["code"] for v in res_invalid.error.details["violations"]]
        self.assertIn("FORBIDDEN_REGION_VIOLATION", violation_codes)

    # ------------------------------------------------------------------
    # 驗收案例 5：完成無關步驟後回報抽屜放不下，重規劃只改受影響配置，進度與歷史可追溯
    # ------------------------------------------------------------------
    def test_05_complete_unrelated_step_then_replan_preserves_progress_and_history(self) -> None:
        sid, ids, _, rev = self._setup_confirmed_session("c5")
        alloc = self.storage.id_allocator
        step_pen = alloc.step_id()  # 無關步驟：文具盒 -> 桌面
        step_book_v1 = alloc.step_id()  # 受影響步驟：課本 -> 抽屜

        plan_v1 = Plan(
            plan_id=ids["plan_01"],
            plan_version=1,
            based_on_revision=rev,
            change_reason="初次規劃",
            assignments=[
                Assignment(
                    item_id=ids["item_02"],
                    region_id=ids["reg_01"],
                    reason="文具放桌面好拿",
                    fit_status=FitStatus.UNKNOWN,
                ),
                Assignment(
                    item_id=ids["item_01"],
                    region_id=ids["reg_02"],
                    reason="課本放抽屜",
                    fit_status=FitStatus.UNKNOWN,
                ),
            ],
            steps=[
                PlanStep(
                    step_id=step_pen,
                    instruction="將文具盒整理到桌面角落",
                    item_ids=[ids["item_02"]],
                    target_region_id=ids["reg_01"],
                    status=StepStatus.PENDING,
                    completed_at=None,
                ),
                PlanStep(
                    step_id=step_book_v1,
                    instruction="將課本放進抽屜",
                    item_ids=[ids["item_01"]],
                    target_region_id=ids["reg_02"],
                    status=StepStatus.PENDING,
                    completed_at=None,
                ),
            ],
            pending_confirmations=[],
        )
        self.assertTrue(self.storage.save_plan(sid, "op_v1", 3, plan_v1).ok)  # rev -> 4

        # 使用者先完成無關步驟 step_pen (rev 4 -> 5)
        res_done = self.storage.complete_step(sid, "req_done_pen", 4, 1, step_pen)
        self.assertTrue(res_done.ok)
        orig_completed_at = res_done.data["step"].completed_at

        # 使用者回報：「抽屜放不下課本，書架中層可用」
        reqs = Requirements(
            entries=[
                Requirement(
                    requirement_id=alloc.requirement_id(),
                    kind=RequirementKind.HARD_CONSTRAINT,
                    rule=RequirementRule.FORBIDDEN_REGION,
                    item_ids=[ids["item_01"]],
                    region_ids=[ids["reg_02"]],
                    source_text="抽屜放不下課本",
                )
            ]
        )
        self.assertTrue(self.storage.update_requirements(sid, "op_req_fb", 5, reqs).ok)  # rev -> 6
        self.assertTrue(
            self.storage.update_regions(
                sid,
                "op_reg_fb",
                6,
                [RegionUpdate(region_id=ids["reg_03"], availability=RegionAvailability.AVAILABLE)],
                source_text="書架中層可用",
            ).ok
        )  # rev -> 7

        # 驗證：不可把已完成的 step_pen 改換目標拿來當新任務
        mutated_plan = Plan(
            plan_id=ids["plan_01"],
            plan_version=2,
            based_on_revision=7,
            change_reason="錯誤示範：竄改已完成步驟",
            assignments=[
                Assignment(
                    item_id=ids["item_02"],
                    region_id=ids["reg_03"],
                    reason="改放書架",
                    fit_status=FitStatus.UNKNOWN,
                )
            ],
            steps=[
                PlanStep(
                    step_id=step_pen,
                    instruction="改放到書架中層",
                    item_ids=[ids["item_02"]],
                    target_region_id=ids["reg_03"],
                    status=StepStatus.PENDING,
                    completed_at=None,
                )
            ],
            pending_confirmations=[],
        )
        res_mutated = self.storage.save_plan(sid, "op_v2_bad", 7, mutated_plan)
        self.assertFalse(res_mutated.ok)
        self.assertEqual(res_mutated.error.code, "PLAN_INVALID")
        self.assertIn(
            "COMPLETED_STEP_MUTATED",
            [v["code"] for v in res_mutated.error.details["violations"]],
        )

        # 正確重規劃 v2：保留 step_pen，為課本配發新步驟 step_book_v2 -> 書架中層 (rev 7 -> 8)
        step_book_v2 = alloc.step_id()
        plan_v2 = Plan(
            plan_id=ids["plan_01"],
            plan_version=2,
            based_on_revision=7,
            change_reason="抽屜放不下課本，改放書架中層",
            assignments=[
                Assignment(
                    item_id=ids["item_02"],
                    region_id=ids["reg_01"],
                    reason="文具放桌面好拿",
                    fit_status=FitStatus.UNKNOWN,
                ),
                Assignment(
                    item_id=ids["item_01"],
                    region_id=ids["reg_03"],
                    reason="改放書架中層",
                    fit_status=FitStatus.UNKNOWN,
                ),
            ],
            steps=[
                PlanStep(
                    step_id=step_pen,
                    instruction="將文具盒整理到桌面角落",
                    item_ids=[ids["item_02"]],
                    target_region_id=ids["reg_01"],
                    status=StepStatus.PENDING,  # 儲存層讀取時會自動套用完成事件
                    completed_at=None,
                ),
                PlanStep(
                    step_id=step_book_v2,
                    instruction="將課本移到書架中層",
                    item_ids=[ids["item_01"]],
                    target_region_id=ids["reg_03"],
                    status=StepStatus.PENDING,
                    completed_at=None,
                ),
            ],
            pending_confirmations=["尚未確認書架中層是否放得下課本"],
        )
        res_v2 = self.storage.save_plan(sid, "op_v2_ok", 7, plan_v2)
        self.assertTrue(res_v2.ok)

        # 讀取最新狀態驗證：v2 生效、step_pen 完成狀態與時間完整保留、歷史位置可追溯
        state = self.storage.get_session_state(sid).data
        self.assertEqual(state.current_plan.plan_version, 2)
        steps_by_id = {s.step_id: s for s in state.current_plan.steps}
        self.assertEqual(steps_by_id[step_pen].status, StepStatus.COMPLETED)
        self.assertEqual(steps_by_id[step_pen].completed_at, orig_completed_at)
        self.assertEqual(steps_by_id[step_book_v2].status, StepStatus.PENDING)

        history = self.storage.get_location_history(sid)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].item_id, ids["item_02"])
        self.assertEqual(history[0].region_id, ids["reg_01"])
        self.assertEqual(history[0].step_id, step_pen)

    # ------------------------------------------------------------------
    # 驗收案例 6：無紀錄回 location null；僅建議位置不算已確認；透過 lookup_item_location 查詢
    # ------------------------------------------------------------------
    def test_06_location_lookup_null_suggested_vs_confirmed(self) -> None:
        sid, ids, _, rev = self._setup_confirmed_session("c6")
        alloc = self.storage.id_allocator

        # 1. 尚無計畫與紀錄 -> location 與 suggested_region_id 皆為 None
        res_empty = self.storage.lookup_item_location(sid, ids["item_01"])
        self.assertTrue(res_empty.ok)
        self.assertIsNone(res_empty.data.location)
        self.assertIsNone(res_empty.data.suggested_region_id)

        # 2. 建立計畫（建議放抽屜），但尚未確認 -> location 仍必須是 None
        plan_v1 = Plan(
            plan_id=ids["plan_01"],
            plan_version=1,
            based_on_revision=rev,
            change_reason="初次規劃",
            assignments=[
                Assignment(
                    item_id=ids["item_01"],
                    region_id=ids["reg_02"],
                    reason="建議放抽屜",
                    fit_status=FitStatus.UNKNOWN,
                )
            ],
            steps=[
                PlanStep(
                    step_id=alloc.step_id(),
                    instruction="將課本放進抽屜",
                    item_ids=[ids["item_01"]],
                    target_region_id=ids["reg_02"],
                    status=StepStatus.PENDING,
                    completed_at=None,
                )
            ],
            pending_confirmations=[],
        )
        self.assertTrue(self.storage.save_plan(sid, "op_p1", rev, plan_v1).ok)  # rev -> 4

        res_suggested = self.storage.lookup_item_location(sid, ids["item_01"])
        self.assertTrue(res_suggested.ok)
        self.assertIsNone(res_suggested.data.location)
        self.assertEqual(res_suggested.data.suggested_region_id, ids["reg_02"])

        # 3. 使用者直接確認位置在桌面 -> location 有值且 source=user_confirmation, step_id=None
        res_conf_loc = self.storage.confirm_item_location(
            sid, "req_loc_01", 4, ids["item_01"], ids["reg_01"]
        )
        self.assertTrue(res_conf_loc.ok)

        res_confirmed = self.storage.lookup_item_location(sid, ids["item_01"])
        self.assertTrue(res_confirmed.ok)
        self.assertIsNotNone(res_confirmed.data.location)
        self.assertEqual(res_confirmed.data.location.region_id, ids["reg_01"])
        self.assertEqual(res_confirmed.data.location.source, LocationSource.USER_CONFIRMATION)
        self.assertIsNone(res_confirmed.data.location.step_id)
        self.assertEqual(res_confirmed.data.suggested_region_id, ids["reg_02"])

    # ------------------------------------------------------------------
    # 驗收案例 7：Agent 超時仍可重載已保存中間資料；結果不明的寫入可查回去重紀錄
    # ------------------------------------------------------------------
    def test_07_timeout_recovery_and_ambiguous_write_replay(self) -> None:
        sid, ids, _, rev = self._setup_confirmed_session("c7")
        alloc = self.storage.id_allocator

        # 1. 模擬本輪互動先存使用者訊息 (rev 3 -> 4)，再存需求 (rev 4 -> 5)，之後 Agent 超時未存計畫
        res_msg = self.storage.append_message(
            sid,
            request_id="req_turn_01",
            expected_revision=rev,
            role=MessageRole.USER,
            text="課本不要放抽屜",
            operation_key="req_turn_01:user_msg",
        )
        self.assertTrue(res_msg.ok)
        self.assertEqual(res_msg.revision, 4)

        reqs = Requirements(
            entries=[
                Requirement(
                    requirement_id=alloc.requirement_id(),
                    kind=RequirementKind.HARD_CONSTRAINT,
                    rule=RequirementRule.FORBIDDEN_REGION,
                    item_ids=[ids["item_01"]],
                    region_ids=[ids["reg_02"]],
                    source_text="課本不要放抽屜",
                )
            ]
        )
        res_req = self.storage.update_requirements(sid, "op_turn_01_req", 4, reqs)
        self.assertTrue(res_req.ok)
        self.assertEqual(res_req.revision, 5)

        # 2. Agent 超時後重新載入狀態：中間保存的對話與需求都在，且沒有未驗證計畫被寫入
        reloaded = self.storage.get_session_state(sid).data
        self.assertEqual(reloaded.revision, 5)
        self.assertEqual(len(reloaded.messages), 1)
        self.assertEqual(len(reloaded.requirements.entries), 1)
        self.assertIsNone(reloaded.current_plan)

        # 3. 即使後續又寫入了超時回覆訊息 (rev 5 -> 6)，重送先前結果不明的寫入仍能從去重紀錄查回原結果
        self.assertTrue(
            self.storage.append_message(
                sid,
                request_id="req_turn_01",
                expected_revision=5,
                role=MessageRole.ASSISTANT,
                text="服務暫時超時，已保留您剛剛的需求。",
                operation_key="req_turn_01:assistant_msg",
            ).ok
        )  # rev -> 6

        res_replay = self.storage.update_requirements(sid, "op_turn_01_req", 4, reqs)
        self.assertTrue(res_replay.ok)
        self.assertEqual(res_replay.revision, 5)  # 查回當時成功提交的結果，不誤判為 REVISION_CONFLICT
        self.assertEqual(self.storage.get_session_state(sid).data.revision, 6)  # 不額外跳號


if __name__ == "__main__":
    unittest.main(verbosity=2)