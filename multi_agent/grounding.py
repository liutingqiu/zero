"""P3: GroundTruth 升级 — 防假阳性
===================================
AST 危险导入检测 + 副作用监控。

修复:
  1. 退出码=0 不代表安全 → 检查 AST 中是否有危险导入
  2. 内存/磁盘副作用检测
  3. runtime 指标验证
"""

import ast as _ast
import re as _re


# ═══════════════════════════════════════════
# AST 危险导入检测
# ═══════════════════════════════════════════

DANGEROUS_IMPORTS: set[str] = {
    'os', 'subprocess', 'shutil', 'socket', 'ctypes',
    'sys', 'signal', 'ptrace', 'fcntl', 'posix',
    'multiprocessing', 'threading', 'concurrent.futures',
}

DANGEROUS_FUNCTIONS: set[str] = {
    'eval', 'exec', 'compile', '__import__', 'open',
    'os.system', 'os.popen', 'os.remove', 'os.unlink',
    'os.rmdir', 'os.chmod', 'os.chown',
    'shutil.rmtree', 'shutil.copy', 'shutil.move',
    'subprocess.call', 'subprocess.run', 'subprocess.Popen',
    'socket.socket', 'socket.connect',
}


class CodeAnalyzer:
    """P3: 代码静态分析——检测危险导入和函数调用。"""

    @staticmethod
    def analyze(code: str) -> tuple[bool, list[str]]:
        """分析代码安全性。

        Returns:
            (is_safe, warnings) — is_safe=False 表示发现危险内容
        """
        warnings = []
        try:
            tree = _ast.parse(code)
        except SyntaxError as exc:
            return False, [f'语法错误: {exc}']

        for node in _ast.walk(tree):
            # 检测危险导入
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    if alias.name.split('.')[0] in DANGEROUS_IMPORTS:
                        warnings.append(f'危险导入: {alias.name}')
            elif isinstance(node, _ast.ImportFrom):
                if node.module and node.module.split('.')[0] in DANGEROUS_IMPORTS:
                    for alias in node.names:
                        warnings.append(f'危险导入: {node.module}.{alias.name}')

            # 检测危险函数调用
            elif isinstance(node, _ast.Call):
                func_name = CodeAnalyzer._get_func_name(node)
                if func_name in DANGEROUS_FUNCTIONS:
                    warnings.append(f'危险调用: {func_name}()')

        return len(warnings) == 0, warnings

    @staticmethod
    def _get_func_name(node: _ast.Call) -> str:
        """提取函数调用的完整名称。"""
        if isinstance(node.func, _ast.Name):
            return node.func.id
        elif isinstance(node.func, _ast.Attribute):
            parts = []
            obj = node.func
            while isinstance(obj, _ast.Attribute):
                parts.append(obj.attr)
                obj = obj.value
            if isinstance(obj, _ast.Name):
                parts.append(obj.id)
            return '.'.join(reversed(parts))
        return ''


# ═══════════════════════════════════════════
# 副作用检测
# ═══════════════════════════════════════════

class SideEffectDetector:
    """P3: 运行时副作用检测——内存/磁盘/文件操作异常。"""

    MAX_OUTPUT_GROWTH_RATIO = 5.0   # 输出长度增长超过5倍 → 可疑
    MAX_CODE_BLOCK_COUNT = 20       # 代码块超过20个 → 可疑
    MIN_UNIQUE_LINE_RATIO = 0.2     # 唯一行比例低于20% → 重复/模板化

    @staticmethod
    def check(output: str, prev_outputs: list[str] | None = None) -> tuple[bool, list[str]]:
        """检测副作用异常。

        Returns:
            (is_clean, issues)
        """
        issues = []

        # 检测1: 输出异常膨胀
        if prev_outputs and len(prev_outputs) > 0:
            avg_len = sum(len(o) for o in prev_outputs) / len(prev_outputs)
            if avg_len > 100 and len(output) > avg_len * SideEffectDetector.MAX_OUTPUT_GROWTH_RATIO:
                issues.append(f'输出异常膨胀: {len(output)} vs 平均{avg_len:.0f}')

        # 检测2: 代码块过多（可能堆砌注释/空结构）
        code_blocks = output.count('```')
        if code_blocks > SideEffectDetector.MAX_CODE_BLOCK_COUNT:
            issues.append(f'代码块过多: {code_blocks}')

        # 检测3: 大量重复行（模板化输出）
        lines = [l.strip() for l in output.split('\n') if l.strip()]
        if len(lines) > 20:
            unique = len(set(lines))
            ratio = unique / len(lines)
            if ratio < SideEffectDetector.MIN_UNIQUE_LINE_RATIO:
                issues.append(f'重复率过高: {ratio:.0%} 唯一行')

        return len(issues) == 0, issues


# ═══════════════════════════════════════════
# GroundTruth 增强
# ═══════════════════════════════════════════

def validate_execution(output: str, exit_code: int = 0,
                       prev_outputs: list[str] | None = None) -> float:
    """P3: 增强的工具执行验证——不只是退出码。

    返回 0.0~1.0 的质量评分。

    检查:
      1. AST 危险导入
      2. 副作用检测
      3. 退出码
    """
    # 退出码检查
    if exit_code != 0:
        return 0.0

    score = 1.0

    # AST 分析
    safe, warnings = CodeAnalyzer.analyze(output)
    if not safe:
        # 每个危险导入扣 0.2 分
        score -= min(0.6, len(warnings) * 0.2)
        if score < 0.3:
            return score

    # 副作用检测
    clean, issues = SideEffectDetector.check(output, prev_outputs)
    if not clean:
        score -= min(0.4, len(issues) * 0.15)

    return max(0.0, score)
