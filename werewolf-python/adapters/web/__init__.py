"""后台观测页适配器：只读地把运行时状态暴露成网页和接口，供开发调试使用。"""

from .dashboard import setup_dashboard

__all__ = ["setup_dashboard"]
