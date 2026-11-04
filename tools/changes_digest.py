#!/usr/bin/env python3
"""
Write what changed across your watches as one readable markdown file, instead of push notifications.

Standard library only, so it runs from cron with any python3 (3.9+), no need for the app's own virtualenv. It only
reads, from GET /api/v1/changes (see docs/api-spec.yaml).

    export CHANGEDETECTION_API_KEY=...   # Settings > API
    python3 tools/changes_digest.py --api-url http://127.0.0.1:5000 --output digest.md             # since the last run
    python3 tools/changes_digest.py --api-url http://127.0.0.1:5000 --output digest.md --since 24h
    python3 tools/changes_digest.py --api-url http://127.0.0.1:5000 --output digest.md --since 2026-10-01 \
        --api-key-file ~/.config/changedetection/api-key

--since takes unix seconds, an ISO date or datetime (UTC unless it says otherwise), or an age such as 90m, 24h or 7d.
Without it the digest starts from the last run, kept in --state-file (default: <output>.state.json), or from 24 hours
ago on the first run.

A snapshot is dated when its fetch started but only exists once the fetch has finished, so a run can miss one that was
still being fetched. "Since the last run" therefore looks OVERLAP_SECONDS further back and uses the state file to leave
out what was already reported. Every successful run moves the last run forward, with or without --since. A run the app could not finish (it stops at
its time limit on very large installs) says so in the digest, and leaves the state as it was so nothing is lost.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_API_KEY_ENV = 'CHANGEDETECTION_API_KEY'
DEFAULT_MAX_DIFF_LINES = 40
FIRST_RUN_LOOKBACK_SECONDS = 24 * 60 * 60
OVERLAP_SECONDS = 600
PAGE_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 60
AGE_UNITS = {'m': 60, 'h': 60 * 60, 'd': 24 * 60 * 60}


class DigestError(Exception):
    """Something the user can fix, reported as one line without a traceback."""


def parse_since(value, now):
    """Unix seconds, an ISO date/datetime or an age like 24h, as unix seconds. Raises ValueError if it can't be read."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    age = re.fullmatch(r'(\d+)([mhd])', value)
    if age:
        return int(now) - int(age.group(1)) * AGE_UNITS[age.group(2)]
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def read_api_key(key_file, key_env):
    if key_file:
        try:
            with open(os.path.expanduser(key_file), encoding='utf-8') as f:
                key = f.read().strip()
        except OSError as e:
            raise DigestError(f"Can't read the API key file: {e.strerror}") from None
        if not key:
            raise DigestError(f"The API key file {key_file} is empty")
        return key
    key = os.environ.get(key_env, '').strip()
    if not key:
        raise DigestError(f"No API key: set {key_env} or pass --api-key-file")
    return key


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The API doesn't redirect, and following one could send the API key to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


def http_getter(api_url, api_key):
    """A function fetching one API path as JSON."""
    api_url = api_url.rstrip('/')
    if urllib.parse.urlsplit(api_url).scheme not in ('http', 'https'):
        raise DigestError(f"--api-url must start with http:// or https://, got '{api_url}'")
    opener = urllib.request.build_opener(_NoRedirect)

    def get_json(path):
        request = urllib.request.Request(api_url + path, headers={'x-api-key': api_key, 'Accept': 'application/json'})
        try:
            with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            raise DigestError(f"The API answered {e.code} {e.reason}: {e.read(500).decode('utf-8', 'replace').strip()}") from None
        except (urllib.error.URLError, OSError) as e:
            raise DigestError(f"Can't reach the API at {api_url}: {getattr(e, 'reason', e)}") from None
        except ValueError:
            raise DigestError(f"The API at {api_url} did not answer with JSON") from None

    return get_json


def fetch_changes(get_json, from_ts, tag=None, max_diff_lines=DEFAULT_MAX_DIFF_LINES):
    """Every page of /api/v1/changes from from_ts until now, as one report in the API's own format."""
    params = {'from': from_ts, 'format': 'json', 'limit': PAGE_SIZE, 'max_lines_per_watch': max_diff_lines}
    if tag:
        params['tag'] = tag
    report = None
    while True:
        offset = len(report['watches']) if report else 0
        page = get_json('/api/v1/changes?' + urllib.parse.urlencode({**params, 'offset': offset}))
        if report is None:
            report = page
            # The app's clock decides what "now" is, and every further page has to use the same period
            params['to'] = page['to']
        else:
            report['watches'].extend(page['watches'])
            report['incomplete'] = report.get('incomplete') or page.get('incomplete')
        if not page['watches'] or len(report['watches']) >= report['total'] or report.get('incomplete'):
            return report


def leave_out_reported(get_json, report, seen, max_diff_lines=DEFAULT_MAX_DIFF_LINES):
    """Drop what was reported before. A watch that has changed again gets its diff from what was reported instead."""
    unreported = []
    for watch in report['watches']:
        reported = seen.get(watch['uuid'])
        if reported is None:
            unreported.append(watch)
        elif watch['to_timestamp'] > reported:
            query = {'from': reported, 'to': report['to'], 'uuids': watch['uuid'], 'format': 'json',
                     'max_lines_per_watch': max_diff_lines}
            unreported.extend(get_json('/api/v1/changes?' + urllib.parse.urlencode(query))['watches'])
    report['watches'] = unreported


def _when(timestamp):
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def _one_line(text):
    return ' '.join(str(text or '').split())


def _fenced_diff(diff):
    """A code fence longer than any run of backticks in the page's own text, so that text can't close it."""
    longest = max((len(run) for run in re.findall(r'`+', diff)), default=0)
    fence = '`' * max(3, longest + 1)
    return f"{fence}diff\n{diff}\n{fence}"


def render_markdown(report, from_ts):
    """The digest. Takes from_ts separately since report['watches'] can have been narrowed by the state file."""
    watches = report['watches']
    out = ['# Changes digest', '',
           f"- Period: {_when(from_ts)} to {_when(report['to'])}",
           f"- Changed: {len(watches)} of {report['monitors_checked']} watches"]
    if report.get('incomplete'):
        out.append('- **Incomplete**: the app stopped at its time limit before it had looked at every watch, '
                   'the next run will try this period again.')
    if not watches:
        out += ['', 'No changes in this period.']

    for watch in watches:
        count = watch['changes']
        notes = [f"{count} change{'s' if count != 1 else ''}"]
        if watch.get('new_in_period'):
            notes.append('new in this period')
        out += ['', f"## {_one_line(watch.get('title')) or watch['url']}", '',
                f"- URL: <{_one_line(watch['url'])}>",
                f"- Last changed: {_when(watch['to_timestamp'])}",
                f"- Changes: {', '.join(notes)}"]
        if watch.get('error'):
            out.append(f"- Last check failed: {_one_line(watch['error'])}")
        if watch.get('diff'):
            out += ['', _fenced_diff(watch['diff'])]
        else:
            out += ['', 'Changed and changed back, no net difference.']
    return '\n'.join(out) + '\n'


def load_state(path):
    """{'last_run': unix seconds, 'seen': {uuid: newest reported snapshot}}, empty when there is no usable file."""
    try:
        with open(path, encoding='utf-8') as f:
            state = json.load(f)
        return state if isinstance(state.get('last_run'), int) and isinstance(state.get('seen'), dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def new_state(state, report):
    """The state after this run. Only what can still come up again inside the overlap needs remembering."""
    seen = dict(state.get('seen') or {})
    seen.update({watch['uuid']: watch['to_timestamp'] for watch in report['watches']})
    oldest = report['to'] - OVERLAP_SECONDS
    return {'last_run': report['to'], 'seen': {uuid: ts for uuid, ts in seen.items() if ts > oldest}}


def write_atomically(path, text):
    """A half written digest should never be what a reader finds."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.changes_digest.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        # mkstemp makes it private, the file it replaces keeps whatever access it had
        os.chmod(tmp_path, os.stat(path).st_mode if os.path.exists(path) else 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def make_digest(get_json, output, state_file, since=None, tag=None, max_diff_lines=DEFAULT_MAX_DIFF_LINES, now=None):
    """Write the digest and return the report. since: unix seconds, or None for since the last run."""
    now = time.time() if now is None else now
    state = load_state(state_file)
    if since is not None:
        from_ts, first_run, skip_seen = since, False, False
    elif state:
        from_ts, first_run, skip_seen = state['last_run'] - OVERLAP_SECONDS, False, True
    else:
        from_ts, first_run, skip_seen = int(now) - FIRST_RUN_LOOKBACK_SECONDS, True, False

    report = fetch_changes(get_json, from_ts, tag=tag, max_diff_lines=max_diff_lines)
    if skip_seen:
        leave_out_reported(get_json, report, state['seen'], max_diff_lines=max_diff_lines)
    # The period shown starts at what the reader saw last, the overlap is only there to catch late snapshots
    write_atomically(output, render_markdown(report, state['last_run'] if skip_seen else from_ts))
    if not report.get('incomplete'):
        write_atomically(state_file, json.dumps(new_state(state, report)) + '\n')
    if first_run:
        print(f"First run: covered the last {FIRST_RUN_LOOKBACK_SECONDS // 3600} hours", file=sys.stderr)
    return report


def build_parser():
    parser = argparse.ArgumentParser(description="Write what changed across your watches as one markdown file.")
    parser.add_argument('--api-url', required=True, help="Base URL of the app, like http://127.0.0.1:5000")
    parser.add_argument('--output', required=True, help="The markdown file to write, replaced on every run")
    parser.add_argument('--since', help="Unix seconds, an ISO date/datetime or an age like 24h. Default: the last run")
    parser.add_argument('--api-key-env', default=DEFAULT_API_KEY_ENV,
                        help="Environment variable holding the API key (default: %(default)s)")
    parser.add_argument('--api-key-file', help="File holding the API key, used instead of the environment variable")
    parser.add_argument('--state-file', help="Where the last run is kept (default: <output>.state.json)")
    parser.add_argument('--tag', help="Only the watches in this tag/group, by name or uuid")
    parser.add_argument('--max-diff-lines', type=int, default=DEFAULT_MAX_DIFF_LINES,
                        help="Longest diff per watch, longer ones are cut with a note (default: %(default)s)")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.max_diff_lines <= 5000:
        parser.error('--max-diff-lines must be between 1 and 5000')
    since = None
    if args.since:
        try:
            since = parse_since(args.since, time.time())
        except ValueError:
            parser.error(f"--since: can't read '{args.since}', use unix seconds, an ISO date or an age like 24h")

    try:
        get_json = http_getter(args.api_url, read_api_key(args.api_key_file, args.api_key_env))
        report = make_digest(get_json, args.output, args.state_file or args.output + '.state.json', since=since,
                             tag=args.tag, max_diff_lines=args.max_diff_lines)
    except (DigestError, OSError) as e:
        print(f"changes_digest: {e}", file=sys.stderr)
        return 1
    print(f"{len(report['watches'])} changed, written to {args.output}")
    if report.get('incomplete'):
        print("The app stopped at its time limit, this digest is incomplete and the next run will retry.",
              file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
