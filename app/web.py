"""本地 Web 应用：纯标准库 http.server，单页应用 + JSON API。

启动：python -m app.web [--port 8080] [--state local-state]
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .errors import CheckError, RegistryError
from .service import Application

WEB_DIR = Path(__file__).resolve().parent / "web"


class WebHandler(BaseHTTPRequestHandler):
    app: Application  # 由 server 注入

    def log_message(self, fmt, *args):  # 安静日志
        pass

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if route == "/app.js":
            self._send_file(WEB_DIR / "app.js", "application/javascript; charset=utf-8")
            return
        if route == "/styles.css":
            self._send_file(WEB_DIR / "styles.css", "text/css; charset=utf-8")
            return
        if route == "/api/catalog":
            self._send_json({"metrics": self.app.metric_catalog()})
            return
        if route == "/api/subscriptions":
            self._send_json({"subscriptions": self.app.subscriptions.list()})
            return
        if route == "/api/inbox":
            inbox = sorted((self.app.state_dir / "inbox").glob("*.json"))
            self._send_json({"notices": [json.loads(p.read_text(encoding="utf-8")) for p in inbox]})
            return
        if route == "/api/options":
            from materials import read_bundle
            _schema, tables = read_bundle(self.app.data_root)
            self._send_json({
                "regions": sorted({r["region"] for r in tables["sales"]}),
                "families": sorted({r["family"] for r in tables["specifications"]}),
                "specs": sorted(r["id"] for r in tables["specifications"]),
                "topics": sorted({r["topic"] for r in tables["topic_memberships"]}),
                "attribution_versions": ["v1", "v2"],
                "dimensions": ["region", "family", "spec", "topic"],
            })
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._send_json({"error": f"请求体不是合法 JSON: {exc}"}, 400)
            return
        try:
            if route == "/api/query":
                self._send_json(self.app.query(payload))
            elif route == "/api/seed":
                self._send_json(self.app.seed())
            elif route == "/api/metrics/register":
                self._send_json(self.app.register_metric(payload))
            elif route == "/api/metrics/confirm":
                self._send_json(self.app.confirm_metric(payload["metric_code"], payload["actor"]))
            elif route == "/api/metrics/edit-draft":
                self._send_json(self.app.edit_draft(payload["metric_code"], payload["changes"]))
            elif route == "/api/revise/preview":
                self._send_json(self.app.revise(
                    payload["metric_code"], payload["changes"], payload["note"],
                    payload["actor"], payload["preview_context"],
                ))
            elif route == "/api/revise/confirm":
                self._send_json(self.app.confirm_revision(
                    payload["metric_code"], payload["changes"], payload["note"], payload["actor"],
                ))
            elif route == "/api/bridge":
                self._send_json(self.app.variance_bridge(payload))
            elif route == "/api/subscriptions/create":
                self._send_json(self.app.subscriptions.create(payload).to_dict())
            elif route == "/api/subscriptions/renew":
                self._send_json(self.app.subscriptions.confirm_renewal(
                    payload["subscription_id"], payload["actor"], payload.get("target_version")
                ).to_dict())
            elif route == "/api/subscriptions/revision-notice":
                self.app.subscriptions.attach_data_revision_notice(
                    payload["subscription_id"], payload["notice"])
                self._send_json({"ok": True})
            elif route == "/api/reports/run-due":
                self._send_json({"delivered": self.app.subscriptions.run_due(payload["now"])})
            elif route == "/api/history":
                self._send_json({"versions": self.app.history(payload["metric_code"])})
            else:
                self._send_json({"error": "not found"}, 404)
        except (CheckError, RegistryError) as exc:
            self._send_json({"error": str(exc)}, 422)
        except KeyError as exc:
            self._send_json({"error": f"缺少必填字段: {exc}"}, 400)


def serve(port: int = 8080, state: str | None = None) -> None:
    app = Application(state_dir=state)
    if not app.registry.all():
        app.seed()
    server = ThreadingHTTPServer(("127.0.0.1", port), WebHandler)
    WebHandler.app = app
    WebHandler.server_version = "metric-platform/1.0"
    print(f"指标平台已启动: http://127.0.0.1:{port}  (Ctrl+C 停止)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--state", default=None)
    args = parser.parse_args()
    serve(args.port, args.state)


if __name__ == "__main__":
    main()
