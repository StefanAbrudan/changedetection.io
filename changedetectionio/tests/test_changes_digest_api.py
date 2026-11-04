#!/usr/bin/env python3
"""tools/changes_digest.py against the app's real /api/v1/changes, so a change in its answer breaks a test here."""
import importlib.util
import os
import time

_script = os.path.join(os.path.dirname(__file__), '..', '..', 'tools', 'changes_digest.py')
_spec = importlib.util.spec_from_file_location('changes_digest', _script)
changes_digest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changes_digest)


def test_changes_digest_with_real_api(client, live_server, measure_memory_usage, datastore_path, tmp_path):
    datastore = client.application.config['DATASTORE']
    api_key = datastore.data['settings']['application'].get('api_access_token')

    def get_json(path):
        res = client.get(path, headers={'x-api-key': api_key})
        assert res.status_code == 200, res.get_data(as_text=True)
        return res.json

    t0 = 1700000000
    changed = datastore.add_watch(url='https://example.com/privacy', extras={'title': 'Privacy Policy', 'paused': True})
    watch = datastore.data['watching'][changed]
    watch.save_history_blob('Intro\nWe share data with our processor.\nOutro', t0, 'a')
    watch.save_history_blob('Intro\nWe share data with our processor and advertisers.\nOutro', t0 + 100, 'b')
    quiet = datastore.add_watch(url='https://example.com/quiet', extras={'title': 'Quiet page', 'paused': True})
    datastore.data['watching'][quiet].save_history_blob('Same', t0 - 100, 'a')

    output = tmp_path / 'digest.md'
    report = changes_digest.make_digest(get_json, str(output), str(tmp_path / 'state.json'), since=t0)

    assert [w['uuid'] for w in report['watches']] == [changed]
    text = output.read_text()
    assert '## Privacy Policy' in text
    assert '- URL: <https://example.com/privacy>' in text
    assert '- Last changed: 2023-11-14 22:15:00 UTC' in text
    assert '-We share data with our processor.\n+We share data with our processor and advertisers.' in text
    assert 'Quiet page' not in text

    # Run again with nothing new: what was reported doesn't come back
    changes_digest.make_digest(get_json, str(output), str(tmp_path / 'state.json'))
    assert 'No changes in this period.' in output.read_text()

    # Two more changes shortly after each other, so both fall inside the overlap: each digest only has its own
    now = int(time.time())
    watch.save_history_blob('Intro\nThird version.\nOutro', now - 5, 'c')
    changes_digest.make_digest(get_json, str(output), str(tmp_path / 'state.json'))
    assert '+Third version.' in output.read_text()

    watch.save_history_blob('Intro\nFourth version.\nOutro', now - 3, 'd')
    changes_digest.make_digest(get_json, str(output), str(tmp_path / 'state.json'))
    text = output.read_text()
    assert '-Third version.\n+Fourth version.' in text
    assert 'advertisers' not in text
