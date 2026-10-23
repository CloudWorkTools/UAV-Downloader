"""Exercise the installed checker in disposable Git repos; reviews here are fixtures."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CHECKER = Path(__file__).resolve().parents[1] / 'tools/flow-docs/flow_checker.py'


class FlowCheckerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='uav-flow-check-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git('init', '-q')
        self.write('src/jobs.py', 'def run():\n    return 1\n\ndef other():\n    return 2\n')
        self.write('src/shared.py', 'def helper():\n    return 3\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Source baseline')
        self.adoption_base = self.git('rev-parse', 'HEAD').stdout.strip()
        features = []
        for identity, path, symbol, dependencies in [
            ('job.run', 'src/jobs.py', 'run', ['shared']),
            ('job.other', 'src/jobs.py', 'other', []),
            ('shared', 'src/shared.py', 'helper', []),
        ]:
            flow = f'docs/flows/{identity}.mmd'
            self.write(flow, 'flowchart TD\nA["Run"]\nB["Return"]\nA --> B\n')
            features.append({'id': identity, 'flow': flow, 'depends_on': dependencies,
                             'mappings': [{'node': node, 'path': path, 'symbol': symbol}
                                          for node in ['A', 'B']]})
        self.manifest = {'version': 1, 'scope': {'include': ['src/*'], 'exclude': {}},
                         'sources': [{'path': path, 'kind': 'python'}
                                     for path in ['src/jobs.py', 'src/shared.py']],
                         'features': features, 'reviews': [], 'policy_inputs': []}
        self.save()
        self.cli('snapshot')
        self.review(self.adoption_base)
        self.cli('check', '--base', self.adoption_base)
        self.git('add', '.')
        self.git('commit', '-qm', 'Flow baseline with synthetic review fixtures')
        self.base = self.git('rev-parse', 'HEAD').stdout.strip()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.name=Flow tests',
                               '-c', 'user.email=flow-tests@example.invalid', *args],
                              cwd=self.root, text=True, capture_output=True, check=True)

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(CHECKER), args[0], '--root',
                                 str(self.root), *args[1:]], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def load(self):
        self.manifest = json.loads((self.root / 'docs/flow-manifest.json').read_text())

    def save(self):
        self.write('docs/flow-manifest.json', json.dumps(self.manifest))

    def fingerprint(self, feature):
        return self.cli('fingerprint', '--base', self.base, '--feature', feature).strip()

    def review(self, base):
        self.load()
        self.manifest['reviews'] = []
        for feature in self.manifest['features']:
            identity = feature['id']
            fingerprint = self.cli('fingerprint', '--base', base, '--feature', identity).strip()
            evidence = f'docs/reviews/{identity}/{fingerprint}.json'
            record = {'feature': identity, 'fingerprint': fingerprint,
                      'reviewer': 'synthetic-test-fixture', 'verdict': 'pass',
                      'findings': [], 'evidence': ['fixture sources'], 'limitations': []}
            self.write(evidence, json.dumps(record))
            self.manifest['reviews'].append({**{key: record[key] for key in
                                               ['feature', 'fingerprint', 'reviewer', 'verdict']},
                                             'evidence': evidence})
        self.save()

    def test_cosmetic_change_refreshes_md5_without_review(self):
        before = self.fingerprint('job.run')
        path = self.root / 'src/jobs.py'
        path.write_text('# ordinary comment\n' + path.read_text())
        self.assertIn('stale md5', self.cli('check', '--base', self.base, ok=False))
        self.cli('snapshot')
        self.assertEqual(self.fingerprint('job.run'), before)
        self.cli('check', '--base', self.base)

    def test_body_change_needs_review_without_invalidating_other_symbol(self):
        before = self.fingerprint('job.other')
        self.write('src/jobs.py', 'def run():\n    return 9\n\ndef other():\n    return 2\n')
        self.cli('snapshot')
        self.assertEqual(self.fingerprint('job.other'), before)
        self.assertIn('missing current review: job.run', self.cli('check', '--base', self.base, ok=False))
        self.review(self.base)
        self.cli('check', '--base', self.base)

    def test_globals_and_shared_dependencies_invalidate_consumers(self):
        before = {identity: self.fingerprint(identity) for identity in ['job.run', 'job.other']}
        self.write('src/shared.py', 'def helper():\n    return 4\n')
        self.cli('snapshot')
        self.assertNotEqual(self.fingerprint('job.run'), before['job.run'])
        self.assertEqual(self.fingerprint('job.other'), before['job.other'])
        path = self.root / 'src/jobs.py'
        path.write_text('FLAG = True\n' + path.read_text())
        self.cli('snapshot')
        self.assertNotEqual(self.fingerprint('job.other'), before['job.other'])

    def test_unregistered_file_and_unmapped_function_fail(self):
        self.write('src/new.py', 'def new():\n    pass\n')
        self.cli('snapshot')
        self.assertIn('unregistered source', self.cli('check', '--base', self.base, ok=False))
        (self.root / 'src/new.py').unlink()
        path = self.root / 'src/jobs.py'
        path.write_text(path.read_text() + '\ndef new():\n    pass\n')
        self.cli('snapshot')
        self.assertIn('unmapped AST symbols', self.cli('check', '--base', self.base, ok=False))

    def test_removed_mapping_missing_symbol_and_missing_path_fail(self):
        self.load()
        original = json.dumps(self.manifest)
        self.manifest['features'][0]['mappings'][0]['symbol'] = 'absent'
        self.save()
        self.assertIn('missing AST symbol', self.cli('check', '--base', self.base, ok=False))
        self.manifest = json.loads(original)
        self.manifest['features'][0]['mappings'] = []
        self.save()
        self.assertIn('unmapped flow nodes', self.cli('check', '--base', self.base, ok=False))
        self.manifest = json.loads(original)
        self.save()
        (self.root / 'src/jobs.py').unlink()
        self.assertIn('missing input', self.cli('check', '--base', self.base, ok=False))

    def test_failed_incomplete_or_altered_review_cannot_pass(self):
        self.write('src/shared.py', 'def helper():\n    return 4\n')
        self.cli('snapshot')
        self.review(self.base)
        self.load()
        record = self.manifest['reviews'][0]
        target = self.root / record['evidence']
        evidence = json.loads(target.read_text())
        for verdict in ['fail', 'incomplete']:
            evidence['verdict'] = verdict
            target.write_text(json.dumps(evidence))
            self.assertIn('invalid review evidence', self.cli('check', '--base', self.base, ok=False))
        evidence['verdict'] = 'pass'
        evidence['findings'] = ['unresolved']
        target.write_text(json.dumps(evidence))
        self.assertIn('invalid review evidence', self.cli('check', '--base', self.base, ok=False))

    def test_staged_candidate_is_independent_of_worktree_fix(self):
        path = self.root / 'src/jobs.py'
        original = path.read_text()
        path.write_text(original.replace('return 1', 'return 9'))
        self.git('add', 'src/jobs.py')
        path.write_text(original)
        self.cli('check', '--base', self.base)
        self.assertIn('stale md5', self.cli('check', '--base', self.base, '--staged', ok=False))
        self.assertIn('snapshot --staged is unsupported', self.cli('snapshot', '--staged', ok=False))

    def test_missing_base_and_removed_feature_fail(self):
        self.assertIn('Git command failed', self.cli('check', '--base', 'missing-base', ok=False))
        self.load()
        self.manifest['features'].pop(1)
        self.manifest['features'][0]['mappings'].append(
            {'node': 'A', 'path': 'src/jobs.py', 'symbol': 'other'})
        self.save()
        self.assertIn('removed features', self.cli('check', '--base', self.base, ok=False))

    def test_file_mode_is_explicit_and_byte_sensitive(self):
        self.load()
        self.manifest['sources'][1]['kind'] = 'file'
        for mapping in self.manifest['features'][2]['mappings']:
            mapping.pop('symbol')
        self.save()
        self.cli('snapshot')
        before = self.fingerprint('shared')
        path = self.root / 'src/shared.py'
        path.write_text('# file mode includes comments\n' + path.read_text())
        self.cli('snapshot')
        self.assertNotEqual(self.fingerprint('shared'), before)

    def test_bad_diagram_paths_and_limits_fail_and_loop_terminates(self):
        flow = 'docs/flows/job.run.mmd'
        original = (self.root / flow).read_text()
        self.write(flow, original + 'B --> MISSING\n')
        self.assertIn('undefined edge node', self.cli('check', '--base', self.base, ok=False))
        self.write(flow, 'sequenceDiagram\nA->>B: unsupported\n')
        self.assertIn('unsupported diagram', self.cli('check', '--base', self.base, ok=False))
        self.write(flow, original + 'B --> A\n')
        self.review(self.base)
        self.cli('check', '--base', self.base)
        self.load()
        self.manifest['limits'] = {'nodes': 1}
        self.save()
        self.assertIn('node', self.cli('check', '--base', self.base, ok=False))
        self.manifest.pop('limits')
        self.manifest['limits'] = {'branch_depth': 0}
        self.save()
        self.write(flow, 'flowchart TD\nA["Run"]\nB["Return"]\nC["Fail"]\nA --> B\nA --> C\n')
        self.assertIn('branch depth', self.cli('check', '--base', self.base, ok=False))
        self.write(flow, original)
        self.manifest.pop('limits')
        self.manifest['policy_inputs'] = ['../outside']
        self.save()
        self.assertIn('unsafe path', self.cli('check', '--base', self.base, ok=False))


if __name__ == '__main__':
    unittest.main()
