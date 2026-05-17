from __future__ import annotations

import argparse
import base64
from pathlib import Path


def write_base64_json(path: Path, data_b64: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(data_b64.encode("ascii")))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--data-b64", required=True)
    args = parser.parse_args()
    write_base64_json(args.path, args.data_b64)


if __name__ == "__main__":
    main()
