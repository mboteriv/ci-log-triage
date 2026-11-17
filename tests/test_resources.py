import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ci_log_triage import core
from ci_log_triage.cli import main


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'a.xml'

    def write(self, text):
        self.path.write_text('<testsuite><testcase><failure>'+text+'</failure></testcase></testsuite>', encoding='utf-8')

    def test_terra_early_rejection(self):
        self.write('x' * 1048576)
        target = self.path.with_suffix('.json')
        target.write_text('keep')
        with patch.object(core, 'fingerprint', side_effect=AssertionError('too late')):
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([str(self.path), '--output', str(target)]), 2)
            self.assertEqual(out.getvalue(), '')
        self.assertEqual(target.read_text(), 'keep')
        self.assertEqual(len(list(self.path.parent.iterdir())), 2)

    def test_diagnostic_unicode_boundary(self):
        with patch.object(core, 'MAX_DIAGNOSTIC_BYTES', 8):
            self.write('😀😀')
            core.analyze([self.path])
            self.write('😀😀x')
            with self.assertRaises(core.InvalidInput): core.analyze([self.path])

    def test_aggregate_boundary(self):
        self.write('small')
        other = self.path.with_name('b.xml')
        other.write_bytes(self.path.read_bytes())
        size = self.path.stat().st_size * 2
        with patch.object(core, 'MAX_TOTAL_BYTES', size):
            self.assertEqual(core.analyze([self.path, other])['summary']['reports'], 2)
        with patch.object(core, 'MAX_TOTAL_BYTES', size - 1):
            with self.assertRaises(core.InvalidInput): core.analyze([self.path, other])

    def test_output_boundaries_and_determinism(self):
        self.write('😀')
        report = core.analyze([self.path])
        for render in (core.render_json, core.render_markdown):
            expected = render(report)
            size = len(expected.encode('utf-8'))
            with patch.object(core, 'MAX_OUTPUT_BYTES', size):
                self.assertEqual(render(report), expected)
            with patch.object(core, 'MAX_OUTPUT_BYTES', size - 1):
                with self.assertRaises(core.InvalidInput): render(report)

    def test_retained_rejection_before_fingerprint(self):
        self.write('small')
        with patch.object(core, 'MAX_RETAINED_BYTES', 1), patch.object(core, 'fingerprint', side_effect=AssertionError):
            with self.assertRaises(core.InvalidInput): core.analyze([self.path])
