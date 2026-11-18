from unittest.mock import patch
from mineval import architecture, benchmark
from mineval.agents import FakeAdapter
from mineval.artifacts import read_json
from test_agents import SimulatedDocker
from test_preparation import HistoryFixture


class ArchitectureTests(HistoryFixture):
    def test_records_and_complete_recomputed_comparisons(self):
        revision = self.freeze('example', self.review)['version']
        options = {'image': 'local:test', 'model': 'reviewer', 'effort': 'low'}
        def infer(store, workspace, prompt, **kwargs):
            if prompt == architecture.RECORD_PROMPT:
                return {'observations': [{'description': 'Answer function', 'evidence': ['app.py']}]}
            labels = sorted(p.name for p in (workspace / 'candidates').iterdir())
            self.assertNotIn('model-one', str(read_json(workspace / 'task.json')))
            for label in labels:
                self.assertEqual(set(read_json(workspace / 'candidates' / label / 'record.json')), {'observations'})
            return {'ranking': [{'candidates': labels, 'explanation': 'Equivalent interfaces',
                                  'evidence': [{'candidate': label, 'path': 'app.py'} for label in labels]}],
                    'uncertainty': 'Similar structure'}
        with patch('mineval.architecture._infer', side_effect=infer):
            for i in range(3):
                run = 'run' + str(i)
                benchmark.run(self.store, 'example', revision, run_id=run,
                              adapter=FakeAdapter(model='model-' + str(i)), sandbox=SimulatedDocker())
                architecture.record(self.store, 'example', run, **options)
                comparison = architecture.compare(self.store, 'example', revision, **options)
                if i == 0: self.assertIsNone(comparison)
                else: self.assertEqual(len(comparison['candidates']), i + 1)
        saved = list((self.store.task('example') / 'architecture/comparisons').iterdir())
        self.assertEqual(len(saved), 2)
        self.assertEqual(sorted(len(read_json(p / 'comparison.json')['candidates']) for p in saved), [2, 3])

    def test_bad_evidence_does_not_publish_record(self):
        revision = self.freeze('example', self.review)['version']
        benchmark.run(self.store, 'example', revision, run_id='run', sandbox=SimulatedDocker())
        with patch('mineval.architecture._infer', return_value={'observations': [{'description': 'Claim', 'evidence': ['missing.py']}]}):
            with self.assertRaisesRegex(ValueError, 'evidence'):
                architecture.record(self.store, 'example', 'run', image='x', model='review', effort='low')
        self.assertFalse((self.store.task('example') / 'architecture/records/run').exists())
