"""只读静态站容器入口。

公开部署仅托管 ``site/`` 的日报快照。它不加载业务配置或飞书凭据，也不提供
sources_api 的可写接口，避免将管理能力暴露到公网。
"""
from __future__ import annotations

import argparse
import functools
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SITE_DIR = ROOT / "site"


class StaticSiteHandler(SimpleHTTPRequestHandler):
    """为编排平台提供健康检查，同时按静态文件规则托管站点。"""

    server_version = "AI-Signal"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = self.path.split("?", 1)[0]
        if path in {"/healthz", "/readyz"}:
            body = b'{"status":"ok"}\n'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def serve(host: str, port: int, site_dir: Path) -> None:
    if not (site_dir / "index.html").is_file():
        raise SystemExit(f"站点首页不存在：{site_dir / 'index.html'}")
    handler = functools.partial(StaticSiteHandler, directory=str(site_dir))
    with ThreadingHTTPServer((host, port), handler) as httpd:
        print(f"AI-Signal static site listening on http://{host}:{port}")
        httpd.serve_forever()


def run() -> int:
    parser = argparse.ArgumentParser(description="AI-Signal 静态站服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--site-dir", default=str(DEFAULT_SITE_DIR))
    args = parser.parse_args()
    serve(args.host, args.port, Path(args.site_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
