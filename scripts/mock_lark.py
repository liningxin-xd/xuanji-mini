from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json


class Handler(BaseHTTPRequestHandler):
    received_cards: list[dict[str, object]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/open-apis/auth/v3/tenant_access_token/internal":
            self._reply({"code": 0, "msg": "ok", "tenant_access_token": "mock_token"})
            return
        if self.path.startswith("/open-apis/im/v1/messages?"):
            payload = json.loads(body.decode("utf-8"))
            card = json.loads(payload["content"])
            if payload.get("msg_type") != "interactive" or card.get("schema") != "2.0":
                self._reply({"code": 400, "msg": "expected an interactive Card JSON 2.0"})
                return
            self.received_cards.append(card)
            print(
                json.dumps(
                    {
                        "received_card_title": card["header"]["title"]["content"],
                        "receive_id": payload.get("receive_id"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            self._reply({"code": 0, "msg": "ok", "data": {"message_id": "om_mock_xuanji"}})
            return
        self.send_error(404)

    def _reply(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Mock Lark for the xuanji-mini glue demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Mock Lark listening at http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
