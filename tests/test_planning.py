import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from help_me_tidy.planning import Plan, Requirements, create_plan, parse_requirements, validate_plan
from help_me_tidy.scene import SceneAnalysis

ROOT = Path(__file__).resolve().parents[1]
MESSAGE = "計算機每天用，不要放進抽屜，桌面左邊要留空"


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.scene = SceneAnalysis.model_validate_json(
            (ROOT / "examples/planning_scene.json").read_text(encoding="utf-8"))

    def plan(self, message=MESSAGE):
        req = parse_requirements(self.scene, message)
        return req, create_plan(self.scene, req)

    def test_fixed_case_clears_left_and_preserves_unknowns(self):
        req, result = self.plan()
        self.assertEqual(req.source_text, MESSAGE)
        self.assertEqual([e.rule for e in req.entries],
                         ["usage_frequency", "forbidden_region", "keep_region_empty"])
        self.assertEqual([e.kind for e in req.entries], ["usage", "hard_constraint", "hard_constraint"])
        self.assertEqual(req.entries[1].item_ids, ["item_calculator"])
        self.assertEqual(result.status, "plan_ready")
        plan = result.plan
        self.assertEqual(plan.plan_version, 1)
        self.assertEqual({a.region_id for a in plan.assignments}, {"region_right"})
        self.assertEqual(len(plan.steps), 2)
        self.assertTrue(all(s.status == "pending" and s.completed_at is None for s in plan.steps))
        self.assertTrue(all(a.fit_status == "unknown" for a in plan.assignments))
        self.assertIn(self.scene.questions[0].text, plan.pending_confirmations)
        self.assertEqual(validate_plan(self.scene, req, plan), [])
        restored = Plan.model_validate_json(plan.model_dump_json())
        self.assertEqual([s.step_id for s in restored.steps], [s.step_id for s in plan.steps])

    def test_conflicts_request_input_without_plan(self):
        for message in (
            "計算機不要放進抽屜，必須放進第一層抽屜",
            "計算機必須放在桌面左側，桌面左邊要留空",
            "計算機必須放在桌面左側，必須放在桌面右側",
        ):
            with self.subTest(message=message):
                _, result = self.plan(message)
                self.assertEqual(result.status, "needs_input")
                self.assertIsNone(result.plan)
                self.assertTrue(result.questions)

    def test_preferences_do_not_override_hard_constraints(self):
        req, result = self.plan("計算機希望放在桌面左側，桌面左邊要留空")
        self.assertEqual(req.entries[0].kind, "preference")
        self.assertEqual(result.plan.assignments[0].region_id, "region_right")
        _, result = self.plan("計算機希望放在桌面右側")
        self.assertEqual(result.plan.assignments[0].region_id, "region_right")

    def test_unsupported_or_ambiguous_text_is_not_ignored(self):
        for message in ("計算機每天用，請讓桌面更漂亮", "手機不要放進抽屜", "不要放進抽屜",
                        "計算機必須放在書架", "計算機每天用，不要放進抽屜或桌面右側"):
            with self.subTest(message=message):
                req, result = self.plan(message)
                self.assertTrue(req.questions)
                self.assertEqual(result.status, "needs_input")
        self.scene.items[1].name = "計算機"
        self.assertEqual(self.plan()[1].status, "needs_input")

    def test_all_drawers_are_forbidden(self):
        extra = self.scene.regions[2].model_copy(deep=True)
        extra.region_id, extra.name = "region_drawer2", "第二層抽屜"
        extra.needs_confirmation, extra.availability = False, "available"
        self.scene.regions.append(extra)
        req, result = self.plan()
        self.assertEqual(set(req.entries[1].region_ids), {"region_drawer", "region_drawer2"})
        self.assertEqual(result.plan.assignments[0].region_id, "region_right")

    def test_unconfirmed_scene_and_required_unknown_region(self):
        self.assertEqual(self.plan("計算機必須放進第一層抽屜")[1].status, "needs_input")
        self.scene.confirmed = False
        self.assertEqual(self.plan()[1].status, "needs_input")

    def test_unavailable_region_is_not_used(self):
        self.scene.regions[1].availability = "unavailable"
        self.assertEqual(self.plan()[1].status, "needs_input")

    def test_invalid_references_and_contracts(self):
        req, _ = self.plan()
        req.entries[1].item_ids = ["missing"]
        with self.assertRaises(ValueError):
            create_plan(self.scene, req)
        for change in (
            lambda d: d["entries"][1].update(rule="note", value="hide restriction"),
            lambda d: d["entries"][1].update(source_text="invented"),
            lambda d: d["entries"][1].update(region_ids=[]),
            lambda d: d["entries"][1].update(kind="usage"),
        ):
            data = parse_requirements(self.scene, MESSAGE).model_dump()
            change(data)
            with self.assertRaises(ValidationError):
                Requirements.model_validate(data)

    def test_validator_rejects_tampered_plan(self):
        req, result = self.plan()
        for mutate in (
            lambda p: setattr(p.assignments[0], "item_id", "missing"),
            lambda p: setattr(p.assignments[0], "region_id", "missing"),
            lambda p: setattr(p.assignments[0], "region_id", "region_left"),
            lambda p: setattr(p.assignments[0], "region_id", "region_drawer"),
            lambda p: setattr(p.steps[0], "target_region_id", "region_left"),
            lambda p: setattr(p.steps[0], "step_id", p.steps[1].step_id),
            lambda p: p.assignments.append(p.assignments[0]),
            lambda p: p.steps.pop(),
            lambda p: p.pending_confirmations.clear(),
        ):
            candidate = result.plan.model_copy(deep=True)
            mutate(candidate)
            self.assertTrue(validate_plan(self.scene, req, candidate))
        data = result.plan.model_dump()
        data["assignments"][0]["fit_status"] = "confirmed"
        with self.assertRaises(ValidationError):
            Plan.model_validate(data)
        required = parse_requirements(self.scene, "計算機必須放在桌面左側")
        self.assertTrue(validate_plan(self.scene, required, result.plan))

    def test_region_dimensions_alone_do_not_confirm_fit(self):
        self.scene.regions[1].dimensions.width_cm = 100.0
        self.scene.regions[1].dimensions.depth_cm = 50.0
        self.scene.regions[1].dimensions.height_cm = 50.0
        self.assertTrue(all(a.fit_status == "unknown" for a in self.plan()[1].plan.assignments))

    def test_cli_and_no_overwrite(self):
        output = Path(tempfile.gettempdir()) / f"tidy-plan-{uuid4().hex}.json"
        command = [sys.executable, "-m", "help_me_tidy", "plan",
                   str(ROOT / "examples/planning_scene.json"), "--message", MESSAGE,
                   "--output", str(output)]
        try:
            first = subprocess.run(command, capture_output=True, cwd=ROOT)
            self.assertEqual(first.returncode, 0, first.stderr)
            saved = output.read_bytes()
            self.assertEqual(json.loads(saved)["result"]["status"], "plan_ready")
            second = subprocess.run(command, capture_output=True, cwd=ROOT)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(output.read_bytes(), saved)
        finally:
            if output.exists():
                output.unlink()


if __name__ == "__main__":
    unittest.main()
