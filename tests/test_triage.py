import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from ci_log_triage import core
from ci_log_triage.cli import main


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, xml, name="report.xml"):
        path = self.root / name
        path.write_text(xml, encoding="utf-8")
        return path

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(map(str, args)))
        return code, out.getvalue(), err.getvalue()

    def test_single_and_original_text(self):
        p = self.write('<testsuite><testcase name="x" classname="C"><failure type="AssertionError" message="bad">original text</failure></testcase></testsuite>')
        r = core.analyze([p])
        self.assertEqual(r["summary"]["events"], 1)
        self.assertEqual(r["groups"][0]["representative"]["text"], "original text")
        self.assertEqual(r["groups"][0]["occurrences"][0]["test"]["name"], "x")

    def test_identical(self):
        case = '<testcase name="x"><failure message="bad">bad</failure></testcase>'
        r = core.analyze([self.write('<testsuite>'+case*2+'</testsuite>')])
        self.assertEqual(r["groups"][0]["count"], 2)
        self.assertEqual([x["test"]["testcase_index"] for x in r["groups"][0]["occurrences"]], [1, 2])

    def test_realistic_nested_line_numbers_and_skipped(self):
        r = core.analyze([Path(__file__).parent/'fixtures/nested.xml'])
        self.assertEqual(r["summary"], dict(reports=1, tests=5, passed=1, skipped=1, failed_tests=3, failure_elements=2, error_elements=1, groups=2, events=3))
        self.assertEqual(r["groups"][0]["count"], 2)
        self.assertEqual(r["groups"][0]["representative"]["test"]["suite"], ['integration','database'])

    def test_distinct_same_exception_and_numbers(self):
        cases = ''.join(f'<testcase><failure type="ValueError" message="value {n}">value {n}</failure></testcase>' for n in [12, 13])
        self.assertEqual(len(core.analyze([self.write('<testsuite>'+cases+'</testsuite>')])["groups"]), 2)

    def test_failure_and_error_never_merge(self):
        p = self.write('<testsuite><testcase><failure message="x"/><error message="x"/></testcase></testsuite>')
        r = core.analyze([p])
        self.assertEqual(r['summary']['failed_tests'],1)
        self.assertEqual(len(r['groups']),2)

    def test_multiple_reports_duplicate_ids_and_determinism(self):
        xml='<testsuite><testcase name="same"><failure message="bad"/></testcase></testsuite>'
        a,b=self.write(xml,'a.xml'),self.write(xml,'b.xml')
        r=core.analyze([a,b])
        self.assertEqual(r['groups'][0]['count'],2)
        self.assertEqual(core.render_json(r),core.render_json(core.analyze([b,a,a])))
        self.assertEqual(core.render_markdown(r),core.render_markdown(core.analyze([b,a])))
        self.assertEqual(len({x['test']['source'] for x in r['groups'][0]['occurrences']}),2)

    def test_empty_reports(self):
        r=core.analyze([self.write('<testsuites/>')])
        self.assertEqual(r['groups'],[])
        self.assertEqual(core.render_markdown(r), '# CI Log Triage\n\nReports: 1 | Tests: 0 | Failed tests: 0 | Failure/error events: 0 | Groups: 0\n\nPassed: 0 | Skipped: 0\n\nNo failure or error elements found.\n')

    def test_malformed_and_wrong_root(self):
        for xml in ('<testsuite>', '', '<html/>'):
            with self.subTest(xml=xml):
                self.assertEqual(self.run_cli(self.write(xml))[0],2)

    def test_missing_and_no_glob_matches(self):
        self.assertEqual(self.run_cli(self.root/'missing.xml')[0],2)
        self.assertEqual(self.run_cli(self.root/'*.missing')[0],2)

    def test_json_markdown_and_atomic_output(self):
        p=self.write('<testsuite><testcase><failure message="oops"/></testcase></testsuite>')
        code,out,err=self.run_cli(p)
        self.assertEqual((code,err),(0,''))
        self.assertEqual(json.loads(out)['schema_version'],'1.0')
        target=self.root/'result.md'
        self.assertEqual(self.run_cli(p,'--format','markdown','--output',target)[0],0)
        self.assertIn('Affected tests:',target.read_text())
        self.assertEqual(self.run_cli(p,'--output',self.root/'missing'/'out')[0],3)

    def test_input_not_overwritten(self):
        p=self.write('<testsuite/>'); before=p.read_bytes()
        self.assertEqual(self.run_cli(p,'--output',p)[0],2)
        self.assertEqual(p.read_bytes(),before)

    def test_hardlink_not_overwritten(self):
        p=self.write('<testsuite/>'); alias=self.root/'alias.xml'
        try:
            os.link(p,alias)
        except (PermissionError, NotImplementedError) as exc:
            self.skipTest(f'filesystem cannot create hardlinks: {exc}')
        self.assertEqual(self.run_cli(p,'--output',alias)[0],2)
        self.assertEqual(p.read_text(),'<testsuite/>')

    def test_element_limit(self):
        p=self.write('<testsuite>'+'<testcase/>'*core.MAX_ELEMENTS+'</testsuite>')
        with self.assertRaises(core.InvalidInput):core.analyze([p])

    def test_skipped_with_failure_keeps_failure(self):
        p=self.write('<testsuite><testcase><skipped/><failure message="bad"/></testcase></testsuite>')
        r=core.analyze([p])
        self.assertEqual((r['summary']['failed_tests'],r['summary']['skipped']),(1,0))

    def test_invalid_second_report_preserves_output(self):
        good=self.write('<testsuite/>');bad=self.write('<oops>','bad.xml')
        target=self.write('keep','out.json')
        self.assertEqual(self.run_cli(good,bad,'--output',target)[0],2)
        self.assertEqual(target.read_text(),'keep')

    def test_entities_and_dtd_rejected_including_utf16(self):
        for declaration in ('<!DOCTYPE testsuite [<!ENTITY e "expanded">]>', '<!DOCTYPE testsuite SYSTEM "file:///secret">'):
            for encoding in ('utf-8','utf-16'):
                with self.subTest(encoding=encoding,declaration=declaration):
                    p=self.root/'entity.xml'
                    p.write_bytes((declaration+'<testsuite/>').encode(encoding))
                    with self.assertRaises(core.InvalidInput):core.analyze([p])

    def test_predefined_entities_and_namespaces(self):
        p=self.write('<testsuite xmlns="urn:junit"><testcase><failure message="a &amp; b">&lt;tag&gt;</failure></testcase></testsuite>')
        self.assertEqual(core.analyze([p])['groups'][0]['representative']['text'],'<tag>')

    def test_limits(self):
        p=self.write('<testsuites>'*65+'</testsuites>'*65)
        with self.assertRaises(core.InvalidInput):core.analyze([p])
        p.write_bytes(b' '*(core.MAX_BYTES+1))
        with self.assertRaises(core.InvalidInput):core.analyze([p])
        with self.assertRaises(core.InvalidInput):core.analyze([p]*33)

    def test_orphan_failure_not_silently_dropped(self):
        with self.assertRaises(core.InvalidInput):
            core.analyze([self.write('<testsuite><failure>oops</failure></testsuite>')])

    def test_empty_diagnostics_not_grouped(self):
        p=self.write('<testsuite><testcase name="a"><failure/></testcase><testcase name="b"><failure/></testcase></testsuite>')
        self.assertEqual(len(core.analyze([p])['groups']),2)

    def test_conservative_normalization(self):
        self.assertEqual(core.normalize('<Thing at 0xABC>'),'<Thing at <ADDRESS>>')
        self.assertEqual(core.normalize('expected 0xABC at line 12'), 'expected 0xABC at line 12')
        self.assertNotEqual(core.normalize('/tmp/a/file'),core.normalize('/tmp/b/file'))

    def test_markdown_untrusted_content(self):
        p=self.write('<testsuite><testcase name="[click](evil)"><failure>&lt;script&gt;\n```\nhello</failure></testcase></testsuite>')
        md=core.render_markdown(core.analyze([p]))
        self.assertIn('````text',md)
        self.assertIn(r'\[click\]\(evil\)',md)


if __name__ == '__main__':
    unittest.main()
