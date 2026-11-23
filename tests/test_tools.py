import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

from help_me_tidy.scene import SceneAnalysis
from help_me_tidy.tools import MemoryBackend, ToolContext, Tools, UserActions


class ToolTests(unittest.TestCase):
    def setUp(self):
        scene = SceneAnalysis.model_validate_json(
            (Path(__file__).parents[1] / 'examples/scene.json').read_text(encoding='utf-8'))
        scene.confirmed = True
        self.backend = MemoryBackend()
        self.sid = self.backend.create_session(scene)['data']['session_id']
        self.tools = Tools(self.backend)
        self.users = UserActions(self.backend)
        self.plan = {
            'plan_id': 'plan_demo', 'plan_version': 1, 'based_on_revision': 0,
            'change_reason': '首次整理',
            'assignments': [{'item_id': 'item_demo', 'region_id': 'region_desk',
                             'reason': '保留桌面方便取用', 'fit_status': 'unknown'}],
            'steps': [{'step_id': 'step_demo', 'instruction': '將物品移到桌面右側',
                       'item_ids': ['item_demo'], 'target_region_id': 'region_desk',
                       'status': 'pending', 'completed_at': None}],
            'pending_confirmations': ['請確認物品種類及桌面可容納此物品'],
        }

    def ctx(self, operation='op', revision=0):
        return ToolContext(session_id=self.sid, operation_id=operation, expected_revision=revision)

    def save(self):
        result = self.tools.save_plan(self.ctx(), self.plan)
        self.assertTrue(result['ok'], result)
        return result

    def test_direct_tools_and_suggested_location(self):
        self.assertEqual(self.tools.get_session_state(self.ctx())['revision'], 0)
        report = self.tools.validate_plan(self.ctx(), self.plan)
        self.assertTrue(report['data']['valid'])
        self.assertTrue(report['data']['warnings'])
        self.save()
        result = self.tools.lookup_item_location(self.ctx(), 'item_demo')['data']
        self.assertIsNone(result['location'])
        self.assertEqual(result['suggested_region_id'], 'region_desk')
        self.assertEqual(self.backend.history(self.sid)['location_history'], [])

    def test_requirement_update_and_constraint_rejection(self):
        requirements = {'entries': [{'requirement_id': 'req_1', 'kind': 'hard_constraint',
            'rule': 'forbidden_region', 'item_ids': ['item_demo'], 'region_ids': ['region_desk'],
            'value': None, 'source_text': '不要放桌面'}], 'questions': []}
        self.assertTrue(self.tools.update_requirements(self.ctx(), requirements)['ok'])
        self.plan['based_on_revision'] = 1
        report = self.tools.validate_plan(self.ctx(), self.plan)['data']
        self.assertIn('CONSTRAINT_VIOLATION', {v['code'] for v in report['violations']})
        self.assertEqual(self.tools.save_plan(self.ctx('save', 1), self.plan)['error']['code'], 'PLAN_INVALID')
        self.assertEqual(self.tools.get_session_state(self.ctx())['revision'], 1)
        self.assertEqual(len(self.backend.history(self.sid)['requirements_history']), 1)

    def test_idempotency_and_revision(self):
        saved = self.save()
        self.assertEqual(self.tools.save_plan(self.ctx(), self.plan), saved)
        other = deepcopy(self.plan)
        other['change_reason'] = '不同內容'
        self.assertEqual(self.tools.save_plan(self.ctx(), other)['error']['code'], 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(self.tools.save_plan(self.ctx('new'), self.plan)['error']['code'], 'REVISION_CONFLICT')
        self.assertEqual(len(self.backend.history(self.sid)['plans']), 1)

    def test_concurrent_saves_only_commit_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.tools.save_plan(self.ctx(), self.plan), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.backend.history(self.sid)['plans']), 1)

    def test_user_completion_and_location_history(self):
        self.save()
        done = self.users.complete_step(self.ctx('done', 1), 1, 'step_demo')
        self.assertTrue(done['ok'])
        self.assertEqual(done['revision'], 2)
        self.assertEqual(self.users.complete_step(self.ctx('done', 1), 1, 'step_demo'), done)
        confirmed = self.users.confirm_item_location(self.ctx('location', 2), 'item_demo', 'region_drawer')
        self.assertEqual(confirmed['revision'], 3)
        again = self.users.complete_step(self.ctx('done-again', 3), 1, 'step_demo')
        self.assertEqual(again['revision'], 3)
        found = self.tools.lookup_item_location(self.ctx(), 'item_demo')['data']['location']
        self.assertEqual(found['region_id'], 'region_drawer')
        self.assertEqual(len(self.backend.history(self.sid)['location_history']), 2)
        self.assertEqual(self.backend.history(self.sid)['plans'][0]['steps'][0]['status'], 'pending')

    def test_completed_progress_preserved_and_cannot_be_forged(self):
        forged = deepcopy(self.plan)
        forged['steps'][0].update(status='completed', completed_at='2026-11-23T12:00:00+08:00')
        self.assertFalse(self.tools.validate_plan(self.ctx(), forged)['data']['valid'])
        self.save()
        self.users.complete_step(self.ctx('done', 1), 1, 'step_demo')
        plan = self.tools.get_session_state(self.ctx())['data']['current_plan']
        plan.update(plan_version=2, based_on_revision=2)
        self.assertTrue(self.tools.save_plan(self.ctx('v2', 2), plan)['ok'])
        self.assertEqual(self.tools.get_session_state(self.ctx())['data']['current_plan']['steps'][0]['status'], 'completed')
        plan.update(plan_version=3, based_on_revision=3)
        plan['steps'][0]['instruction'] = '改變操作'
        self.assertFalse(self.tools.validate_plan(self.ctx(), plan)['data']['valid'])

    def test_validation_failures(self):
        for mutate, code in [
            (lambda p: p['assignments'][0].update(fit_status='confirmed'), 'UNSUPPORTED_FIT'),
            (lambda p: p.update(pending_confirmations=[]), 'MISSING_CONFIRMATION'),
            (lambda p: p['assignments'][0].update(region_id='foreign'), 'INVALID_REFERENCE'),
            (lambda p: p['steps'][0].update(target_region_id='region_drawer'), 'STEP_TARGET_MISMATCH'),
            (lambda p: p.update(plan_version=3), 'PLAN_VERSION_CONFLICT'),
            (lambda p: p['assignments'].append(deepcopy(p['assignments'][0])), 'DUPLICATE_ASSIGNMENT'),
        ]:
            with self.subTest(code=code):
                plan = deepcopy(self.plan)
                mutate(plan)
                result = self.tools.validate_plan(self.ctx(), plan)
                self.assertTrue(result['ok'])
                self.assertIn(code, {v['code'] for v in result['data']['violations']})
        self.assertEqual(self.tools.get_session_state(self.ctx())['revision'], 0)

    def test_errors_isolation_and_registry(self):
        self.assertEqual(self.tools.lookup_item_location(self.ctx(), 'foreign')['error']['code'], 'ITEM_NOT_FOUND')
        missing = self.tools.get_session_state(ToolContext(session_id='missing'))
        self.assertEqual(missing['error']['code'], 'SESSION_NOT_FOUND')
        self.assertIsNone(missing['revision'])
        self.assertEqual(self.tools.call('complete_step', {}, self.ctx())['error']['code'], 'UNKNOWN_TOOL')
        self.assertEqual(self.tools.call('get_session_state', {'session_id': 'other'}, self.ctx())['error']['code'], 'INVALID_REQUEST')
        state = self.tools.get_session_state(self.ctx())
        state['data']['scene']['items'].clear()
        self.assertEqual(len(self.tools.get_session_state(self.ctx())['data']['scene']['items']), 1)
        self.assertEqual(len(json.loads(json.dumps(self.tools.definitions()))), 5)

    def test_step_four_plan_can_be_saved(self):
        from help_me_tidy.planning import create_plan, parse_requirements

        scene = SceneAnalysis.model_validate_json(
            (Path(__file__).parents[1] / 'examples/planning_scene.json').read_text(encoding='utf-8'))
        self.sid = self.backend.create_session(scene)['data']['session_id']
        req = parse_requirements(scene, '計算機每天用，不要放進抽屜，桌面左邊要留空')
        result = self.tools.update_requirements(self.ctx(), req.model_dump(mode='json'))
        self.assertTrue(result['ok'], result)
        draft = create_plan(scene, req, based_on_revision=result['revision'])
        self.assertEqual(draft.status, 'plan_ready')
        candidate = draft.plan.model_dump(mode='json')
        self.assertTrue(self.tools.validate_plan(self.ctx(), candidate)['data']['valid'])
        saved = self.tools.save_plan(self.ctx('save', 1), candidate)
        self.assertTrue(saved['ok'], saved)
        self.assertEqual(saved['revision'], 2)

    def test_failed_user_action_is_atomic(self):
        self.save()
        before = self.backend.history(self.sid)
        failed = self.users.confirm_item_location(self.ctx('bad', 1), 'item_demo', 'foreign')
        self.assertFalse(failed['ok'])
        self.assertEqual(self.backend.history(self.sid), before)
        failed = self.users.complete_step(self.ctx('bad-step', 1), 2, 'step_demo')
        self.assertEqual(failed['error']['code'], 'PLAN_VERSION_CONFLICT')
        self.assertEqual(self.backend.history(self.sid), before)


if __name__ == '__main__':
    unittest.main()
