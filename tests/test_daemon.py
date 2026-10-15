"""Unit tests for UAV Downloader Daemon."""

import json
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
import urllib.request
import urllib.error

from uav_downloader.daemon.server import DaemonServer
from uav_downloader.daemon.task_manager import TaskManager, DownloadTask
from uav_downloader.daemon.browse_service import BrowseService


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def request_json(url: str, method: str = 'GET', data: dict = None, headers: dict = None) -> tuple[int, dict]:
    req_headers = {'Content-Type': 'application/json'}
    if headers:
        req_headers.update(headers)
    body = json.dumps(data).encode('utf-8') if data is not None else None
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode('utf-8')
            return resp.status, json.loads(content)
    except urllib.error.HTTPError as exc:
        content = exc.read().decode('utf-8')
        try:
            return exc.code, json.loads(content)
        except Exception:
            return exc.code, {"error": content}


class TestDaemon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preferences = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.preferences.cleanup)
        preferences_patch = patch('uav_downloader.core.config._ui_prefs_path',
                                  return_value=f'{cls.preferences.name}/ui_prefs.json')
        preferences_patch.start()
        cls.addClassCleanup(preferences_patch.stop)
        cls.port = get_free_port()
        cls.server = DaemonServer(host='127.0.0.1', port=cls.port)
        cls.server.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        # Wait a tiny bit for server to start
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_ping(self):
        status, data = request_json(f"{self.base_url}/api/ping")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "ok")
        self.assertIn("version", data)
        self.assertIn("uptime_seconds", data)

    def test_sites(self):
        status, data = request_json(f"{self.base_url}/api/sites")
        self.assertEqual(status, 200)
        sites = data.get("sites", [])
        self.assertIn("JableTV", sites)
        self.assertIn("MissAV", sites)
        self.assertIn("SupJav", sites)
        self.assertIn("Hanime1", sites)

    def test_categories(self):
        status, data = request_json(f"{self.base_url}/api/categories?site=JableTV")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("site"), "JableTV")
        categories = data.get("categories", [])
        self.assertGreater(len(categories), 0)
        self.assertIn("url", categories[0])
        self.assertIn("name", categories[0])

    def test_grouped_categories_api(self):
        with patch.object(BrowseService, 'get_categories', return_value=[
            {'name': '最近更新', 'url': 'https://jable.tv/latest-updates/'}
        ]):
            status, data = request_json(f"{self.base_url}/api/categories?site=JableTV")
        self.assertEqual(status, 200)
        self.assertTrue(data['categories'])
        self.assertTrue(any(group['id'] == 'tags:衣著' for group in data['groups']))
        self.assertEqual(data['groups'][0]['name'], '動態 / 榜單')

    def test_watcher_targets(self):
        status, data = request_json(f"{self.base_url}/api/watcher/targets")
        self.assertEqual(status, 200)
        targets = data.get("targets", {})
        self.assertIn("JableTV", targets)
        self.assertIn("MissAV", targets)
        self.assertIn("SupJav", targets)
        self.assertIn("Hanime1", targets)
        self.assertGreater(len(targets["JableTV"]), 0)

    def test_settings_get_and_post(self):
        status, settings = request_json(f"{self.base_url}/api/settings")
        self.assertEqual(status, 200)
        self.assertIn("resolution", settings)
        self.assertIn("download_concurrency", settings)

        # Update setting
        update_payload = {
            "resolution": "720",
            "filename_mode": "code-only",
        }
        status_post, post_res = request_json(
            f"{self.base_url}/api/settings", method="POST", data=update_payload
        )
        self.assertEqual(status_post, 200)
        self.assertTrue(post_res.get("success"))

        # Verify updated
        _, updated = request_json(f"{self.base_url}/api/settings")
        self.assertEqual(updated.get("resolution"), "720")
        self.assertEqual(updated.get("filename_mode"), "code-only")

    def test_nas_settings_are_persistent_and_secrets_are_not_returned(self):
        import os
        import tempfile
        from uav_downloader.daemon import nas

        profile = dict(enabled=True, host="192.168.1.20", share="video",
                       directory="", username="user", password="private-nas-password")
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CONFIG_DIR=folder):
            status, result = request_json(f"{self.base_url}/api/settings", method="POST", data={"nas": profile})
            self.assertEqual(status, 200)
            self.assertTrue(result["success"])
            self.assertNotIn(profile["password"], json.dumps(result))
            status, result = request_json(f"{self.base_url}/api/settings")
            self.assertTrue(result["nas"]["password_saved"])
            self.assertNotIn("password", result["nas"])
            self.assertEqual(nas.load_settings(secret=True)["password"], profile["password"])
            status, result = request_json(f"{self.base_url}/api/settings", method="POST", data={"nas": {**profile, "share": "../bad"}})
            self.assertEqual(status, 400)
            self.assertFalse(result["success"])
            with patch.object(nas, "test_connection") as test:
                status, result = request_json(f"{self.base_url}/api/settings", method="POST", data={"test_nas": True, "nas": profile})
                self.assertTrue(result["success"])
                test.assert_called_once_with(profile)

    @patch("uav_downloader.sites.jabletv.JableTVBrowser.fetch_page")
    def test_browse_mocked(self, mock_fetch):
        mock_fetch.return_value = [
            {"url": "https://jable.tv/videos/test-001/", "title": "Test Title 1", "thumbnail": "https://img/1.jpg", "duration": "12:34"}
        ]
        status, data = request_json(
            f"{self.base_url}/api/browse",
            method="POST",
            data={"site": "JableTV", "url": "https://jable.tv/latest-updates/", "page": 1},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data.get("count"), 1)
        self.assertEqual(len(data.get("videos")), 1)
        self.assertEqual(data["videos"][0]["title"], "Test Title 1")

    @patch("uav_downloader.sites.missav.MissAVBrowser.fetch_page")
    def test_search_mocked(self, mock_fetch):
        mock_fetch.side_effect = lambda url: [] if 'page=' in url else [
            {"url": "https://missav.ai/test-002", "title": "MissAV Test", "thumbnail": "https://img/2.jpg"}
        ]
        status, data = request_json(
            f"{self.base_url}/api/search",
            method="POST",
            data={"site": "MissAV", "query": "SSIS", "page": 1},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data.get("count"), 1)
        self.assertEqual(data["videos"][0]["title"], "MissAV Test")

    def test_download_task_lifecycle(self):
        # 1. Start download task
        start_payload = {
            "url": "https://jable.tv/videos/test-mock-url/",
            "dest": "/tmp/test_download",
            "resolution": "1080",
        }
        status, res = request_json(
            f"{self.base_url}/api/download/start", method="POST", data=start_payload
        )
        self.assertEqual(status, 200)
        self.assertTrue(res.get("success"))
        task_data = res.get("task", {})
        task_id = task_data.get("task_id")
        self.assertTrue(task_id.startswith("task_"))

        # 2. Get task list
        status_list, list_res = request_json(f"{self.base_url}/api/download/tasks")
        self.assertEqual(status_list, 200)
        task_ids = [t["task_id"] for t in list_res.get("tasks", [])]
        self.assertIn(task_id, task_ids)
        self.assertIn('activities', list_res)
        self.assertIsInstance(list_res['observed_at'], (int, float))

        # 3. Query single task
        status_single, single_res = request_json(f"{self.base_url}/api/download/task?task_id={task_id}")
        self.assertEqual(status_single, 200)
        self.assertEqual(single_res.get("task", {}).get("task_id"), task_id)

        # 4. Cancel task
        status_cancel, cancel_res = request_json(
            f"{self.base_url}/api/download/cancel", method="POST", data={"task_id": task_id}
        )
        self.assertEqual(status_cancel, 200)
        self.assertTrue(cancel_res.get("success"))

    def test_task_controls_and_destination_check_api(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(self.server.task_manager, 'start_dispatcher'), patch('uav_downloader.daemon.nas.load_settings', return_value={'enabled': False}):
            task = self.server.task_manager.add_task('https://jable.tv/videos/control-fixture/', dest=folder)
            for action, expected in [('pause', 'paused'), ('resume', 'pending'), ('pause', 'paused')]:
                code, result = request_json(f'{self.base_url}/api/download/action', method='POST', data={'action': action, 'task_id': task.task_id})
                self.assertEqual(code, 200)
                self.assertTrue(result['success'])
                self.assertEqual(task.status, expected)
            code, result = request_json(f'{self.base_url}/api/download/action', method='POST', data={'action': 'resume', 'task_id': 'missing'})
            self.assertEqual(code, 409)
            self.assertFalse(result['success'])
            code, result = request_json(f'{self.base_url}/api/download/action', method='POST', data={'action': 'test_destination', 'task_id': task.task_id})
            self.assertEqual(code, 200)
            self.assertTrue(result['success'])
            for body in [{'action': []}, {'action': 'pause', 'task_id': []}, {'action': 'test_destination', 'task_id': ''}]:
                code, result = request_json(f'{self.base_url}/api/download/action', method='POST', data=body)
                self.assertEqual(code, 400)
            self.server.task_manager.cancel_task(task.task_id)
            code, result = request_json(f'{self.base_url}/api/download/action', method='POST', data={'action': 'retry', 'task_id': task.task_id})
            self.assertEqual(code, 200)
            self.assertEqual(task.status, 'pending')
            self.server.task_manager.cancel_task(task.task_id)
            code, result = request_json(f'{self.base_url}/api/download/action', method='POST', data={'action': 'remove', 'task_id': task.task_id})
            self.assertEqual(code, 200)
            self.assertIsNone(self.server.task_manager.get_task(task.task_id))
            completed = self.server.task_manager.add_task('https://jable.tv/videos/clear-completed-fixture/', dest=folder)
            completed.status = 'completed'
            code, result = request_json(f'{self.base_url}/api/download/action', method='POST', data={'action': 'clear_completed'})
            self.assertEqual(code, 200)
            self.assertTrue(result['success'])
            self.assertGreaterEqual(result['removed'], 1)
            self.assertIsNone(self.server.task_manager.get_task(completed.task_id))

    def test_progress_is_visible_while_search_response_is_pending(self):
        from uav_downloader.core import activity
        started, release = threading.Event(), threading.Event()
        results = []
        def slow_search(*args):
            activity.report('讀取第 4 頁', part='條件一')
            started.set()
            assert release.wait(3)
            return {'videos': [], 'count': 0}
        with patch.object(BrowseService, 'search_videos', side_effect=slow_search):
            thread = threading.Thread(target=lambda: results.append(request_json(
                f'{self.base_url}/api/search', method='POST', data={'site':'JableTV', 'query':'live progress fixture'})))
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                code, data = request_json(f'{self.base_url}/api/download/tasks')
                record = next(item for item in data['activities'] if item['query']=='live progress fixture')
                self.assertEqual(code, 200)
                self.assertEqual(record['state'], 'running')
                self.assertEqual(record['parts']['條件一'], '讀取第 4 頁')
            finally:
                release.set()
                thread.join(3)
            self.assertEqual(results[0][0], 200)

    def test_cancel_search_reaches_workers_and_removes_activity(self):
        from uav_downloader.core import activity
        started, release = threading.Event(), threading.Event()
        results = []
        def slow_search(*args, **kwargs):
            started.set()
            assert release.wait(3)
            activity.check_cancelled()
            return {'videos': []}
        job = 'cancel-daemon-fixture'
        with patch.object(BrowseService, 'search_videos', side_effect=slow_search):
            thread = threading.Thread(target=lambda: results.append(request_json(
                f'{self.base_url}/api/search', method='POST', data={'site':'MissAV', 'query':'a b', 'request_id':job})))
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                status, result = request_json(f'{self.base_url}/api/search/cancel', method='POST', data={'request_id':job})
                self.assertEqual(status, 200)
                self.assertTrue(result['success'])
                self.assertFalse(any(item.get('job_id') == job for item in activity.snapshot()))
            finally:
                release.set()
                thread.join(3)
            self.assertTrue(results[0][1]['cancelled'])
            self.assertFalse(any(item.get('job_id') == job for item in activity.snapshot()))
        status, _ = request_json(f'{self.base_url}/api/search/cancel', method='POST', data={'request_id':[]})
        self.assertEqual(status, 400)

    def test_history(self):
        status, res = request_json(f"{self.base_url}/api/history?limit=10&offset=0")
        self.assertEqual(status, 200)
        self.assertIn("total", res)
        self.assertIn("entries", res)

        # History check
        check_status, check_res = request_json(
            f"{self.base_url}/api/history/check",
            method="POST",
            data={"url": "https://not-downloaded-random-url-xyz.com"},
        )
        self.assertEqual(check_status, 200)
        self.assertIn("downloaded", check_res)


class TestDaemonAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = get_free_port()
        cls.token = "secret_test_token_123"
        cls.server = DaemonServer(host='127.0.0.1', port=cls.port, auth_token=cls.token)
        cls.server.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_unauthorized(self):
        status, res = request_json(f"{self.base_url}/api/ping")
        self.assertEqual(status, 401)
        self.assertIn("error", res)

    def test_authorized_bearer(self):
        status, res = request_json(
            f"{self.base_url}/api/ping",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(res.get("status"), "ok")


if __name__ == "__main__":
    unittest.main()
