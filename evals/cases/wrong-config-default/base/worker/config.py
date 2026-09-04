from dataclasses import dataclass


@dataclass
class WorkerConfig:
    timeout_seconds: int = 30
