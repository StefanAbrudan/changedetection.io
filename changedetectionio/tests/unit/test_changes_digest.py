#!/usr/bin/env python3

# run from dir above changedetectionio/ dir
# python3 -m unittest changedetectionio.tests.unit.test_changes_digest

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

_script = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'tools', 'changes_digest.py')
_spec = importlib.util.spec_from_file_location('changes_digest', _script)
changes_digest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changes_digest)

T0 = 1700000000  # 2023-11-14T22:13:20Z


def _watch(uuid, to_timestamp, diff='--- a\n+++ b\n-old\n+new', **extra):
    return {'uuid': uuid, 'title': f'Title {uuid}', 'url': f'https://example.com/{uuid}', 'changes': 1,
            'to_timestamp': to_timestamp, 'from_timestamp': T0, 'new_in_period': False, 'no_net_difference': not diff,
            'error': None, 'diff': diff, 'truncated': False, **extra}


def _fake_api(watches, to=T0 + 1000, incomplete=False, calls=None):
    """get_json() answering like /api/v1/changes, paging included."""
    def get_json(path):
        query = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
        if calls is not None:
            calls.append(query)
        if 'uuids' in query:
            # The app narrowed down to one watch, from what was last reported of it
            found = [w for w in watches if w['uuid'] == query['uuids']]
            return {'from': int(query['from']), 'to': int(query['to']), 'total': len(found),
                    'watches': [{**w, 'diff': f"since {query['from']}"} for w in found]}
        offset, limit = int(query['offset']), int(query['limit'])
        return {'from': int(query['from']), 'to': int(query.get('to', to)), 'total': len(watches), 'offset': offset,
                'limit': limit, 'monitors_checked': 5, 'changes_total': len(watches), 'incomplete': incomplete,
                'watches': watches[offset:offset + limit]}
    return get_json


class TestParseSince(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(changes_digest.parse_since(str(T0), T0 + 5), T0)
        self.assertEqual(changes_digest.parse_since('2023-11-14T22:13:20Z', 0), T0)
        self.assertEqual(changes_digest.parse_since('2023-11-14T23:13:20+01:00', 0), T0)
        # No timezone means UTC, whatever the machine's own timezone is
        self.assertEqual(changes_digest.parse_since('2023-11-14', 0), 1699920000)
        self.assertEqual(changes_digest.parse_since('90m', T0), T0 - 5400)
        self.assertEqual(changes_digest.parse_since('24h', T0), T0 - 86400)
        self.assertEqual(changes_digest.parse_since('7d', T0), T0 - 604800)

    def test_unreadable(self):
        for bad in ('', 'yesterday', '24x', '-5'):
            with self.assertRaises(ValueError, msg=bad):
                changes_digest.parse_since(bad, T0)


class TestRender(unittest.TestCase):
    def _report(self, *watches, **extra):
        return {'to': T0 + 100, 'monitors_checked': 3, 'watches': list(watches), **extra}

    def test_watch_section(self):
        text = changes_digest.render_markdown(self._report(_watch('a', T0 + 50, title='Pricing\n  page')), T0)
        self.assertIn('- Period: 2023-11-14 22:13:20 UTC to 2023-11-14 22:15:00 UTC', text)
        self.assertIn('- Changed: 1 of 3 watches', text)
        # A heading has to stay on one line whatever the page's title looks like
        self.assertIn('\n## Pricing page\n', text)
        self.assertIn('- URL: <https://example.com/a>', text)
        self.assertIn('- Last changed: 2023-11-14 22:14:10 UTC', text)
        self.assertIn('- Changes: 1 change\n', text)
        self.assertIn('```diff\n--- a\n+++ b\n-old\n+new\n```', text)

    def test_nothing_changed(self):
        text = changes_digest.render_markdown(self._report(), T0)
        self.assertIn('- Changed: 0 of 3 watches', text)
        self.assertIn('No changes in this period.', text)

    def test_no_net_difference_error_and_new(self):
        text = changes_digest.render_markdown(self._report(_watch('a', T0 + 50, diff='', changes=2, new_in_period=True,
                                                                  error='Timeout\nwhile fetching')), T0)
        self.assertIn('- Changes: 2 changes, new in this period', text)
        self.assertIn('- Last check failed: Timeout while fetching', text)
        self.assertIn('Changed and changed back, no net difference.', text)
        self.assertNotIn('```', text)

    def test_page_text_cannot_close_the_code_fence(self):
        text = changes_digest.render_markdown(self._report(_watch('a', T0 + 50, diff='+```\n+## Fake heading')), T0)
        self.assertIn('````diff\n+```\n+## Fake heading\n````', text)

    def test_incomplete_is_stated(self):
        self.assertIn('**Incomplete**', changes_digest.render_markdown(self._report(incomplete=True), T0))


class TestFetch(unittest.TestCase):
    def test_every_page_with_the_same_period(self):
        calls = []
        watches = [_watch(str(i), T0 + i) for i in range(changes_digest.PAGE_SIZE + 5)]
        report = changes_digest.fetch_changes(_fake_api(watches, calls=calls), T0, tag='group', max_diff_lines=7)
        self.assertEqual(len(report['watches']), len(watches))
        self.assertEqual([c['offset'] for c in calls], ['0', str(changes_digest.PAGE_SIZE)])
        # The first page's answer fixes "now", the next page has to ask for that same period
        self.assertNotIn('to', calls[0])
        self.assertEqual(calls[1]['to'], str(T0 + 1000))
        self.assertEqual((calls[0]['tag'], calls[0]['max_lines_per_watch'], calls[0]['format']), ('group', '7', 'json'))

    def test_stops_when_the_app_stops_early(self):
        calls = []
        report = changes_digest.fetch_changes(_fake_api([_watch('a', T0)] * 3, incomplete=True, calls=calls), T0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(report['incomplete'])


class TestLastRun(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = os.path.join(tmp.name, 'sub', 'digest.md')
        self.state = os.path.join(tmp.name, 'state.json')

    def _run(self, api, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()):
            return changes_digest.make_digest(api, self.output, self.state, now=T0 + 90000, **kwargs)

    def _digest(self):
        with open(self.output, encoding='utf-8') as f:
            return f.read()

    def test_first_run_looks_back_a_day_and_second_starts_from_it(self):
        calls = []
        self._run(_fake_api([_watch('a', T0 + 500)], calls=calls))
        self.assertEqual(calls[0]['from'], str(T0 + 90000 - 86400))
        self.assertIn('## Title a', self._digest())

        calls.clear()
        self._run(_fake_api([], to=T0 + 2000, calls=calls))
        # From the last run, minus the overlap that catches snapshots which were still being fetched
        self.assertEqual(calls[0]['from'], str(T0 + 1000 - changes_digest.OVERLAP_SECONDS))
        self.assertIn('- Period: 2023-11-14 22:30:00 UTC to', self._digest())
        self.assertIn('No changes in this period.', self._digest())

    def test_overlap_catches_late_snapshot_without_repeating_what_was_reported(self):
        self._run(_fake_api([_watch('reported', T0 + 900)]))
        # 'late' is dated before the last run (T0 + 1000), it only appeared after that run had finished
        self._run(_fake_api([_watch('reported', T0 + 900), _watch('late', T0 + 950), _watch('again', T0 + 1500)],
                            to=T0 + 2000))
        digest = self._digest()
        self.assertNotIn('Title reported', digest)
        self.assertIn('Title late', digest)
        self.assertIn('Title again', digest)
        # A further change of a reported watch shows, compared with what was reported rather than the overlap's start
        self._run(_fake_api([_watch('again', T0 + 2100)], to=T0 + 2200))
        digest = self._digest()
        self.assertIn('Title again', digest)
        self.assertIn(f'since {T0 + 1500}', digest)

    def test_seen_only_keeps_what_the_overlap_can_reach(self):
        self._run(_fake_api([_watch('old', T0 + 10), _watch('recent', T0 + 990)]))
        with open(self.state, encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'last_run': T0 + 1000, 'seen': {'recent': T0 + 990}})

    def test_since_shows_everything_and_moves_the_last_run(self):
        self._run(_fake_api([_watch('a', T0 + 900)]))
        calls = []
        self._run(_fake_api([_watch('a', T0 + 900)], to=T0 + 2000, calls=calls), since=T0)
        self.assertEqual(calls[0]['from'], str(T0))
        self.assertIn('Title a', self._digest())
        self.assertEqual(changes_digest.load_state(self.state)['last_run'], T0 + 2000)

    def test_incomplete_run_keeps_the_state_so_nothing_is_lost(self):
        self._run(_fake_api([]))
        before = changes_digest.load_state(self.state)
        self._run(_fake_api([_watch('a', T0 + 1500)], to=T0 + 2000, incomplete=True))
        self.assertIn('**Incomplete**', self._digest())
        self.assertEqual(changes_digest.load_state(self.state), before)

    def test_broken_state_file_counts_as_first_run(self):
        with open(self.state, 'w', encoding='utf-8') as f:
            f.write('{not json')
        calls = []
        self._run(_fake_api([], calls=calls))
        self.assertEqual(calls[0]['from'], str(T0 + 90000 - 86400))


class _StubAPI(BaseHTTPRequestHandler):
    """The real HTTP path of the script against a real socket."""
    def do_GET(self):
        self.server.seen_keys.append(self.headers.get('x-api-key'))
        if self.path.startswith('/redirect/'):
            self.send_response(302)
            self.send_header('Location', 'http://127.0.0.1:1/elsewhere')
            self.end_headers()
            return
        if self.headers.get('x-api-key') != 'the-key':
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"message": "Invalid access - API key invalid."}')
            return
        body = json.dumps({'from': 1, 'to': T0, 'total': 1, 'offset': 0, 'limit': 100, 'monitors_checked': 1,
                           'changes_total': 1, 'incomplete': False, 'watches': [_watch('a', T0 - 5)]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestCommandLine(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(('127.0.0.1', 0), _StubAPI)
        self.server.seen_keys = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.output = os.path.join(tmp.name, 'digest.md')

    def _main(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = changes_digest.main(['--api-url', self.url, '--output', self.output, *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_key_from_file(self):
        key_file = os.path.join(self.dir, 'key')
        with open(key_file, 'w', encoding='utf-8') as f:
            f.write('the-key\n')
        code, stdout, _ = self._main('--api-key-file', key_file, '--since', '24h')
        self.assertEqual(code, 0)
        self.assertIn('1 changed', stdout)
        self.assertEqual(self.server.seen_keys, ['the-key'])
        with open(self.output, encoding='utf-8') as f:
            self.assertIn('## Title a', f.read())
        self.assertTrue(os.path.exists(self.output + '.state.json'))

    def test_key_from_env(self):
        os.environ['DIGEST_TEST_KEY'] = 'the-key'
        self.addCleanup(os.environ.pop, 'DIGEST_TEST_KEY')
        self.assertEqual(self._main('--api-key-env', 'DIGEST_TEST_KEY')[0], 0)

    def test_wrong_key_is_one_readable_line_and_writes_nothing(self):
        os.environ['DIGEST_TEST_KEY'] = 'wrong-secret-value'
        self.addCleanup(os.environ.pop, 'DIGEST_TEST_KEY')
        code, _, stderr = self._main('--api-key-env', 'DIGEST_TEST_KEY')
        self.assertEqual(code, 1)
        self.assertIn('403', stderr)
        self.assertNotIn('wrong-secret-value', stderr)
        self.assertFalse(os.path.exists(self.output))

    def test_no_key(self):
        code, _, stderr = self._main('--api-key-env', 'DIGEST_TEST_KEY_NOT_SET')
        self.assertEqual(code, 1)
        self.assertIn('No API key', stderr)
        self.assertEqual(self.server.seen_keys, [])

    def test_unreachable_api(self):
        self.url = 'http://127.0.0.1:1'
        os.environ['DIGEST_TEST_KEY'] = 'the-key'
        self.addCleanup(os.environ.pop, 'DIGEST_TEST_KEY')
        code, _, stderr = self._main('--api-key-env', 'DIGEST_TEST_KEY')
        self.assertEqual(code, 1)
        self.assertIn("Can't reach the API", stderr)

    def test_only_http(self):
        self.url = 'file:///etc/hosts'
        os.environ['DIGEST_TEST_KEY'] = 'the-key'
        self.addCleanup(os.environ.pop, 'DIGEST_TEST_KEY')
        self.assertEqual(self._main('--api-key-env', 'DIGEST_TEST_KEY')[0], 1)

    def test_redirect_is_not_followed(self):
        os.environ['DIGEST_TEST_KEY'] = 'the-key'
        self.addCleanup(os.environ.pop, 'DIGEST_TEST_KEY')
        self.url += '/redirect'
        code, _, stderr = self._main('--api-key-env', 'DIGEST_TEST_KEY')
        self.assertEqual(code, 1)
        self.assertIn('302', stderr)

    def test_bad_since_exits_with_usage(self):
        with self.assertRaises(SystemExit) as raised:
            self._main('--since', 'yesterday')
        self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
