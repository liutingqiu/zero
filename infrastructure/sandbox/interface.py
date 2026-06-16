"""沙箱接口 · 抽象基类
=======================
所有沙箱实现的统一接口。支持 Windows Job Object / Docker / 自定义。
"""

from abc import ABC, abstractmethod


class SandboxInterface(ABC):
    """沙箱抽象基类——定义安全隔离的标准接口。"""

    @abstractmethod
    def setup(self) -> bool:
        """初始化沙箱环境。返回 True 表示成功。"""
        ...

    @abstractmethod
    def run_command(self, command: str, description: str = '') -> bool:
        """在沙箱中执行命令。返回 True 表示安全执行成功。"""
        ...

    @abstractmethod
    def cleanup(self):
        """清理沙箱资源。"""
        ...

    @abstractmethod
    def is_active(self) -> bool:
        """检查沙箱是否激活。"""
        ...

    @abstractmethod
    def enforce_path(self, path: str) -> bool:
        """检查路径是否在沙箱允许范围内。"""
        ...
