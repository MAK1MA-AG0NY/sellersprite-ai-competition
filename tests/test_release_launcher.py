"""Offline desktop launcher tests; no GUI, account, or live website needed."""

import logging
import threading
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import release_launcher as launcher


class DesktopLauncherTests(unittest.TestCase):
    def test_closing_the_ui_joins_the_server_worker(self):
        stop = threading.Event()
        worker_exited = threading.Event()

        class FakeServer:
            ready = threading.Event()
            started = False

            def run(self):
                self.started = True
                self.ready.set()
                stop.wait(2)
                worker_exited.set()

            @property
            def should_exit(self):
                return stop.is_set()

            @should_exit.setter
            def should_exit(self, value):
                if value:
                    stop.set()

        with launcher.local_service(FakeServer()):
            self.assertFalse(stop.is_set())
            self.assertFalse(worker_exited.is_set())
        self.assertTrue(stop.is_set())
        self.assertTrue(worker_exited.is_set())

    def test_should_not_open_any_ui_if_server_start_fails(self):
        def fail():
            raise SystemExit(1)

        server = SimpleNamespace(ready=threading.Event(), started=False, should_exit=False,
                                 config=SimpleNamespace(port=8765), run=fail)
        with self.assertLogs(launcher.logger, level=logging.ERROR):
            with self.assertRaisesRegex(RuntimeError, "8765"):
                with launcher.local_service(server):
                    self.fail("The UI must not open before the local server is running")
        self.assertTrue(server.should_exit)

    def test_normal_window_close_does_not_launch_a_browser(self):
        with (patch.object(launcher, "prepare_runtime"),
              patch.object(launcher, "local_service", return_value=nullcontext()),
              patch.object(launcher, "show_desktop", return_value=True) as desktop,
              patch.object(launcher, "show_in_browser") as browser):
            self.assertEqual(launcher.main([]), 0)
        desktop.assert_called_once_with("http://127.0.0.1:8765/")
        browser.assert_not_called()

    def test_webview_failure_or_explicit_browser_mode_keeps_fallback(self):
        with (patch.object(launcher, "prepare_runtime"),
              patch.object(launcher, "local_service", return_value=nullcontext()),
              patch.object(launcher, "show_desktop", return_value=False) as desktop,
              patch.object(launcher, "show_in_browser") as browser):
            self.assertEqual(launcher.main(["--port", "8766"]), 0)
            desktop.assert_called_once_with("http://127.0.0.1:8766/")
            browser.assert_called_once_with("http://127.0.0.1:8766/", fallback=True)
            desktop.reset_mock()
            browser.reset_mock()
            self.assertEqual(launcher.main(["--browser"]), 0)
            desktop.assert_not_called()
            browser.assert_called_once_with("http://127.0.0.1:8765/", fallback=False)


if __name__ == "__main__":
    unittest.main()
