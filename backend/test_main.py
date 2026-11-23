"""
test_main.py - 驗證 main.py 的 10 條 /api/v1 路由、去重機制與 HTTP 錯誤契約
執行方式：在 HelpMeTidy/backend/ 目錄下輸入 python test_main.py
"""

import unittest
from fastapi.testclient import TestClient

import main
from storage import MemoryStorage

FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF"
FAKE_PNG = b"\x89PNG\r\n\x1a\nfake_png_bytes"


class FailingAgent:
    """用於測試 Agent 失敗時，/analysis 回 503、/messages 回 200 + status=failed。"""

    def analyze_scene(self, context, images, user_message):
        raise RuntimeError("模擬多模態模型連線失敗")

    def create_plan(self, toolkit, scene, user_message):
        raise RuntimeError("模擬規劃模型失敗")

    def handle_message(self, toolkit, user_message):
        raise RuntimeError("模擬對話模型失敗")


class TestMainAPI(unittest.TestCase):
    def setUp(self) -> None:
        # 每個測試重置記憶體狀態與去重快取，確保測試彼此獨立
        main.storage = MemoryStorage()
        main.http_idempotency = main.HttpIdempotencyStore()
        main.agent = main.MockAgent()
        self.client = TestClient(main.app)

    def _create_confirmed_session_via_http(self) -> tuple[str, int, dict]:
        r1 = self.client.post("/api/v1/sessions", json={"request_id": "req_01"})
        self.assertEqual(r1.status_code, 201)
        sid = r1.json()["session_id"]

        r2 = self.client.post(
            f"/api/v1/sessions/{sid}/images",
            data={"request_id": "req_02", "expected_revision": "0"},
            files=[("files", ("room.jpg", FAKE_JPEG, "image/jpeg"))],
        )
        self.assertEqual(r2.status_code, 201)
        img_id = r2.json()["images"][0]["image_id"]

        r3 = self.client.post(
            f"/api/v1/sessions/{sid}/analysis",
            json={
                "request_id": "req_03",
                "expected_revision": r2.json()["revision"],
                "image_ids": [img_id],
                "user_message": "幫我整理書桌",
            },
        )
        self.assertEqual(r3.status_code, 200)
        scene = r3.json()["scene"]
        self.assertFalse(scene["confirmed"])

        r4 = self.client.put(
            f"/api/v1/sessions/{sid}/scene/confirmation",
            json={
                "request_id": "req_04",
                "expected_revision": r3.json()["revision"],
                "scene": scene,
            },
        )
        self.assertEqual(r4.status_code, 200)
        self.assertTrue(r4.json()["scene"]["confirmed"])
        return sid, r4.json()["revision"], r4.json()["scene"]

    # ------------------------------------------------------------------
    # 1. 完整跑通 FRONTEND_API.md 第 1 節首次展示流程（涵蓋 9 條路由）
    # ------------------------------------------------------------------
    def test_01_full_demo_workflow(self) -> None:
        sid, rev, scene = self._create_confirmed_session_via_http()

        # 建立第一版計畫
        r_plan = self.client.post(
            f"/api/v1/sessions/{sid}/plans",
            json={
                "request_id": "req_05",
                "expected_revision": rev,
                "user_message": "常用文具要好拿",
            },
        )
        self.assertEqual(r_plan.status_code, 200)
        plan_body = r_plan.json()
        self.assertEqual(plan_body["status"], "plan_ready")
        self.assertEqual(plan_body["plan"]["plan_version"], 1)
        rev = plan_body["revision"]

        # 找出「課本」(step 0) 與「文具盒」(step 1) 的步驟
        steps = plan_body["plan"]["steps"]
        self.assertEqual(len(steps), 2)
        step_book = steps[0]
        step_pen = steps[1]
        pen_item_id = step_pen["item_ids"][0]
        book_item_id = step_book["item_ids"][0]

        # 完成無關步驟（文具盒）
        r_comp = self.client.post(
            f"/api/v1/sessions/{sid}/steps/{step_pen['step_id']}/completion",
            json={
                "request_id": "req_06",
                "expected_revision": rev,
                "plan_version": 1,
            },
        )
        self.assertEqual(r_comp.status_code, 200)
        self.assertEqual(r_comp.json()["step"]["status"], "completed")
        self.assertEqual(len(r_comp.json()["locations"]), 1)
        rev = r_comp.json()["revision"]

        # 回報「抽屜放不下，書架中層可用」-> 產出 v2 計畫並保留已完成進度
        r_msg = self.client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={
                "request_id": "req_07",
                "expected_revision": rev,
                "user_message": "抽屜放不下，書架中層可用",
            },
        )
        self.assertEqual(r_msg.status_code, 200)
        msg_body = r_msg.json()
        self.assertEqual(msg_body["status"], "plan_ready")
        self.assertEqual(msg_body["plan"]["plan_version"], 2)

        v2_steps = {s["step_id"]: s for s in msg_body["plan"]["steps"]}
        self.assertIn(step_pen["step_id"], v2_steps)
        self.assertEqual(v2_steps[step_pen["step_id"]]["status"], "completed")
        self.assertNotIn(step_book["step_id"], v2_steps)  # 受影響步驟已配發新 step_id

        # 精確查詢位置：文具盒有確認位置、課本只有建議位置（location 為 null）
        r_pen_loc = self.client.get(f"/api/v1/sessions/{sid}/items/{pen_item_id}/location")
        self.assertEqual(r_pen_loc.status_code, 200)
        self.assertIsNotNone(r_pen_loc.json()["location"])

        r_book_loc = self.client.get(f"/api/v1/sessions/{sid}/items/{book_item_id}/location")
        self.assertEqual(r_book_loc.status_code, 200)
        self.assertIsNone(r_book_loc.json()["location"])
        self.assertIsNotNone(r_book_loc.json()["suggested_region_id"])

        # 重新載入完整 SessionState
        r_get = self.client.get(f"/api/v1/sessions/{sid}")
        self.assertEqual(r_get.status_code, 200)
        self.assertEqual(r_get.json()["session_id"], sid)
        self.assertEqual(r_get.json()["current_plan"]["plan_version"], 2)

    # ------------------------------------------------------------------
    # 2. 測試圖片實際檔案內容驗證（假檔回 415、真 PNG 回 201）
    # ------------------------------------------------------------------
    def test_02_image_upload_validation(self) -> None:
        sid = self.client.post("/api/v1/sessions", json={"request_id": "req_img_01"}).json()["session_id"]

        # 上傳偽裝成 jpg 的純文字檔 -> 415 UNSUPPORTED_IMAGE_TYPE
        r_bad = self.client.post(
            f"/api/v1/sessions/{sid}/images",
            data={"request_id": "req_img_02", "expected_revision": "0"},
            files=[("files", ("fake.jpg", b"plain text not image", "image/jpeg"))],
        )
        self.assertEqual(r_bad.status_code, 415)
        self.assertEqual(r_bad.json()["error"]["code"], "UNSUPPORTED_IMAGE_TYPE")

        # 上傳真正的 PNG 檔案 -> 201
        r_ok = self.client.post(
            f"/api/v1/sessions/{sid}/images",
            data={"request_id": "req_img_03", "expected_revision": "0"},
            files=[("files", ("room.png", FAKE_PNG, "image/png"))],
        )
        self.assertEqual(r_ok.status_code, 201)
        self.assertEqual(r_ok.json()["images"][0]["mime_type"], "image/png")

    # ------------------------------------------------------------------
    # 3. 測試 HTTP 錯誤碼與固定錯誤格式（400 / 404 / 409）
    # ------------------------------------------------------------------
    def test_03_http_error_codes_and_envelope(self) -> None:
        # 400 INVALID_REQUEST（缺少必填 request_id）
        r_400 = self.client.post("/api/v1/sessions", json={})
        self.assertEqual(r_400.status_code, 400)
        self.assertEqual(r_400.json()["error"]["code"], "INVALID_REQUEST")

        # 404 SESSION_NOT_FOUND
        r_404 = self.client.get("/api/v1/sessions/ses_not_exist")
        self.assertEqual(r_404.status_code, 404)
        self.assertEqual(r_404.json()["error"]["code"], "SESSION_NOT_FOUND")

        # 409 SCENE_NOT_CONFIRMED（尚未分析與確認場景就建計畫）
        sid = self.client.post("/api/v1/sessions", json={"request_id": "req_e1"}).json()["session_id"]
        r_unconf = self.client.post(
            f"/api/v1/sessions/{sid}/plans",
            json={"request_id": "req_e2", "expected_revision": 0, "user_message": "快幫我規劃"},
        )
        self.assertEqual(r_unconf.status_code, 409)
        self.assertEqual(r_unconf.json()["error"]["code"], "SCENE_NOT_CONFIRMED")

        # 409 REVISION_CONFLICT（傳入過期的 expected_revision）
        r_rev = self.client.post(
            f"/api/v1/sessions/{sid}/images",
            data={"request_id": "req_e3", "expected_revision": "99"},
            files=[("files", ("room.jpg", FAKE_JPEG, "image/jpeg"))],
        )
        self.assertEqual(r_rev.status_code, 409)
        self.assertEqual(r_rev.json()["error"]["code"], "REVISION_CONFLICT")

    # ------------------------------------------------------------------
    # 4. 測試 HTTP 層 request_id 去重重播與內容衝突（409 IDEMPOTENCY_CONFLICT）
    # ------------------------------------------------------------------
    def test_04_idempotency_replay_and_conflict(self) -> None:
        r1 = self.client.post("/api/v1/sessions", json={"request_id": "req_dup_01"})
        r2 = self.client.post("/api/v1/sessions", json={"request_id": "req_dup_01"})
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r1.json()["session_id"], r2.json()["session_id"])

        sid = r1.json()["session_id"]
        r_img = self.client.post(
            f"/api/v1/sessions/{sid}/images",
            data={"request_id": "req_dup_02", "expected_revision": "0"},
            files=[("files", ("room.jpg", FAKE_JPEG, "image/jpeg"))],
        )
        img_id = r_img.json()["images"][0]["image_id"]

        # 複合路由 /analysis 重送相同 request_id 與內容 -> 原樣回傳
        body = {
            "request_id": "req_dup_03",
            "expected_revision": 1,
            "image_ids": [img_id],
            "user_message": "整理桌面",
        }
        r_a1 = self.client.post(f"/api/v1/sessions/{sid}/analysis", json=body)
        r_a2 = self.client.post(f"/api/v1/sessions/{sid}/analysis", json=body)
        self.assertEqual(r_a1.status_code, 200)
        self.assertEqual(r_a1.json(), r_a2.json())

        # 同一個 request_id 但改動內容 -> 409 IDEMPOTENCY_CONFLICT
        body_diff = {**body, "user_message": "改掉需求內容"}
        r_conflict = self.client.post(f"/api/v1/sessions/{sid}/analysis", json=body_diff)
        self.assertEqual(r_conflict.status_code, 409)
        self.assertEqual(r_conflict.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")

    # ------------------------------------------------------------------
    # 5. 測試第 10 條路由：直接確認物品位置（POST /locations/confirmations）與對話查詢
    # ------------------------------------------------------------------
    def test_05_direct_location_confirmation_and_chat_query(self) -> None:
        sid, rev, scene = self._create_confirmed_session_via_http()
        item_book_id = scene["items"][0]["item_id"]
        reg_shelf_id = scene["regions"][2]["region_id"]

        # 直接確認課本在書架中層（不經過計畫步驟）
        r_loc = self.client.post(
            f"/api/v1/sessions/{sid}/locations/confirmations",
            json={
                "request_id": "req_loc_01",
                "expected_revision": rev,
                "item_id": item_book_id,
                "region_id": reg_shelf_id,
            },
        )
        self.assertEqual(r_loc.status_code, 200)
        loc = r_loc.json()["location"]
        self.assertEqual(loc["source"], "user_confirmation")
        self.assertIsNone(loc["step_id"])
        rev = r_loc.json()["revision"]

        # 透過對話詢問位置 -> status=answered 且附帶確認位置
        r_chat = self.client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={
                "request_id": "req_loc_02",
                "expected_revision": rev,
                "user_message": "課本在哪裡？",
            },
        )
        self.assertEqual(r_chat.status_code, 200)
        self.assertEqual(r_chat.json()["status"], "answered")
        self.assertEqual(r_chat.json()["location"]["region_id"], reg_shelf_id)

    # ------------------------------------------------------------------
    # 6. 測試模型失敗契約：/analysis 回 503；/messages 回 HTTP 200 + status=failed
    # ------------------------------------------------------------------
    def test_06_agent_failure_contracts(self) -> None:
        sid = self.client.post("/api/v1/sessions", json={"request_id": "req_f1"}).json()["session_id"]
        r_img = self.client.post(
            f"/api/v1/sessions/{sid}/images",
            data={"request_id": "req_f2", "expected_revision": "0"},
            files=[("files", ("room.jpg", FAKE_JPEG, "image/jpeg"))],
        )
        img_id = r_img.json()["images"][0]["image_id"]

        # 替換為必定失敗的 Agent
        main.agent = FailingAgent()

        # 分析接口失敗 -> HTTP 503 SERVICE_UNAVAILABLE
        r_analysis_fail = self.client.post(
            f"/api/v1/sessions/{sid}/analysis",
            json={
                "request_id": "req_f3",
                "expected_revision": 1,
                "image_ids": [img_id],
                "user_message": "分析失敗測試",
            },
        )
        self.assertEqual(r_analysis_fail.status_code, 503)
        self.assertEqual(r_analysis_fail.json()["error"]["code"], "SERVICE_UNAVAILABLE")

        # 對話接口失敗 -> HTTP 200 + status="failed"（注意前一步保存了使用者訊息，需先取最新 revision）
        latest_rev = self.client.get(f"/api/v1/sessions/{sid}").json()["revision"]
        r_msg_fail = self.client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={
                "request_id": "req_f4",
                "expected_revision": latest_rev,
                "user_message": "對話失敗測試",
            },
        )
        self.assertEqual(r_msg_fail.status_code, 200)
        self.assertEqual(r_msg_fail.json()["status"], "failed")
        self.assertEqual(r_msg_fail.json()["error"]["code"], "MODEL_UNAVAILABLE")
        self.assertTrue(r_msg_fail.json()["error"]["retryable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)