"""Usage: python3 server.py --host IP --port PORT --questions FILE"""
import argparse
import sys

from protocol import ProtocolError
from question_bank import load_bank
from server_network import Server


def port_number(text):
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("port must be 1-65535")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Networked Quiz Tournament server")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=port_number)
    parser.add_argument("--questions", required=True)
    args = parser.parse_args(argv)
    try:
        quizzes = load_bank(args.questions)
        server = Server(args.host, args.port, quizzes)
    except (ProtocolError, OSError) as exc:
        print(f"Server startup failed: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"SERVER_READY {server.address[0]} {server.address[1]}", flush=True)
    server.run()
    print("SERVER_STOPPED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
