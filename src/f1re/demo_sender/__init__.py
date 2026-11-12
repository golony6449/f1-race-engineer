"""F1 25 UDP 데모 송신기(타임 트라이얼)."""

from f1re.demo_sender.driving import DriverProfile, Sample, generate_lap
from f1re.demo_sender.sender import DemoSender

__all__ = ["DemoSender", "DriverProfile", "Sample", "generate_lap"]
