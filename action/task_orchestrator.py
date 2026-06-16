"""零 · Task Orchestrator
==========================
中控台核心——拆任务、分Agent、追状态、管重试。

修复要点：
  - _llm_decompose：JSON 解析用 extract_first_json（括号计数）
  - 日志：统一 get_logger
  - 避免裸 except，用具体异常
"""

import threading
import time
import uuid
from datetime import datetime

from config import get_logger
from utils.json_helpers import extract_first_json

logger = get_logger('zero.orchestrator')


class Task:
    """一个子任务"""
    def __init__(self, task_id, description, required_capabilities,
                 success_criteria=None, parent_id=None):
        self.id = task_id
        self.description = description
        self.required_capabilities = required_capabilities  # ['code_generation', ...]
        self.success_criteria = success_criteria or f'完成: {description[:50]}'
        self.parent_id = parent_id
        self.agent_id = None
        self.result = None
        self.error = None
        self.created_at = datetime.now().isoformat()
        self.completed_at = None
    
    def to_dict(self):
        return {
            'id': self.id, 'description': self.description,
            'capabilities': self.required_capabilities,
            'success_criteria': self.success_criteria,
            'agent': self.agent_id, 'result': self.result,
            'error': self.error, 'created': self.created_at,
            'completed': self.completed_at
        }


class TaskOrchestrator:
    """任务编排器——中控台核心。
    
    流程:
      用户任务 → decompose → 逐个子任务 → match Agent → execute → 
      record result → 下一个子任务 → 汇总返回
    
    连接 TaskStateMachine(状态追踪) 和 AgentRegistry(能力匹配)。
    """
    
    def __init__(self, state_machine, agent_registry, llm_caller=None, reviewer=None):
        self.tsm = state_machine        # TaskStateMachine
        self.registry = agent_registry  # AgentRegistry
        self.llm = llm_caller           # LLM 调用（用于拆任务）
        self.reviewer = reviewer        # Reviewer（v2: 结果验证）
        self._tasks = {}                # {task_id: Task}
        self._lock = threading.Lock()
    
    # ── 任务拆解 ──
    
    def decompose(self, goal, max_subtasks=5):
        """将大任务拆解为子任务列表。
        
        简单任务不拆（写函数、查资料、聊天等）。
        只有明确的多步骤任务才拆（做个网站、写项目等）。
        """
        # 不拆的情况：太短、单一请求
        if len(goal) < 30 or not any(kw in goal for kw in 
            ['做', '建', '搭', '开发', '项目', '全流程', '整个', '完整']):
            return self._simple_decompose(goal)
        
        if self.llm:
            return self._llm_decompose(goal, max_subtasks)
        else:
            return self._simple_decompose(goal)
    
    def _kanban_log(self, task, status='todo'):
        """自动写入看板。"""
        try:
            from action.kanban import add_task as kb_add, update_status as kb_update
            if status == 'todo':
                kb_id = kb_add(task.description, body=task.success_criteria or '',
                              priority='normal', assignee=task.agent_id or '')
                task._kb_id = kb_id
            else:
                kb_id = getattr(task, '_kb_id', 0)
                if kb_id:
                    kb_update(kb_id, status,
                             result=str(task.result)[:200] if task.result else '',
                             error=str(task.error)[:200] if task.error else '')
        except Exception as exc:
            logger.debug('kanban 不可用: %s', exc)
    
    def _simple_decompose(self, goal):
        """无 LLM 时的简单拆解——整个目标就是一个任务"""
        tid = f'task_{uuid.uuid4().hex[:8]}'
        task = Task(tid, goal, ['chat'])  # 默认聊天能力
        with self._lock:
            self._tasks[tid] = task
        return [task]
    
    def _llm_decompose(self, goal, max_subtasks):
        """让 LLM 拆解任务。"""
        prompt = f"""将以下任务拆解为 {max_subtasks} 步以内的子任务。每步指定需要的能力。

任务: {goal}

可用能力: code_generation, visual_design, search, file_ops, chat, browser_control

输出 JSON:
{{"steps": [{{"step": 1, "action": "做什么", "capability": "能力名"}}]}}"""

        try:
            reply = self.llm(messages=[
                {'role': 'system', 'content': '你是任务拆解器'},
                {'role': 'user', 'content': prompt},
            ])
            plan = extract_first_json(reply)
            if isinstance(plan, dict) and isinstance(plan.get('steps'), list):
                steps = plan['steps']
                tasks = []
                for s in steps[:max_subtasks]:
                    tid = f'task_{uuid.uuid4().hex[:8]}'
                    cap = s.get('capability', 'chat')
                    caps = [cap.strip()] if isinstance(cap, str) else cap
                    task = Task(tid, s.get('action', str(s)), caps)
                    with self._lock:
                        self._tasks[tid] = task
                    tasks.append(task)
                return tasks or self._simple_decompose(goal)
        except Exception as exc:
            logger.warning('LLM decompose 失败: %s', exc)
        return self._simple_decompose(goal)
    
    # ── 任务执行 ──

    def execute_from_subtasks(self, goal: str, subtasks_raw: list) -> dict:
        """从主模型给出的子任务列表直接执行（跳过 LLM 拆解）。"""
        tasks = []
        for s in subtasks_raw:
            if not isinstance(s, dict):
                continue
            tid = f'task_{uuid.uuid4().hex[:8]}'
            cap = s.get('capability', 'chat')
            caps = [cap.strip()] if isinstance(cap, str) else cap
            task = Task(tid, s.get('action', str(s)), caps)
            with self._lock:
                self._tasks[tid] = task
            tasks.append(task)
        if not tasks:
            return self.execute(goal, max_subtasks=4)
        return self._run_tasks(goal, tasks)

    def _run_tasks(self, goal: str, tasks: list) -> dict:
        """执行一组子任务 → 文件写入后处理 → 汇总。"""
        results = []
        for task in tasks:
            self._kanban_log(task, 'todo')
            result = self._execute_one(task)
            results.append(result)
            final_status = 'done' if result['status'] == 'done' else 'blocked'
            self._kanban_log(task, final_status)
            if result['status'] == 'failed' and result.get('no_agent', False):
                break
        return self._finalize(goal, results)

    def execute(self, goal, max_subtasks=5):
        """执行一个完整任务。拆解→逐子任务执行→汇总。"""
        subtasks = self.decompose(goal, max_subtasks)
        return self._run_tasks(goal, subtasks)

    def _finalize(self, goal: str, results: list) -> dict:
        """文件写入后处理 + 汇总。"""
        import re as _ore
        goal_path_m = _ore.search(
            r'(?<![A-Za-z])[A-Za-z]:[\\/][^\s<>"|\n]+\.(?:html|css|js|py|json|txt|md)',
            goal)
        if goal_path_m:
            filepath = goal_path_m.group(0)
            # 找代码最多的结果
            best = max(results, key=lambda r: len(str(r.get('result', ''))), default=None)
            if best:
                task_result = str(best.get('result', ''))
                # 提取代码块：从第一个 ```html 到最后一个 ```
                code_start = task_result.find('```html')
                if code_start == -1:
                    code_start = task_result.find('```')
                if code_start >= 0:
                    # 跳过开头的 ```html\n
                    nl = task_result.find('\n', code_start)
                    code_body = task_result[nl+1:] if nl > 0 else task_result[code_start+3:]
                    # 找最后的 ```
                    last_end = code_body.rfind('```')
                    content = code_body[:last_end].strip() if last_end > 0 else code_body[:10000]
                else:
                    content = task_result[:10000]
                try:
                    from action.tools import execute as _texec
                    wr = _texec('write_file', {'path': filepath, 'content': content})
                    if wr.ok:
                        best['file_written'] = filepath
                        logger.info('orchestrator wrote: %s (%d chars)', filepath, len(content))
                except Exception as exc:
                    logger.warning('orchestrator write error: %s', exc)

        # 3. 汇总
        done = sum(1 for r in results if r['status'] == 'done')
        failed = sum(1 for r in results if r['status'] == 'failed')
        written = [r.get('file_written') for r in results if r.get('file_written')]

        summary = f'{len(results)}个子任务, {done}完成, {failed}失败'
        if written:
            summary += f', 已写入: {", ".join(written)}'

        return {
            'status': 'done' if failed == 0 else ('partial' if done > 0 else 'failed'),
            'results': results,
            'summary': summary,
            'subtasks': len(results),
            'completed': done,
            'failed': failed,
        }
    
    def _execute_one(self, task):
        """执行单个子任务——匹配Agent→执行→记录结果。
        
        统一使用 Result 协议。
        """
        self.tsm.transition(task.id, 'task.created')
        
        # 1. 匹配 Agent
        agent = self.registry.match(task.required_capabilities,
                                     prefer_free=True, task_id=task.id)
        if not agent:
            self.tsm.transition(task.id, 'task.failed')
            return {'status': 'failed', 'task_id': task.id,
                    'error': '没有可用的Agent', 'no_agent': True}
        
        task.agent_id = agent['id']
        self.tsm.transition(task.id, 'task.assigned')
        self.tsm.transition(task.id, 'task.started')
        
        # 2. 执行（返回 Result）
        try:
            call_result = self._call_agent(agent, task)
            if call_result.ok:
                task.result = str(call_result.data) if call_result.data else ''
                task.completed_at = datetime.now().isoformat()
                
                # Reviewer 验证
                if self.reviewer:
                    self.tsm.transition(task.id, 'task.completed')
                    self.tsm.transition(task.id, 'task.reviewing')
                    verdict = self.reviewer.review(task, agent)
                    if not verdict.get('passed'):
                        self.registry.record_result(agent['id'], False)
                        self.tsm.transition(task.id, 'task.review_failed')
                        return {'status': 'failed', 'task_id': task.id,
                                'error': 'Reviewer不通过: ' + verdict.get('reason', '未知'),
                                'score': verdict.get('score', 0),
                                'agent': agent['name']}
                    self.tsm.transition(task.id, 'task.review_passed')
                    review_score = verdict.get('score')
                else:
                    review_score = None
                
                self.registry.record_result(agent['id'], True)
                # task 已在 review_passed 状态，无需再跳 completed
                return {'status': 'done', 'task_id': task.id,
                        'agent': agent['name'], 'result': task.result,
                        'review_score': review_score}
            else:
                self.registry.record_result(agent['id'], False)
                err_msg = call_result.error.message if call_result.error else '未知错误'
                task.error = err_msg
                self.tsm.transition(task.id, 'task.failed')
                return {'status': 'failed', 'task_id': task.id,
                        'error': err_msg, 'agent': agent['name']}
        except Exception as e:
            from utils.result import ErrorCode  # noqa: WPS433
            self.registry.record_result(agent['id'], False)
            error_code = ErrorCode.INTERNAL
            task.error = str(e)
            self.tsm.transition(task.id, 'task.failed')
            return {'status': 'failed', 'task_id': task.id,
                    'error': str(e), 'agent': agent['name'],
                    'error_code': error_code}
    
    def _call_agent(self, agent, task):
        """调用 Agent 执行任务。返回统一的 Result 信封。
        
        executor 签名: fn(prompt, caps, extra) -> str
        """
        from utils.result import ok, err, ErrorCode

        executor = agent.get('executor')
        if executor:
            try:
                reply = executor(task.description, task.required_capabilities, {})
                return ok(reply)
            except Exception as e:
                return err(ErrorCode.MODEL_UNAVAILABLE, f'Agent执行异常: {e}',
                           retryable=True)

        # 回退：通过 LLM caller
        if self.llm:
            try:
                reply = self.llm(messages=[
                    {'role': 'system', 'content': '你是零的执行Agent'},
                    {'role': 'user', 'content': task.description},
                ])
                return ok(reply)
            except Exception as e:
                return err(ErrorCode.MODEL_UNAVAILABLE, f'LLM调用失败: {e}',
                           retryable=True)

        return err(ErrorCode.MODEL_UNAVAILABLE,
                   'Agent未配置executor且无LLM')
    
    # ── 状态查询 ──
    
    def status(self):
        return {
            'tasks_total': len(self._tasks),
            'tasks_active': sum(1 for t in self._tasks.values() 
                               if self.tsm.get_state(t.id) not in (
                                   'task.review_passed', 'task.cancelled', None)),
            'state_summary': self.tsm.get_all(),
        }
