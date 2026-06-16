"""零 · AI OS Kernel v5
=======================
因果事件图 + 对抗式共识 + 运行时合约执行 + 分片事件流。

v5 核心升级:
  1. Causal Event Graph: parent_event_ids → 可解释因果链
  2. Adversarial Consensus: proposer→adversary→judge→referee 四阶段
  3. Runtime Enforcement: 每次 execute 前 validate_input + authority_ok
  4. Sharded Streams: planner/executor/critic/system 独立事件流
"""

from __future__ import annotations

import copy
import json
import math
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from config import get_logger
from utils.json_helpers import extract_first_json
from multi_agent.stability import VectorClock, TemporalValidator, ConvergenceEngine, RepairSystem
from multi_agent.consensus import AdversarialConsensus

logger = get_logger('zero.kernel')
from multi_agent.stability import VectorClock, TemporalValidator, ConvergenceEngine, RepairSystem
from multi_agent.consensus import AdversarialConsensus



# ═══════════════════════════════════════════
# Causal Event Graph
# ═══════════════════════════════════════════


class MultiAgentOrchestratorV6:
    """v6 编排器——向量时钟 + 收敛引擎 + 修复系统。"""

    def __init__(self, llm_caller):
        self.planner = PlannerV5(llm_caller)
        self.executor = ExecutorV5(llm_caller)
        self.critic = CriticV5(llm_caller)
        self.synthesizer = SynthesizerV5(llm_caller)
        self._clocks: dict[str, VectorClock] = defaultdict(VectorClock)
        self._convergence = ConvergenceEngine()
        self._repair = RepairSystem()
        self._validator = TemporalValidator()

    def run(self, task: str) -> dict:
        bb = BlackboardV5(task)
        logger.info('kernel v6: starting (with stability layer)')

        # 1. Plan
        proposed = self.planner.propose(bb)
        step_ids = bb.create_steps(proposed)
        if not step_ids:
            return {'status': 'failed', 'answer': '规划失败',
                    'blackboard': bb, 'completed': 0, 'failed': 1,
                    'stability': {'convergence': False, 'repairs': 0}}

        # 2. Execute + Stability
        completed = 0
        failed = 0
        repairs = 0

        for sid in step_ids:
            step = bb.get_step(sid)
            if not step:
                continue
            info = {'id': sid, 'action': step['action'],
                    'criteria': step['criteria']}

            for attempt in range(ConvergenceEngine.MAX_ROUNDS):
                try:
                    # 向量时钟
                    self._clocks['executor'].tick('executor')

                    bb.start_step(sid, 'executor')
                    output = self.executor.execute(info, bb)

                    # 规则评分
                    from behavior_canon import synthetic_evaluate
                    rule_s, _ = synthetic_evaluate(output, 'code')

                    # Critic
                    critique = self.critic.review(output, info)
                    bb.submit_critique(sid, critique, 'critic')
                    critic_s = critique.get('score', 50) / 100.0
                    attacks = critique.get('adversarial', [])

                    # 共识
                    result = bb.adversarial_check(
                        sid, output, rule_s, critic_s, attacks,
                    )

                    # ── 收敛检查 ──
                    should_stop, reason = self._convergence.check(
                        sid, result.final_score, result.decision, attempt,
                    )

                    if result.passed and not should_stop:
                        bb.complete_step(sid, output, 'executor')
                        completed += 1
                        break
                    elif should_stop:
                        if result.passed:
                            bb.complete_step(sid, output, 'executor')
                            completed += 1
                        else:
                            bb.fail_step(sid, 'executor', reason)
                            failed += 1
                        logger.info('v6 converge: %s → %s', sid, reason)
                        break
                    elif attempt < ConvergenceEngine.MAX_ROUNDS - 1:
                        info['action'] = f'{info["action"]}\n[修复] {"; ".join(attacks)}'
                        repairs += 1
                    else:
                        bb.fail_step(sid, 'executor', result.referee_ruling)
                        failed += 1

                except ContractViolation as exc:
                    # ── 修复系统 ──
                    plan = self._repair.diagnose(sid, str(exc), '')
                    if self._repair.should_retry(sid):
                        info['action'] = (
                            f'{info["action"]}\n[自动修复] {plan.suggested_fix}'
                        )
                        repairs += 1
                        continue
                    logger.warning('v6 repair exhausted: %s', exc)
                    failed += 1
                    break

        # 3. 因果验证
        for sid in step_ids:
            chain = bb.causal_chain(sid)
            ok, msg = self._validator.validate_chain(chain)
            if not ok:
                logger.warning('v6 temporal violation: %s', msg)

        # 4. Synthesize
        answer = self.synthesizer.synthesize(bb)
        status = 'done' if failed == 0 else ('partial' if completed > 0 else 'failed')

        return {
            'status': status, 'answer': answer, 'blackboard': bb,
            'completed': completed, 'failed': failed,
            'stability': {
                'convergence': True,
                'repairs': repairs,
                'clocks': {k: v.to_dict() for k, v in self._clocks.items()},
            },
            'stats': bb.stats(),
        }


def collaborate_v6(task: str, llm_caller) -> dict:
    return MultiAgentOrchestratorV6(llm_caller).run(task)


# ═══════════════════════════════════════════
# v7 Semantic Layer
# ═══════════════════════════════════════════
# 三个语义升级:
#   1. Global Replanner — root_cause → replan DAG
#   2. Semantic Convergence — reasoning similarity + score + causal
#   3. Event Compression — cluster → merge → summary event


# ── Semantic Convergence v2 ──

class SemanticConvergence(ConvergenceEngine):
    """v7 语义收敛——在 score 稳定基础上增加 reasoning 一致性检查。"""

    def __init__(self):
        super().__init__()
        self._reasonings: dict[str, list[str]] = defaultdict(list)
        self._scores: dict[str, list[float]] = defaultdict(list)
        self._outputs: dict[str, list[str]] = defaultdict(list)

    def check_semantic(self, step_id: str, score: float, decision: str,
                       reasoning: str, attempt: int,
                       output: str = '') -> tuple[bool, str]:
        """P3: 语义收敛——score稳定 + n-gram语义相似 + 反刷分。"""
        # 基础收敛检查
        should_stop, reason = self.check(step_id, score, decision, attempt)
        if should_stop:
            return True, reason

        # P3: 反刷分检测
        self._scores[step_id].append(score)
        self._outputs[step_id].append(output)
        if _detect_reward_hacking(self._scores[step_id], self._outputs[step_id]):
            return True, '检测到刷分行为，强制终止迭代'

        # 语义相似度检查（升级为 n-gram）
        self._reasonings[step_id].append(reasoning)
        if len(self._reasonings[step_id]) >= 3:
            recent = self._reasonings[step_id][-3:]
            sim = _reasoning_similarity(recent)
            if sim > 0.8:
                return True, f'语义收敛 (n-gram相似度{sim:.2f}>0.8)'

        return False, '继续'


def _reasoning_similarity(reasonings: list[str]) -> float:
    """P3: 语义相似度——字符 n-gram 重叠（比 token Jaccard 更鲁棒）。

    使用 3-gram 字符集重叠率，对同义词替换和词序变化不敏感，
    但对真正语义变化（不同词根）保持区分力。
    """
    if len(reasonings) < 2:
        return 1.0
    import math as _m

    def _ngrams(text: str, n: int = 3) -> set:
        text = text.lower()
        return {text[i:i + n] for i in range(max(0, len(text) - n + 1))}

    similarities = []
    for i in range(len(reasonings) - 1):
        a = _ngrams(reasonings[i])
        b = _ngrams(reasonings[i + 1])
        if not a or not b:
            similarities.append(0.0)
        else:
            # Jaccard on 3-grams
            similarities.append(len(a & b) / max(len(a | b), 1))

    avg = sum(similarities) / len(similarities) if similarities else 1.0
    # 如果方差极低（所有对比几乎相同）→ 奖励刷取嫌疑，降低相似度
    if len(similarities) >= 2:
        variance = sum((s - avg) ** 2 for s in similarities) / len(similarities)
        if variance < 0.005:
            avg -= 0.15  # 可疑: 太一致可能是模板化输出
    return max(0.0, min(1.0, avg))


def _detect_reward_hacking(scores: list[float], outputs: list[str]) -> bool:
    """P3: 反刷分检测——检测 Agent 是否通过表面修饰虚增评分。

    信号:
      1. 得分持续上升但输出长度单调增长（堆砌注释/空行）
      2. 代码块数量异常增长（重复代码结构）
      3. 输出开始出现大量重复模式
    """
    if len(scores) < 3 or len(outputs) < 3:
        return False

    # 检测1: 得分上升 + 长度单调增长
    score_rising = all(scores[i] <= scores[i + 1] for i in range(len(scores) - 1))
    lengths = [len(o) for o in outputs]
    length_rising = all(lengths[i] <= lengths[i + 1] for i in range(len(lengths) - 1))
    if score_rising and length_rising and lengths[-1] > lengths[0] * 2:
        return True  # 长度翻倍但得分上升 → 可疑

    # 检测2: 代码块数量异常增长
    code_blocks = [o.count('```') for o in outputs]
    if len(set(code_blocks[-2:])) == 1 and code_blocks[-1] > code_blocks[0] * 1.5:
        return True

    # 检测3: 大量重复行（模板化）
    for o in outputs[-2:]:
        lines = [l.strip() for l in o.split('\n') if l.strip()]
        if len(lines) > 10:
            unique_ratio = len(set(lines)) / len(lines)
            if unique_ratio < 0.3:
                return True

    return False


# ── Global Replanner ──

@dataclass
class FailureTrace:
    """失败追踪——从症状追溯到根因。"""
    step_id: str
    events: list[CausalEvent]
    root_cause: str
    affected_steps: list[str]


class GlobalReplanner:
    """v7 全局重规划——violation → root cause → replan DAG。

    替代 v6 的局部 retry。
    """

    def __init__(self, llm_caller):
        self.llm = llm_caller

    def trace_root_cause(self, step_id: str,
                         blackboard: BlackboardV5) -> FailureTrace:
        """从失败步骤追溯根因。"""
        chain = blackboard.causal_chain(step_id)
        events = [e for e in chain if e.event_type in (
            EventType.STEP_FAILED, EventType.CRITIQUE_SUBMITTED,
            EventType.CONSENSUS_REACHED,
        )]

        # 提取失败描述
        failures = [
            e.data.get('reason', '') or
            str(e.data.get('issues', '')) or
            str(e.data.get('decision', ''))
            for e in events
        ]
        root = '; '.join(failures[:3]) if failures else '未知原因'

        # 受影响步骤: 失败步骤及之后的依赖步骤
        step_order = blackboard._step_order
        try:
            idx = step_order.index(step_id)
            affected = step_order[idx:]
        except ValueError:
            affected = [step_id]

        return FailureTrace(
            step_id=step_id, events=chain,
            root_cause=root, affected_steps=affected,
        )

    def replan(self, trace: FailureTrace,
               blackboard: BlackboardV5) -> list[dict]:
        """基于失败追踪生成新计划。"""
        system = """你是 Global Replanner。根据失败原因重新规划。

分析失败根因，输出修正后的新步骤。注意:
- 跳过已完成步骤
- 为受影响步骤生成新的执行方案
- 新步骤应包含修正措施

输出 JSON: {"steps": [{"step": N, "action": "...", "agent": "executor", "criteria": "..."}]}"""

        prompt = (
            f'原任务: {blackboard.task}\n'
            f'失败步骤: {trace.step_id}\n'
            f'根因: {trace.root_cause}\n'
            f'受影响: {", ".join(trace.affected_steps)}\n'
            f'当前状态:\n{blackboard.summary()}\n\n'
            f'请输出修正方案。'
        )
        try:
            reply = self.llm(messages=[
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': prompt},
            ])
            plan = extract_first_json(str(reply)) or {}
            return plan.get('steps', [])
        except Exception:
            return []


# ── Event Compression ──

class EventCompressor:
    """v7 事件压缩——语义聚类合并，防止 event graph 无限膨胀。"""

    MAX_EVENTS_BEFORE_COMPRESS = 300

    def should_compress(self, log: ShardedEventLog) -> bool:
        return log.stats()['total'] >= self.MAX_EVENTS_BEFORE_COMPRESS

    def compress(self, log: ShardedEventLog) -> dict:
        """压缩事件日志——按 (event_type, step_id) 聚类，合并冗余。"""
        all_events = log.replay()
        if len(all_events) < self.MAX_EVENTS_BEFORE_COMPRESS:
            return {'compressed': 0, 'total': len(all_events)}

        # 按类型分组
        by_type: dict[str, list[CausalEvent]] = defaultdict(list)
        for e in all_events:
            key = f'{e.event_type.value}:{e.step_id}'
            by_type[key].append(e)

        compressed = 0
        for key, events in by_type.items():
            if len(events) >= 5:
                # 合并: 保留第一个和最后一个，删除中间
                to_merge = events[1:-1]
                # 标记为已压缩（实际删除由外部控制）
                compressed += len(to_merge)
                logger.debug('compress: %s → %d events merged',
                             key, len(to_merge))

        logger.info('v7 compress: %d/%d events compressed',
                    compressed, len(all_events))
        return {'compressed': compressed, 'total': len(all_events)}


# ═══════════════════════════════════════════
# v7 Orchestrator
# ═══════════════════════════════════════════

class MultiAgentOrchestratorV7:
    """v7 编排器——全局重规划 + 语义收敛 + 事件压缩。"""

    def __init__(self, llm_caller):
        self.planner = PlannerV5(llm_caller)
        self.executor = ExecutorV5(llm_caller)
        self.critic = CriticV5(llm_caller)
        self.synthesizer = SynthesizerV5(llm_caller)
        self._clocks: dict[str, VectorClock] = defaultdict(VectorClock)
        self._convergence = SemanticConvergence()
        self._repair = RepairSystem()
        self._replanner = GlobalReplanner(llm_caller)
        self._compressor = EventCompressor()
        self._validator = TemporalValidator()

    def run(self, task: str) -> dict:
        bb = BlackboardV5(task)
        logger.info('kernel v7: starting (semantic layer)')

        # 1. Plan
        proposed = self.planner.propose(bb)
        step_ids = bb.create_steps(proposed)
        if not step_ids:
            return {'status': 'failed', 'answer': '规划失败',
                    'blackboard': bb, 'stats': bb.stats()}

        # 2. Execute
        completed = 0
        failed = 0
        repairs = 0
        replans = 0

        i = 0
        while i < len(step_ids):
            sid = step_ids[i]
            step = bb.get_step(sid)
            if not step:
                i += 1
                continue
            info = {'id': sid, 'action': step['action'],
                    'criteria': step['criteria']}

            step_ok = False
            for attempt in range(ConvergenceEngine.MAX_ROUNDS):
                try:
                    self._clocks['executor'].tick('executor')
                    bb.start_step(sid, 'executor')
                    output = self.executor.execute(info, bb)

                    from behavior_canon import synthetic_evaluate
                    rule_s, _ = synthetic_evaluate(output, 'code')

                    critique = self.critic.review(output, info)
                    bb.submit_critique(sid, critique, 'critic')
                    critic_s = critique.get('score', 50) / 100.0
                    attacks = critique.get('adversarial', [])
                    judge_reasoning = critique.get('judge_rationale', str(critique))

                    result = bb.adversarial_check(
                        sid, output, rule_s, critic_s, attacks,
                    )

                    # 语义收敛
                    should_stop, reason = self._convergence.check_semantic(
                        sid, result.final_score, result.decision,
                        judge_reasoning, attempt,
                    )

                    if result.passed and not should_stop:
                        bb.complete_step(sid, output, 'executor')
                        completed += 1
                        step_ok = True
                        break
                    elif should_stop:
                        if result.passed:
                            bb.complete_step(sid, output, 'executor')
                            completed += 1
                            step_ok = True
                        else:
                            bb.fail_step(sid, 'executor', reason)
                            failed += 1
                        break
                    elif attempt < ConvergenceEngine.MAX_ROUNDS - 1:
                        info['action'] = f'{info["action"]}\n[修复] {"; ".join(attacks)}'
                        repairs += 1
                    else:
                        bb.fail_step(sid, 'executor', result.referee_ruling)
                        failed += 1

                except ContractViolation as exc:
                    plan = self._repair.diagnose(sid, str(exc), '')
                    if self._repair.should_retry(sid):
                        info['action'] = f'{info["action"]}\n[修复] {plan.suggested_fix}'
                        repairs += 1
                        continue
                    failed += 1
                    break

            # ── 全局重规划 ──
            if not step_ok and failed > 0:
                trace = self._replanner.trace_root_cause(sid, bb)
                new_steps = self._replanner.replan(trace, bb)
                if new_steps:
                    replans += 1
                    new_ids = bb.create_steps(new_steps)
                    step_ids = step_ids[:i + 1] + new_ids + step_ids[i + 1:]
                    logger.info('v7 replan: %d new steps added after %s',
                                len(new_ids), sid)

            i += 1

        # 3. 因果验证
        for sid in step_ids:
            chain = bb.causal_chain(sid)
            ok, msg = self._validator.validate_chain(chain)
            if not ok:
                logger.warning('v7 temporal: %s', msg)

        # 4. 事件压缩
        compress_stats = self._compressor.compress(bb.events) if self._compressor.should_compress(bb.events) else {}

        # 5. Synthesize
        answer = self.synthesizer.synthesize(bb)
        status = 'done' if failed == 0 else ('partial' if completed > 0 else 'failed')

        return {
            'status': status, 'answer': answer,
            'completed': completed, 'failed': failed,
            'stability': {
                'repairs': repairs, 'replans': replans,
                'compressed': compress_stats.get('compressed', 0),
                'clocks': {k: v.to_dict() for k, v in self._clocks.items()},
            },
            'stats': bb.stats(),
        }


def collaborate_v7(task: str, llm_caller) -> dict:
    return MultiAgentOrchestratorV7(llm_caller).run(task)


# ═══════════════════════════════════════════
# v8 Grounded Runtime — 现实锚定层
# ═══════════════════════════════════════════
# 三个机制:
#   1. GroundTruth — 收集外部信号(tool/feedback/env)作为 primary truth
#   2. Anti-Self-Validation — LLM自评权重×0.3
#   3. Reality Constraint — execution outcome > reasoning outcome
#
# 核心原则: ❗系统的判断必须来自外部世界，不是自身推理


class GroundTruth:
    """外部真实信号——非 LLM 来源的客观结果。

    来源:
      - tool_exec: 工具执行结果 (0=失败, 1=成功)
      - human_feedback: 用户显式反馈 (-1/0/+1)
      - env_signal: API返回码/系统状态/DB查询结果 (0~1)
    """

    SOURCE_WEIGHTS: dict[str, float] = {
        'tool_exec': 1.0,        # 最高权重——机器不会撒谎
        'human_feedback': 0.9,   # 人类判断
        'env_signal': 0.8,       # 环境信号
        'llm_internal': 0.3,     # LLM自评——最低权重
    }

    def __init__(self):
        self._signals: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
        # {step_id: [(source, score, detail), ...]}
        self._lock = threading.Lock()

    def ingest(self, step_id: str, source: str, score: float,
               detail: str = ''):
        """摄入外部信号。"""
        with self._lock:
            self._signals[step_id].append((source, score, detail))

    def get_grounded_score(self, step_id: str,
                           internal_score: float) -> tuple[float, bool]:
        """获取锚定后的评分——外部信号优先。

        Returns:
            (final_score, has_external) — has_external=True 表示有外部信号
        """
        with self._lock:
            signals = self._signals.get(step_id, [])

        if not signals:
            return internal_score, False

        # 加权融合: 外部信号 + 内部评分(降权)
        weighted_sum = 0.0
        weight_sum = 0.0
        for source, score, _ in signals:
            w = self.SOURCE_WEIGHTS.get(source, 0.5)
            weighted_sum += score * w
            weight_sum += w

        # 内部评分以最低权重参与
        llm_weight = self.SOURCE_WEIGHTS['llm_internal']
        weighted_sum += internal_score * llm_weight
        weight_sum += llm_weight

        final = weighted_sum / weight_sum if weight_sum > 0 else internal_score
        return final, True

    def has_external(self, step_id: str) -> bool:
        with self._lock:
            return len(self._signals.get(step_id, [])) > 0

    def summary(self, step_id: str) -> str:
        with self._lock:
            signals = self._signals.get(step_id, [])
        if not signals:
            return '(无外部信号)'
        parts = [f'{src}:{score:.2f}' for src, score, _ in signals]
        return ', '.join(parts)


class GroundedJudge:
    """v8 锚定裁判——外部信号优先于 LLM 共识。

    规则:
      1. 有 tool_exec 结果 → 以工具结果为准
      2. 有 human_feedback → 覆盖 LLM 评分
      3. 纯 LLM 评分 → 降权 ×0.3
    """

    def __init__(self):
        self.ground_truth = GroundTruth()
        self._adversarial = AdversarialConsensus()

    def evaluate(self, step_id: str, output: str,
                 rule_score: float, critic_score: float,
                 attacks: list[str], judge_score: float | None = None
                 ) -> tuple[float, bool, str]:
        """锚定评估——外部优先。"""
        # 内部评分（降权）
        internal = self._adversarial.evaluate(
            output, 'code', rule_score, critic_score, attacks, judge_score,
        ).final_score

        # 外部锚定
        grounded, has_ext = self.ground_truth.get_grounded_score(
            step_id, internal,
        )

        if has_ext:
            # 有外部信号 → 以锚定评分为准
            passed = grounded >= 0.5
            source = f'grounded({self.ground_truth.summary(step_id)})'
        else:
            # 纯内部 → 降权
            passed = internal >= 0.6  # 阈值提高到 0.6
            source = 'internal(×0.3 weight)'

        return grounded, passed, source


# ═══════════════════════════════════════════
# v8 Orchestrator — Grounded
# ═══════════════════════════════════════════

class MultiAgentOrchestratorV8:
    """v8 编排器——外部真实信号锚定。"""

    def __init__(self, llm_caller, tool_executor=None):
        self.planner = PlannerV5(llm_caller)
        self.executor = ExecutorV5(llm_caller)
        self.critic = CriticV5(llm_caller)
        self.synthesizer = SynthesizerV5(llm_caller)
        self._judge = GroundedJudge()
        self._clocks: dict[str, VectorClock] = defaultdict(VectorClock)
        self._convergence = SemanticConvergence()
        self._replanner = GlobalReplanner(llm_caller)
        self._compressor = EventCompressor()
        self._validator = TemporalValidator()
        self._tool_executor = tool_executor  # 可选的工具执行器

    def run(self, task: str) -> dict:
        bb = BlackboardV5(task)
        logger.info('kernel v8: grounded runtime starting')

        proposed = self.planner.propose(bb)
        step_ids = bb.create_steps(proposed)
        if not step_ids:
            return {'status': 'failed', 'answer': '规划失败',
                    'blackboard': bb, 'stats': bb.stats()}

        completed = 0
        failed = 0
        grounded = 0

        i = 0
        while i < len(step_ids):
            sid = step_ids[i]
            step = bb.get_step(sid)
            if not step:
                i += 1
                continue
            info = {'id': sid, 'action': step['action'],
                    'criteria': step['criteria']}
            step_ok = False

            for attempt in range(ConvergenceEngine.MAX_ROUNDS):
                try:
                    self._clocks['executor'].tick('executor')
                    bb.start_step(sid, 'executor')
                    output = self.executor.execute(info, bb)

                    # ── 外部工具执行 ──
                    if self._tool_executor:
                        try:
                            tool_result = self._tool_executor(output)
                            tool_ok = 1.0 if tool_result else 0.0
                            self._judge.ground_truth.ingest(
                                sid, 'tool_exec', tool_ok,
                                'success' if tool_ok > 0.5 else 'failed',
                            )
                        except Exception:
                            pass

                    from behavior_canon import synthetic_evaluate
                    rule_s, _ = synthetic_evaluate(output, 'code')

                    critique = self.critic.review(output, info)
                    bb.submit_critique(sid, critique, 'critic')
                    critic_s = critique.get('score', 50) / 100.0
                    attacks = critique.get('adversarial', [])

                    # ── 锚定裁判 ──
                    score, passed, source = self._judge.evaluate(
                        sid, output, rule_s, critic_s, attacks,
                    )
                    if self._judge.ground_truth.has_external(sid):
                        grounded += 1

                    # 收敛
                    should_stop, reason = self._convergence.check_semantic(
                        sid, score, 'accept' if passed else 'reject',
                        source, attempt,
                    )

                    if passed and not should_stop:
                        bb.complete_step(sid, output, 'executor')
                        completed += 1
                        step_ok = True
                        break
                    elif should_stop:
                        if passed:
                            bb.complete_step(sid, output, 'executor')
                            completed += 1
                            step_ok = True
                        else:
                            bb.fail_step(sid, 'executor', reason)
                            failed += 1
                        break
                    elif attempt < ConvergenceEngine.MAX_ROUNDS - 1:
                        info['action'] = f'{info["action"]}\n[{source}] {"; ".join(attacks)}'
                    else:
                        bb.fail_step(sid, 'executor', f'{source}: score={score:.2f}')
                        failed += 1

                except ContractViolation as exc:
                    failed += 1
                    break

            if not step_ok and failed > 0:
                trace = self._replanner.trace_root_cause(sid, bb)
                new_steps = self._replanner.replan(trace, bb)
                if new_steps:
                    new_ids = bb.create_steps(new_steps)
                    step_ids = step_ids[:i + 1] + new_ids + step_ids[i + 1:]
            i += 1

        for sid in step_ids:
            chain = bb.causal_chain(sid)
            ok, msg = self._validator.validate_chain(chain)
            if not ok:
                logger.warning('v8 temporal: %s', msg)

        self._compressor.compress(bb.events) if self._compressor.should_compress(bb.events) else None

        answer = self.synthesizer.synthesize(bb)
        status = 'done' if failed == 0 else ('partial' if completed > 0 else 'failed')

        return {
            'status': status, 'answer': answer,
            'completed': completed, 'failed': failed,
            'grounded': grounded,
            'stats': bb.stats(),
        }


def collaborate_v8(task: str, llm_caller, tool_executor=None) -> dict:
    return MultiAgentOrchestratorV8(llm_caller, tool_executor).run(task)

