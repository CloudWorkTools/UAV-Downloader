"""Lightweight, resilient HTTP JSON IPC daemon for UAV Downloader."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from uav_downloader.apps.watch_categories import (
    HANIME1_GROUPS,
    JABLE_GROUPS,
    MISSAV_GROUPS,
    SUPJAV_GROUPS,
)
from uav_downloader.core import config
from uav_downloader.core import firecrawl
from uav_downloader.core.history import get_history_manager
from uav_downloader.core.video_identity import site_from_url
from uav_downloader.daemon import nas
from uav_downloader.daemon.browse_service import BrowseService
from uav_downloader.daemon.category_catalog import category_groups
from uav_downloader.daemon.task_manager import TaskManager
from uav_downloader.metadata import APP_DISPLAY_NAMES, VERSION


class DaemonRequestHandler(BaseHTTPRequestHandler):
    """Handles JSON-RPC and REST requests from the Rust Web UI."""

    server_start_time: float = time.time()
    task_manager: TaskManager = TaskManager()
    auth_token: Optional[str] = None

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr logging for quiet IPC."""
        # Un-comment below if debugging IPC traffic:
        # sys.stderr.write(f"[Daemon] {self.address_string()} - {format % args}\n")
        pass

    def _check_auth(self) -> bool:
        if not self.auth_token:
            return True
        header = self.headers.get("Authorization", "")
        token = ""
        if header.startswith("Bearer "):
            token = header[7:].strip()
        elif "X-Auth-Token" in self.headers:
            token = self.headers["X-Auth-Token"].strip()
        return token == self.auth_token

    def _send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, message: str, status: int = 400) -> None:
        self._send_json({"error": message, "success": False}, status=status)

    def _read_json_body(self) -> Dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return {}
        try:
            length = int(length_header)
            raw = self.rfile.read(length).decode("utf-8")
            if not raw.strip():
                return {}
            return json.loads(raw)
        except Exception as exc:
            raise ValueError(f"無效的 JSON 請求: {exc}")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._check_auth():
            return self._send_error("未授權 (Unauthorized)", 401)

        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            if path == "/api/ping":
                uptime = round(time.time() - self.server_start_time, 1)
                return self._send_json({
                    "status": "ok",
                    "version": VERSION,
                    "app": "UAV-Downloader-Daemon",
                    "uptime_seconds": uptime,
                    "time": datetime.now(timezone.utc).isoformat(),
                })

            elif path == "/api/sites":
                return self._send_json({
                    "sites": BrowseService.get_supported_sites()
                })

            elif path == "/api/categories":
                site = query.get("site", ["JableTV"])[0]
                categories = BrowseService.get_categories(site)
                return self._send_json({
                    "site": site,
                    "categories": categories,
                    "groups": category_groups(site, categories),
                })

            elif path == "/api/watcher/targets":
                return self._send_json({
                    "targets": {
                        "JableTV": JABLE_GROUPS,
                        "MissAV": MISSAV_GROUPS,
                        "SupJav": SUPJAV_GROUPS,
                        "Hanime1": HANIME1_GROUPS,
                    }
                })

            elif path == "/api/download/tasks":
                tasks = self.task_manager.list_tasks()
                return self._send_json({"tasks": tasks})

            elif path == "/api/download/task":
                task_id = query.get("task_id", [""])[0]
                task = self.task_manager.get_task(task_id)
                if not task:
                    return self._send_error("任務不存在", 404)
                return self._send_json({"task": task.to_dict()})

            elif path == "/api/history":
                history = get_history_manager()
                limit = int(query.get("limit", [100])[0])
                offset = int(query.get("offset", [0])[0])
                all_entries = list(history._entries.values())
                all_entries.reverse()  # newest first
                paged = all_entries[offset:offset + limit]
                return self._send_json({
                    "total": len(all_entries),
                    "offset": offset,
                    "limit": limit,
                    "entries": paged,
                })

            elif path == "/api/settings":
                prefs = config._load_prefs()
                return self._send_json({
                    "nas": nas.load_settings(),
                    "firecrawl": firecrawl.settings(),
                    "download_dir": prefs.get("download_dir") or os.environ.get("DOWNLOAD_DIR") or os.path.abspath("download"),
                    "resolution": config.get_resolution_pref(),
                    "subtitle_pref": config.get_subtitle_pref(),
                    "download_cover": config.get_download_cover(),
                    "recognition_quality": config.get_recognition_quality(),
                    "subtitle_runtime_supported": os.name == "nt",
                    "filename_mode": config.get_filename_mode(),
                    "download_concurrency": config.get_download_concurrency(),
                    "max_workers_per_video": config.get_max_workers_per_video(),
                    "proxy_url": config.get_proxy_url(),
                    "proxy_mode": config.get_proxy_mode(),
                })

            else:
                return self._send_error(f"路徑不存在: {path}", 404)

        except ValueError as exc:
            return self._send_error(str(exc), 400)
        except Exception as exc:
            return self._send_error(str(exc), 500)

    def do_POST(self) -> None:
        if not self._check_auth():
            return self._send_error("未授權 (Unauthorized)", 401)

        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"

        try:
            body = self._read_json_body()
        except ValueError as exc:
            return self._send_error(str(exc), 400)

        try:
            if path == "/api/browse":
                site = body.get("site", "JableTV")
                url = body.get("url")
                page = int(body.get("page", 1))

                if not url:
                    # Default to first category / homepage
                    cats = BrowseService.get_categories(site)
                    url = cats[0]["url"] if cats else "https://jable.tv/latest-updates/"

                result = BrowseService.fetch_videos(site, url, page)
                return self._send_json(result)

            elif path == "/api/search":
                site = body.get("site", "JableTV")
                query = body.get("query", "").strip()
                page = int(body.get("page", 1))

                if not query:
                    return self._send_error("請提供搜尋關鍵字", 400)

                try:
                    result = BrowseService.search_videos(site, query, page)
                except ValueError as exc:
                    return self._send_error(str(exc), 400)
                return self._send_json(result)

            elif path == "/api/download/start":
                url = body.get("url", "").strip()
                if not url:
                    return self._send_error("請提供影片網址", 400)

                dest = body.get("dest")
                resolution = body.get("resolution")
                max_workers = body.get("max_workers")
                enable_subtitles = body.get("enable_subtitles")
                subtitle_mode = body.get("subtitle_mode")

                task = self.task_manager.add_task(
                    url=url,
                    dest=dest,
                    resolution=resolution,
                    max_workers=max_workers,
                    enable_subtitles=enable_subtitles,
                    subtitle_mode=subtitle_mode,
                    download_cover=body.get("download_cover"),
                )
                return self._send_json({
                    "success": True,
                    "task": task.to_dict(),
                })

            elif path == '/api/download/action':
                action, task_id = body.get('action'), body.get('task_id')
                if action == 'test_destination':
                    if task_id is not None and (not isinstance(task_id, str) or not task_id):
                        return self._send_error('task_id 必須是文字', 400)
                    return self._send_json(self.task_manager.check_destination(task_id))
                operations = {'pause': self.task_manager.pause_task,
                              'resume': self.task_manager.restart_task,
                              'retry': self.task_manager.restart_task,
                              'remove': self.task_manager.remove_task}
                if not isinstance(action, str) or action not in operations or not isinstance(task_id, str) or not task_id:
                    return self._send_error('請提供有效的下載操作與 task_id', 400)
                if not operations[action](task_id):
                    return self._send_error('任務狀態已變更，目前無法執行此操作', 409)
                return self._send_json({'success': True})

            elif path == "/api/download/cancel":
                task_id = body.get("task_id", "")
                if not task_id:
                    return self._send_error("缺少 task_id", 400)
                ok = self.task_manager.cancel_task(task_id)
                return self._send_json({"success": ok})

            elif path == "/api/download/cancel_all":
                self.task_manager.cancel_all()
                return self._send_json({"success": True})

            elif path == "/api/history/check":
                url = body.get("url", "")
                title = body.get("title", "")
                history = get_history_manager()
                matched = history.find_downloaded({"url": url, "title": title})
                return self._send_json({
                    "downloaded": matched is not None,
                    "matched": matched,
                })

            elif path == "/api/settings":
                if body.get("test_firecrawl"):
                    return self._send_json(firecrawl.probe(body.get('site', 'JableTV')))
                if "firecrawl" in body:
                    firecrawl.save_settings(body['firecrawl'])
                    BrowseService._search_term.cache_clear()
                    BrowseService._search_tags.cache_clear()
                if body.get("test_nas"):
                    nas.test_connection(body.get("nas", {}))
                    return self._send_json({"success": True})
                if "nas" in body:
                    nas.save_settings(body["nas"])
                if "download_dir" in body:
                    prefs = config._load_prefs()
                    prefs["download_dir"] = os.path.abspath(body["download_dir"])
                    config._save_prefs(prefs)
                if "resolution" in body:
                    config.set_resolution_pref(body["resolution"])
                if "subtitle_pref" in body:
                    if body["subtitle_pref"] not in config.VALID_SUBTITLE_PREFS:
                        raise ValueError("不支援的字幕模式")
                    config.set_subtitle_pref(body["subtitle_pref"])
                if "download_cover" in body:
                    config.set_download_cover(body["download_cover"])
                if "recognition_quality" in body:
                    if body["recognition_quality"] not in config.VALID_RECOGNITION_QUALITIES:
                        raise ValueError("不支援的字幕辨識品質")
                    config.set_recognition_quality(body["recognition_quality"])
                if "filename_mode" in body:
                    config.set_filename_mode(body["filename_mode"])
                if "download_concurrency" in body:
                    config.set_download_concurrency(int(body["download_concurrency"]))
                    self.task_manager.max_concurrent = config.get_download_concurrency()
                if "max_workers_per_video" in body:
                    config.set_max_workers_per_video(int(body["max_workers_per_video"]))
                if "proxy_url" in body:
                    config.set_proxy_url(body["proxy_url"])
                if "proxy_mode" in body:
                    config.set_proxy_mode(body["proxy_mode"])

                return self._send_json({"success": True, "settings": config._load_prefs()})

            else:
                return self._send_error(f"路徑不存在: {path}", 404)

        except ValueError as exc:
            return self._send_error(str(exc), 400)
        except Exception as exc:
            return self._send_error(str(exc), 500)


class DaemonServer:
    """Wrapper to start and manage the ThreadingHTTPServer daemon."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 52341,
        auth_token: Optional[str] = None,
        max_concurrent: Optional[int] = None,
    ):
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

        # Bind shared TaskManager
        self.task_manager = TaskManager(
            max_concurrent=config.get_download_concurrency() if max_concurrent is None else max_concurrent,
            state_path=nas._directory() / "web_download_tasks.json",
        )
        DaemonRequestHandler.task_manager = self.task_manager
        DaemonRequestHandler.auth_token = self.auth_token

    def start(self) -> None:
        self.server = ThreadingHTTPServer((self.host, self.port), DaemonRequestHandler)
        self.task_manager.start_dispatcher()
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True, name="uav-daemon-http"
        )
        self.thread.start()
        if firecrawl.settings()['enabled']:
            threading.Thread(target=firecrawl.probe, daemon=True, name='uav-firecrawl-probe').start()

    def stop(self) -> None:
        self.task_manager.stop_dispatcher()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2.0)


def start_daemon(
    host: str = "127.0.0.1",
    port: int = 52341,
    auth_token: Optional[str] = None,
    max_concurrent: Optional[int] = None,
) -> DaemonServer:
    server = DaemonServer(
        host=host, port=port, auth_token=auth_token, max_concurrent=max_concurrent
    )
    server.start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="UAV Downloader Background IPC Daemon")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=52341, help="Listen port (default: 52341)")
    parser.add_argument("--token", default=None, help="Optional authentication token")
    parser.add_argument("--concurrency", type=int, default=None, help="Max concurrent video downloads (default: saved setting)")
    args = parser.parse_args()

    print(f"啟動 UAV Downloader 常駐 Worker 於 http://{args.host}:{args.port}", flush=True)
    server = start_daemon(
        host=args.host,
        port=args.port,
        auth_token=args.token,
        max_concurrent=args.concurrency,
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止服務...", flush=True)
        server.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
