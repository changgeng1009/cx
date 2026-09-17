"""Adapter 层。"""

from .base import Adapter
from .mock import MockAdapter, RunScript, build_mock_adapter

__all__ = ["Adapter", "MockAdapter", "RunScript", "build_mock_adapter"]
