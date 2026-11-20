"""Offline desktop launcher tests; no GUI, account, or live website needed."""

import logging
import sys
import threading
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import release_launcher as launcher


class _WindowEvent:
    def __init__(self):
        self.callbacks = []
        self.fired = False

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self

    def is_set(self):
        return self.fired

    def emit(self):
        self.fired = True
        for callback in self.callbacks:
            callback()


class DesktopLauncherTests(unittest.TestCase):
    def _simulate_slow_window_creation(self, *, page_loads):
        events = SimpleNamespace(**{name: _WindowEvent() for name in ("initialized", "shown", "loaded", "closed")})
        window = SimpleNamespace(events=events, destroy=Mock(side_effect=events.closed.emit))
        completed = threading.Event()
        waited_while_shown = []

        def wait_for_load(timeout):
            shown = events.shown.is_set()
            waited_while_shown.append(shown)
            if page_loads and shown:
                events.loaded.emit()
            return completed.is_set()

        completed.wait = wait_for_load

        def start(func=None, **_):
            # A launch-time watchdog exhausts its deadline before the form appears.
            if func is not None:
                func()
            events.shown.emit()
            if not events.closed.is_set():
                if page_loads and not events.loaded.is_set():
                    events.loaded.emit()
                events.closed.emit()  # User closes the window normally.

        webview = SimpleNamespace(__file__=__file__, settings={}, create_window=Mock(return_value=window), start=start)
        fake_threading = SimpleNamespace(Event=Mock(side_effect=[completed, threading.Event()]))
        with (patch.dict(sys.modules, webview=webview),
              patch.object(launcher, "installed_webview2", return_value=True),
              patch.object(launcher, "threading", fake_threading)):
            normal_close = launcher.show_desktop("http://127.0.0.1:8765/")
        return normal_close, window.destroy, waited_while_shown

    def test_late_window_load_can_close_without_browser_fallback(self):
        normal_close, destroy, waits = self._simulate_slow_window_creation(page_loads=True)
        self.assertTrue(normal_close)
        destroy.assert_not_called()
        self.assertEqual(waits, [True])

    def test_visible_window_that_never_loads_still_falls_back(self):
        with self.assertLogs(launcher.logger, level=logging.WARNING):
            normal_close, destroy, waits = self._simulate_slow_window_creation(page_loads=False)
        self.assertFalse(normal_close)
        destroy.assert_called_once_with()
        self.assertEqual(waits, [True])

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
