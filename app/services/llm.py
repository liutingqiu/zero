"""零 · 核心服务层
================
LLM 调用、消息处理、Token 管理。

从 zero_server.py 剥离——P0 单体拆分。
"""

import json
import os
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime

from config import (
    AGNES_API_URL, DATA_DIR, HTTP_HOST, HTTP_PORT,
    MEMORY_DB, UNLOCK_DURATION_SECONDS, ZERO_ROOT,
    get_agnes_key, get_api_key, get_api_url, get_logger,
)
from utils.json_helpers import extract_first_json
from utils.text_helpers import truncate

os.chdir(ZERO_ROOT)
logger = get_logger('zero.service')

from message_bus import TaskStateMachine, get_bus
from security.guard import SessionManager, detect_jailbreak
from cognition import memory_manager
from cognition.working_memory import WorkingMemory
from action.agent_loop import AgentLoop
from action.agent_registry import AgentRegistry, seed_defaults
from action.reviewer import Reviewer
from action.task_orchestrator import TaskOrchestrator
from action.tools import execute as tool_execute

AGNES_MODELS = {
    'text_fast': 'agnes-1.5-flash', 'text': 'agnes-2.0-flash',
    'image': 'agnes-image-2.1-flash', 'image_old': 'agnes-image-2.0-flash',
    'video': 'agnes-video-v2.0',
}

def _select_agnes_model(task_type='text'):
    if task_type in ('image', 'image_generation'): return AGNES_MODELS['image']
    if task_type in ('video', 'video_generation'): return AGNES_MODELS['video']
    return AGNES_MODELS['text']

def _post_json(url, payload_dict, api_key, timeout=30):
    payload = json.dumps(payload_dict, ensure_ascii=False).encode('utf-8')
    req = __import__('urllib.request').Request(url, data=payload, headers={
        'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}, method='POST')
    with __import__('urllib.request').urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))

def call_llm(system=None, prompt=None, *, messages=None,
             prefer_free=True, task_type='text', timeout=30,
             task_text='', extra_rules='', agent_id='', skip_ground=False):
    if messages is not None:
        msgs = list(messages)
    else:
        msgs = [{'role':'system','content':system or ''},{'role':'user','content':prompt or ''}]

    from semantic_gateway import process as gateway_process
    try: msgs = gateway_process(msgs)
    except Exception as exc:
        logger.error('Gateway 拒绝消息: %s', exc)
        return f'[语义协议违规] {exc}'

    from behavior_canon import (canonicalize as canon_behavior,
                                validate_output, retry_feedback, Path)
    ctx = canon_behavior(msgs, task_text=task_text, task_type=task_type,
                         agent_id=agent_id, extra_rules=extra_rules)
    msgs = ctx.messages
    prefer_explore = (ctx.path == Path.EXPLORATORY)
    temperature = ctx.temp_policy.sample(ctx.control_strength, prefer_explore)

    safe_msgs = []
    for m in msgs:
        role = m.get('role','user'); content = str(m.get('content',''))
        max_len = 2000 if role == 'system' else 4000
        safe_msgs.append({'role':role,'content':truncate(content,max_len)})
    msgs = safe_msgs

    candidates = []
    agnes_key = get_agnes_key()
    if prefer_free and agnes_key:
        candidates.append({'name':'agnes','url':AGNES_API_URL,'key':agnes_key,
                           'model':_select_agnes_model(task_type)})
    deepseek_key = get_api_key(); deepseek_url = get_api_url()
    if deepseek_key:
        candidates.append({'name':'deepseek','url':deepseek_url,'key':deepseek_key,
                           'model':'deepseek-chat'})
    if not candidates:
        return '[模型不可用：请配置 AGNES_API_KEY 或 LLM_API_KEY 环境变量]'

    from behavior_canon import SchemaMode
    max_retries = 2 if ctx.schema_mode == SchemaMode.STRICT else 0

    for c in candidates:
        retries = 0
        while retries <= max_retries:
            try:
                data = _post_json(c['url'],{'model':c['model'],'messages':msgs,
                    'max_tokens':2000,'temperature':temperature},c['key'],timeout=timeout)
                content = data['choices'][0]['message']['content']
                if not content: logger.warning('[%s] 返回空内容',c['name']); break
                passed, issues = validate_output(content, ctx.control_strength,
                                                  ctx.task_type, mode=ctx.schema_mode)
                if passed or retries >= max_retries:
                    if not passed: logger.debug('校验未通过(重试耗尽): %s',issues)
                    from behavior_canon import record_outcome
                    record_outcome(task_type=ctx.task_type,agent_id=ctx.agent_id,
                        control_raw=ctx.control_raw,control_final=ctx.control_strength,
                        success=passed,output_quality=0.7 if passed else 0.3)
                    if not skip_ground:
                        from behavior_canon import auto_ground_v3
                        auto_ground_v3(content,ctx.task_type,ctx.agent_id,
                                       ctx.control_strength,llm_caller=call_llm)
                    return content
                retries += 1
                logger.debug('[%s] 重试%d: %s',c['name'],retries,issues)
                msgs.append({'role':'user','content':retry_feedback(issues,retries)})
            except Exception as exc:
                logger.warning('[%s] 失败: %s',c['name'],exc); break
    return '[所有模型不可用，请稍后重试]'


class TokenStore:
    def __init__(self, ttl_seconds: int):
        self._tokens: dict[str,float] = {}; self._lock = threading.Lock(); self._ttl = ttl_seconds
    def issue(self) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[token] = time.time() + self._ttl
            now = time.time()
            expired = [t for t,exp in self._tokens.items() if exp < now]
            for t in expired: del self._tokens[t]
        return token
    def validate(self, token: str) -> bool:
        if not token: return False
        with self._lock:
            exp = self._tokens.get(token)
            if exp and exp > time.time(): return True
            if exp: del self._tokens[token]
        return False
    def count(self) -> int:
        with self._lock: return len(self._tokens)

bus = get_bus(); session = SessionManager(); wm = WorkingMemory()
tokens = TokenStore(ttl_seconds=UNLOCK_DURATION_SECONDS)
tsm = TaskStateMachine(bus); registry = AgentRegistry()
reviewer = None; orch = None

def _build_agent_context() -> str:
    now = datetime.now()
    weekday_cn = ['一','二','三','四','五','六','日'][now.weekday()]
    hour = now.hour
    period = ('凌晨' if hour<6 else '早上' if hour<9 else '上午' if hour<12 else '下午' if hour<18 else '晚上')
    parts = [f'当前时间: {now.month}/{now.day} 周{weekday_cn} {period}']
    if wm.active_project: parts.append(f'活跃项目: {wm.active_project}')
    today = memory_manager.get_today_state()
    if today: parts.append(f'今日: {today.get("messages_count",0)}消息 {today.get("tasks_completed",0)}任务')
    summaries = memory_manager.get_conversation_summaries(days=3,limit=3)
    if summaries: parts.append('最近话题: '+'；'.join(s['topic'][:30] for s in summaries))
    return '\n'.join(parts)

def _parse_at_mentions(text: str) -> list:
    import re as _re3
    pattern = r'@(\w+)\s+([^\n@]+)'
    return [(m.group(1).lower(),m.group(2).strip()) for m in _re3.finditer(pattern,text)]

def _auto_write_files(reply: str) -> int:
    import re as _re4
    code_start = reply.find('```html')
    if code_start == -1: code_start = reply.find('```')
    if code_start == -1: return 0
    nl = reply.find('\n',code_start)
    code_body = reply[nl+1:] if nl>0 else reply[code_start+3:]
    last_end = code_body.rfind('```')
    content = code_body[:last_end].strip() if last_end>0 else ''
    if not content or len(content)<50: return 0
    prefix = reply[:code_start]
    path_m = _re4.search(r'(?<![A-Za-z])[A-Za-z]:[\\/][^\s<>\"|\n]+\.(?:html|css|js|py|json|txt|md|bat|sh)',prefix)
    if not path_m: return 0
    filepath = path_m.group(0)
    wr = tool_execute('write_file',{'path':filepath,'content':content})
    if wr.ok: logger.info('auto-wrote: %s (%d chars)',filepath,len(content)); return 1
    return 0

def handle_message(text):
    is_attack, reason = detect_jailbreak(text)
    if is_attack: return f'🛡️ 检测到{reason}，已拒绝。','zero'
    wm.add_message('user',text)
    import re as _re2
    proj_match = _re2.search(r'E:[\\/]project[\\/]([^\\/\s\"\'<>|:*?]+)',text)
    if proj_match: wm.track_project(proj_match.group(1))
    at_mentions = _parse_at_mentions(text)
    if at_mentions:
        results = []
        for agent_id, task_desc in at_mentions:
            agent = registry._agents.get(agent_id)
            if agent and agent.get('executor'):
                try: output = agent['executor'](task_desc,['chat'],{}); results.append(output)
                except Exception as e: results.append(f'[{agent_id}] ❌ {e}')
            else: results.append(f'[{agent_id}] ❌ Agent 不可用')
        reply = '\n\n'.join(results); agent_name = 'orchestrator'
        n = _auto_write_files(reply)
        if n>0: reply += '\n\n✅ 文件已自动保存。'
    else:
        ctx = _build_agent_context()
        history = wm.get_conversation_history(limit=12)
        messages = [{'role':'system','content':(f'{ctx}\n你是零，主人的智能助手。\n聊天时直接回复。需要执行任务时用 @Agent名 分发：\n  @reasonix 写代码  @agnes_text 聊天  @agnes_image 生图  @tavily 搜索\n给文件路径+代码块会自动保存。用中文。')},*history,{'role':'user','content':text}]
        raw = call_llm(messages=messages,prefer_free=False,task_type='reasoning',task_text=text,agent_id='reasonix')
        reply = raw; agent_name = 'reasonix'
        at_in_reply = _parse_at_mentions(reply)
        if at_in_reply:
            results = []
            for aid, task_desc in at_in_reply:
                ag = registry._agents.get(aid)
                if ag and ag.get('executor'):
                    try: out = ag['executor'](task_desc,['chat'],{}); results.append(out)
                    except Exception as e: results.append(f'[{aid}] ❌ {e}')
                else: results.append(f'[{aid}] ❌ 不可用')
            reply = '\n\n'.join(results); agent_name = 'orchestrator'
            n = _auto_write_files(reply)
            if n>0: reply += '\n\n✅ 文件已自动保存。'
        n = _auto_write_files(reply)
        if n>0: reply += '\n\n✅ 文件已自动保存。'
    wm.add_message('assistant',reply); wm.mark_task_done()
    try:
        memory_manager.save_task(task_id=f'msg_{datetime.now().strftime("%Y%m%d_%H%M%S")}',agent='reasonix',task_type='chat',input_summary=text[:100],outcome='success',tokens_used=len(reply))
    except Exception as exc: logger.warning('写记忆失败: %s',exc)
    return reply, agent_name

seed_defaults(registry, llm_caller=call_llm, image_caller=None)
reviewer = Reviewer(llm_caller=call_llm)
orch = TaskOrchestrator(tsm, registry, llm_caller=call_llm, reviewer=reviewer)
