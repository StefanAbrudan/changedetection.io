#!/usr/bin/env python3

# run from dir above changedetectionio/ dir
# python3 -m unittest changedetectionio.tests.unit.test_listen_host

import contextlib
import io
import os
import unittest

from changedetectionio import DEFAULT_LISTEN_HOST, default_listen_host, print_help

_dockerfile = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'Dockerfile')


class TestListenHost(unittest.TestCase):

    def test_default_is_local_only(self):
        self.assertEqual(DEFAULT_LISTEN_HOST, '127.0.0.1')
        self.assertEqual(default_listen_host({}), '127.0.0.1')

    def test_blank_value_does_not_open_every_interface(self):
        self.assertEqual(default_listen_host({'LISTEN_HOST': ''}), '127.0.0.1')
        self.assertEqual(default_listen_host({'LISTEN_HOST': '   '}), '127.0.0.1')

    def test_explicit_value_is_kept(self):
        self.assertEqual(default_listen_host({'LISTEN_HOST': '0.0.0.0'}), '0.0.0.0')
        self.assertEqual(default_listen_host({'LISTEN_HOST': ' :: '}), '::')

    def test_help_states_the_default(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            print_help()
        self.assertIn('default: LISTEN_HOST or 127.0.0.1', out.getvalue())

    def test_docker_image_still_listens_for_published_ports(self):
        with open(_dockerfile) as f:
            self.assertIn('ENV LISTEN_HOST=0.0.0.0', f.read())


if __name__ == '__main__':
    unittest.main()
