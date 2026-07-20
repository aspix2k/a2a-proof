import sys
from pathlib import Path

from a2a_proof.demo import _DemoHandler, _DemoServer


def main() -> None:
    ready_file = Path(sys.argv[1])
    with _DemoServer(("127.0.0.1", 0), _DemoHandler) as server:
        host, port = server.server_address[:2]
        ready_file.write_text(f"http://{host}:{port}\n", encoding="ascii")
        server.serve_forever()


if __name__ == "__main__":
    main()
