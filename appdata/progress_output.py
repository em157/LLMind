"""Small progress and status output helper for CLI flows."""

import time


class ProgressOutput:
    @staticmethod
    def info(message: str) -> None:
        print(f"[INFO] {message}")

    @staticmethod
    def ok(message: str) -> None:
        print(f"[ OK ] {message}")

    @staticmethod
    def warn(message: str) -> None:
        print(f"[WARN] {message}")

    @staticmethod
    def error(message: str) -> None:
        print(f"[FAIL] {message}")

    @staticmethod
    def step(message: str, duration_seconds: float = 0.35) -> None:
        print(f"[....] {message}", end="", flush=True)
        time.sleep(duration_seconds)
        print(" done")
