"""Docker 沙箱 · 跨平台隔离
============================
基于 Docker 容器的进程级隔离——替代 Windows Job Object。

特性:
  - 真正的进程级硬隔离
  - 内存/CPU 限制通过 cgroups
  - 网络隔离通过 Docker network
  - 跨平台: Linux / macOS / Windows (WSL2)
"""

import os
import subprocess
import shutil
from datetime import datetime

from config import get_logger
from infrastructure.sandbox.interface import SandboxInterface

logger = get_logger('zero.sandbox.docker')

BASE_SANDBOX_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), 'data', 'sandbox')


class DockerSandbox(SandboxInterface):
    """Docker 容器沙箱——跨平台安全隔离。"""

    IMAGE = 'python:3.11-slim'  # 轻量 Python 镜像

    def __init__(self, network_enabled: bool = False,
                 max_memory_mb: int = 512, max_timeout: int = 120,
                 image: str = ''):
        self.test_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.test_dir = os.path.join(BASE_SANDBOX_DIR, f'docker_{self.test_id}')
        self.container_name = f'zero_sandbox_{self.test_id}'
        self.network_enabled = network_enabled
        self.max_memory_mb = max_memory_mb
        self.max_timeout = max_timeout
        self.image = image or self.IMAGE
        self._active = False

    def setup(self) -> bool:
        """创建 Docker 容器并启动。"""
        os.makedirs(self.test_dir, exist_ok=True)
        try:
            # 检查 Docker 是否可用
            r = subprocess.run(['docker', 'info'], capture_output=True, timeout=5)
            if r.returncode != 0:
                logger.warning('Docker 不可用，降级为无沙箱模式')
                self._active = False
                return True  # 不阻断执行，但记录警告

            # 创建容器
            cmd = [
                'docker', 'run', '-d', '--rm',
                '--name', self.container_name,
                '--memory', f'{self.max_memory_mb}m',
                '--cpus', '1',
                '--network', 'none' if not self.network_enabled else 'bridge',
                '--read-only',
                '--tmpfs', '/tmp:rw,noexec,nosuid,size=256m',
                '-v', f'{os.path.abspath(self.test_dir)}:/workspace:rw',
                '-w', '/workspace',
                self.image,
                'sleep', str(self.max_timeout),
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                self._active = True
                logger.info('Docker 沙箱启动: %s', self.container_name)
            else:
                logger.warning('Docker 容器创建失败: %s', r.stderr.strip())
                self._active = False
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning('Docker 不可用: %s', exc)
            self._active = False
        return True

    def run_command(self, command: str, description: str = '') -> bool:
        """在 Docker 容器中执行命令。"""
        if not self._active:
            logger.warning('沙箱未激活，跳过: %s', description)
            return False

        try:
            cmd = ['docker', 'exec', self.container_name,
                   'timeout', str(self.max_timeout),
                   'sh', '-c', command]
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.max_timeout + 5)
            ok = r.returncode == 0
            if not ok:
                logger.warning('Docker 执行失败(%s): %s',
                               description, r.stderr[:200])
            return ok
        except subprocess.TimeoutExpired:
            logger.warning('Docker 执行超时: %s', description)
            return False
        except Exception as exc:
            logger.warning('Docker 执行异常: %s', exc)
            return False

    def cleanup(self):
        """停止并删除容器。"""
        if self._active:
            try:
                subprocess.run(['docker', 'stop', self.container_name],
                               capture_output=True, timeout=5)
                subprocess.run(['docker', 'rm', '-f', self.container_name],
                               capture_output=True, timeout=5)
            except Exception:
                pass
            self._active = False
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def is_active(self) -> bool:
        return self._active

    def enforce_path(self, path: str) -> bool:
        """Docker 沙箱路径在容器内，宿主机路径检查由 volume 挂载保证。"""
        if not path:
            return True
        try:
            real = os.path.realpath(path)
            sandbox_real = os.path.realpath(self.test_dir)
            return real.startswith(sandbox_real + os.sep) or real == sandbox_real
        except (OSError, ValueError):
            return False


def create_sandbox(network_enabled: bool = False,
                   max_memory_mb: int = 512,
                   max_timeout: int = 120) -> SandboxInterface:
    """工厂函数——根据环境自动选择沙箱实现。

    Docker 可用 → DockerSandbox
    否则 → 尝试 Windows Job Object (回退到 security/sandbox.py 中的 Sandbox)
    """
    # 优先 Docker
    try:
        r = subprocess.run(['docker', 'info'], capture_output=True, timeout=3)
        if r.returncode == 0:
            return DockerSandbox(
                network_enabled=network_enabled,
                max_memory_mb=max_memory_mb,
                max_timeout=max_timeout,
            )
    except Exception:
        pass

    # 回退 Windows Job Object
    import platform
    if platform.system() == 'Windows':
        from security.sandbox import Sandbox as WinSandbox
        sb = WinSandbox(
            network_enabled=network_enabled,
            max_memory_mb=max_memory_mb,
            max_timeout=max_timeout,
        )
        sb.setup()
        return sb

    # 无沙箱回退
    logger.warning('无可用沙箱后端，使用无隔离模式')
    return _NoOpSandbox()


class _NoOpSandbox(SandboxInterface):
    """无操作沙箱——所有检查通过，不做实际隔离。"""
    def setup(self) -> bool: return True
    def run_command(self, c, d='') -> bool: return True
    def cleanup(self): pass
    def is_active(self) -> bool: return False
    def enforce_path(self, p: str) -> bool: return True
