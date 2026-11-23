import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from pydantic import ValidationError

from help_me_tidy.analysis import analyze_scene
from help_me_tidy.scene import SceneAnalysis, confirm_scene, materialize_scene

ROOT = Path(__file__).resolve().parents[1]


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "examples/scene.json").read_text(encoding="utf-8"))

    def test_unknown_survives_confirmation(self):
        scene = SceneAnalysis.model_validate(self.data)
        result = confirm_scene(scene, scene)
        self.assertTrue(result.confirmed)
        self.assertFalse(scene.confirmed)
        self.assertIsNone(result.regions[1].dimensions.width_cm)
        self.assertEqual(result.regions[1].availability, "unknown")
        self.assertTrue(result.items[0].needs_confirmation)
        self.assertEqual(len(result.questions), 2)

    def test_rename_and_measured_dimensions_keep_ids(self):
        original = SceneAnalysis.model_validate(self.data)
        self.data["items"][0].update(name="計算機", needs_confirmation=False)
        self.data["questions"].pop(0)
        self.data["regions"][0]["dimensions"]["width_cm"] = 100.0
        result = confirm_scene(original, SceneAnalysis.model_validate(self.data))
        self.assertEqual(result.items[0].item_id, original.items[0].item_id)
        self.assertEqual(result.items[0].name, "計算機")
        self.assertEqual(result.regions[0].dimensions.width_cm, 100.0)

    def test_replacement_identity_rejected(self):
        original = SceneAnalysis.model_validate(self.data)
        self.data["image_ids"] = ["different_photo"]
        with self.assertRaises(ValueError):
            confirm_scene(original, SceneAnalysis.model_validate(self.data))
        changed = json.loads(original.model_dump_json().replace("item_demo", "new_item"))
        with self.assertRaises(ValueError):
            confirm_scene(original, SceneAnalysis.model_validate(changed))

    def test_invalid_data_rejected(self):
        changes = [
            lambda d: d["items"][0].update(observed_region_id="missing"),
            lambda d: d["relations"][0].update(item_id="missing"),
            lambda d: d["questions"][0].update(related_ids=["missing"]),
            lambda d: d["regions"][0].update(region_id="item_demo"),
            lambda d: d["regions"][1].update(needs_confirmation=False),
            lambda d: d.update(questions=[]),
            lambda d: d["regions"][0]["dimensions"].update(width_cm=-1.0),
            lambda d: d["regions"][0]["dimensions"].update(width_cm=float("inf")),
            lambda d: d["items"][0].update(name="   "),
            lambda d: d["items"][0].update(needs_confirmation="false"),
            lambda d: d.update(unexpected="ignored?"),
        ]
        for change in changes:
            with self.subTest(change=change):
                data = json.loads(json.dumps(self.data))
                change(data)
                with self.assertRaises(ValidationError):
                    SceneAnalysis.model_validate(data)

    def test_model_cannot_confirm_or_invent_dimensions(self):
        for change in [lambda d: d.update(confirmed=True),
                       lambda d: d["regions"][0]["dimensions"].update(width_cm=10.0)]:
            data = json.loads(json.dumps(self.data))
            change(data)
            with self.assertRaises(ValueError):
                materialize_scene(json.dumps(data), ["trusted_image"])

    def test_structured_api_and_remapping(self):
        response = SimpleNamespace(text=json.dumps(self.data))
        client = Mock()
        client.models.generate_content.return_value = response
        scene, actual = analyze_scene(client, object(), "請分析", model="test-model",
                                      image_id="trusted_image")
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIn("items", config.response_json_schema["properties"])
        self.assertIs(actual, response)
        self.assertEqual(scene.image_ids, ["trusted_image"])
        self.assertNotEqual(scene.items[0].item_id, "item_demo")
        self.assertEqual(scene.relations[0].item_id, scene.items[0].item_id)
        self.assertEqual(scene.items[0].observed_region_id, scene.regions[0].region_id)
        self.assertEqual(scene.questions[0].related_ids, [scene.items[0].item_id])
        self.assertFalse(scene.confirmed)

    def test_empty_and_malformed_model_response(self):
        for text in (None, "not json"):
            client = Mock()
            client.models.generate_content.return_value = SimpleNamespace(text=text)
            with self.assertRaises(ValueError):
                analyze_scene(client, object(), "test", model="test", image_id="img")

    def test_allocator_collisions_rejected(self):
        with self.assertRaises(ValueError):
            materialize_scene(json.dumps(self.data), ["img"], lambda kind: "collision")

    def test_cli_confirmation_and_no_overwrite(self):
        output = Path(tempfile.gettempdir()) / f"help-me-tidy-test-{uuid4().hex}.json"
        try:
            command = [sys.executable, "-m", "help_me_tidy", "confirm",
                       str(ROOT / "examples/scene.json"), str(ROOT / "examples/scene.json"),
                       "--output", str(output)]
            first = subprocess.run(command, capture_output=True, cwd=ROOT)
            self.assertEqual(first.returncode, 0, first.stderr)
            content = output.read_bytes()
            self.assertTrue(json.loads(content)["scene"]["confirmed"])
            second = subprocess.run(command, capture_output=True, cwd=ROOT)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(content, output.read_bytes())
        finally:
            if output.exists():
                output.unlink()


if __name__ == "__main__":
    unittest.main()
