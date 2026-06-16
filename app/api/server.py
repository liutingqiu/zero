"""零 · API 层 (aiohttp)
========================
全异步 HTTP 服务——替代 ThreadingHTTPServer。

P2: asyncio 事件循环 + 非阻塞 SSE + 协程式路由。
"""

import json
import os
import sys
import urllib.parse
import logging

from aiohttp import web

from config import (
    HTTP_HOST, HTTP_PORT, ZERO_ROOT,
    get_agnes_key, get_api_key, get_api_url, get_logger,
)
from interface.webapp import WEBAPP_HTML
from app.services.llm import (
    call_llm, handle_message, tokens, session, wm,
    bus, tsm, registry, reviewer, orch,
)
from cognition import memory_manager

logger = get_logger('zero.api')
os.chdir(ZERO_ROOT)

routes = web.RouteTableDef()


# ═══════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════

def _authed(request: web.Request) -> bool:
    return session.is_unlocked()


def _cors_headers() -> dict:
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
    }


# ═══════════════════════════════════════════
# 路由
# ═══════════════════════════════════════════

@routes.view('/health')
class HealthHandler(web.View):
    async def get(self):
        return web.json_response({
            'status': 'ok',
            'session': '已解锁' if session.is_unlocked() else '已锁定',
            'active_tokens': tokens.count(),
        }, headers=_cors_headers())


@routes.view('/api/settings')
class SettingsHandler(web.View):
    async def get(self):
        if not _authed(self.request):
            return web.json_response({'error': '需要认证'}, status=401, headers=_cors_headers())
        agent_status = registry.list_all()
        mem_status = memory_manager.status()
        api_info = {
            'agnes': bool(get_agnes_key()),
            'deepseek': bool(get_api_key()),
            'base_url': get_api_url() if get_api_key() else '',
        }
        return web.json_response({
            'agents': agent_status, 'memory': mem_status, 'apis': api_info,
            'session_unlocked': session.is_unlocked(), 'watch_root': 'E:\\project',
        }, headers=_cors_headers())


@routes.view('/api/history')
class HistoryHandler(web.View):
    async def get(self):
        if not _authed(self.request):
            return web.json_response({'error': '需要认证'}, status=401, headers=_cors_headers())
        try:
            from cognition.memory_manager import get_conversation_summaries
            summaries = get_conversation_summaries(days=7, limit=50)
            return web.json_response({'history': summaries}, headers=_cors_headers())
        except Exception as exc:
            return web.json_response({'error': str(exc)}, status=500, headers=_cors_headers())


@routes.view('/api/kanban')
class KanbanHandler(web.View):
    async def get(self):
        if not _authed(self.request):
            return web.json_response({'error': '需要认证'}, status=401, headers=_cors_headers())
        try:
            from action.kanban import list_tasks, stats
            s = stats(); tasks = list_tasks(limit=20)
            return web.json_response({
                'done': s['done'], 'total': s['total'],
                'tasks': [{'title': t.title[:60], 'status': t.status, 'id': t.id} for t in tasks if t.title],
            }, headers=_cors_headers())
        except Exception as exc:
            return web.json_response({'error': str(exc)}, status=500, headers=_cors_headers())


@routes.view('/api/notifications')
class NotificationHandler(web.View):
    async def get(self):
        if not _authed(self.request):
            return web.json_response({'error': '需要认证'}, status=401, headers=_cors_headers())
        return web.json_response({'notifications': []}, headers=_cors_headers())


@routes.view('/api/image-proxy')
class ImageProxyHandler(web.View):
    async def get(self):
        url = self.request.query.get('url', '')
        if not url:
            return web.json_response({'error': 'missing url'}, status=400, headers=_cors_headers())
        try:
            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession() as client:
                async with client.get(url, headers={'User-Agent': 'Zero/1.0'}, timeout=15) as resp:
                    img_data = await resp.read()
            ct = resp.content_type or 'image/png'
            return web.Response(body=img_data, content_type=ct,
                                headers={'Cache-Control': 'public, max-age=86400'})
        except Exception as exc:
            return web.json_response({'error': str(exc)}, status=502, headers=_cors_headers())


@routes.view('/api/chat/stream')
class ChatStreamHandler(web.View):
    async def get(self):
        if not _authed(self.request):
            return web.Response(status=401, content_type='text/event-stream',
                                headers=_cors_headers())
        message = self.request.query.get('m', '')
        if not message:
            return web.json_response({'error': 'missing message'}, status=400, headers=_cors_headers())

        resp = web.StreamResponse(
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream; charset=utf-8',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                **_cors_headers(),
            })
        await resp.prepare(self.request)

        async def _send(kind, payload):
            data = json.dumps({'type': kind, 'data': payload}, ensure_ascii=False)
            try:
                await resp.write(('data: ' + data + '\n\n').encode('utf-8'))
            except (ConnectionResetError, BrokenPipeError):
                pass

        await _send('status', 'thinking')
        try:
            reply, agent = handle_message(message)
            chunk_size = 80
            for i in range(0, len(reply), chunk_size):
                await _send('chunk', reply[i:i + chunk_size])
            await _send('done', {'agent': agent, 'total_chars': len(reply)})
        except Exception as exc:
            logger.warning('SSE chat failed: %s', exc)
            await _send('error', str(exc))
        return resp


@routes.view('/api/collab/stream')
class CollabStreamHandler(web.View):
    async def get(self):
        if not _authed(self.request):
            return web.Response(status=401, content_type='text/event-stream',
                                headers=_cors_headers())
        message = self.request.query.get('m', '')
        if not message:
            return web.json_response({'error': 'missing message'}, status=400, headers=_cors_headers())

        resp = web.StreamResponse(
            status=200, reason='OK',
            headers={
                'Content-Type': 'text/event-stream; charset=utf-8',
                'Cache-Control': 'no-cache', 'Connection': 'keep-alive',
                **_cors_headers(),
            })
        await resp.prepare(self.request)

        async def _send(kind, payload):
            data = json.dumps({'type': kind, 'data': payload}, ensure_ascii=False)
            try:
                await resp.write(('data: ' + data + '\n\n').encode('utf-8'))
            except (ConnectionResetError, BrokenPipeError):
                pass

        from app.services.async_llm import async_call_llm as _acall

        try:
            from multi_agent import PlannerV5, BlackboardV5, ExecutorV5, CriticV5, SynthesizerV5
            from behavior_canon import synthetic_evaluate

            # 使用异步 LLM
            async def _fast_call(**kw):
                return await _acall(messages=kw.get('messages'), prefer_free=False, timeout=60)

            await _send('status', '启动协作引擎')
            await _send('step', {'role': 'planner', 'status': 'running', 'action': '正在分析任务...'})

            try:
                planner = PlannerV5(lambda **kw: call_llm(**kw))  # 同步包装
                bb = BlackboardV5(message)
                proposed = planner.propose(bb)
                step_ids = bb.create_steps(proposed)
                await _send('step', {'role': 'planner', 'status': 'done',
                                     'action': f'拆解为 {len(step_ids)} 个步骤',
                                     'detail': str(proposed)[:500]})
            except Exception as exc:
                await _send('step', {'role': 'planner', 'status': 'failed',
                                     'action': f'规划失败: {exc}'})
                await _send('done', {'status': 'failed', 'answer': f'规划失败: {exc}'})
                return resp

            executor = ExecutorV5(lambda **kw: call_llm(**kw))
            critic = CriticV5(lambda **kw: call_llm(**kw))
            completed = 0

            for sid in step_ids:
                step = bb.get_step(sid)
                if not step:
                    continue
                info = {'id': sid, 'action': step['action'], 'criteria': step['criteria']}
                await _send('step', {'id': sid, 'role': 'executor', 'status': 'running',
                                     'action': step['action'][:120]})

                for attempt in range(3):
                    try:
                        bb.start_step(sid, 'executor')
                        output = executor.execute(info, bb)
                        rule_s, _ = synthetic_evaluate(output, 'code')
                        critique = critic.review(output, info)
                        bb.submit_critique(sid, critique, 'critic')

                        if critique.get('passed', True):
                            bb.complete_step(sid, output, 'executor')
                            completed += 1
                            await _send('step', {
                                'id': sid, 'role': 'executor', 'status': 'done',
                                'action': step['action'][:120], 'output': output[:800],
                                'critique': {'score': critique.get('score'), 'passed': True},
                            })
                            break
                        elif attempt < 2:
                            issues = critique.get('issues', [])
                            suggestions = critique.get('suggestions', [])
                            info['action'] = f'{info["action"]}\n[修正] {"; ".join(issues)}'
                            await _send('step', {
                                'id': sid, 'role': 'critic', 'status': 'running',
                                'action': f'发现问题: {"; ".join(issues[:2])}',
                                'detail': f'建议: {"; ".join(suggestions[:2])}',
                            })
                        else:
                            bb.fail_step(sid, 'executor', '审查未通过')
                            await _send('step', {'id': sid, 'role': 'executor',
                                                  'status': 'failed',
                                                  'action': step['action'][:120]})
                    except Exception as exc:
                        await _send('step', {'id': sid, 'role': 'executor',
                                              'status': 'failed',
                                              'action': str(exc)[:120]})
                        break

            await _send('step', {'role': 'synthesizer', 'status': 'running',
                                 'action': '正在整合结果...'})
            synthesizer = SynthesizerV5(lambda **kw: call_llm(**kw))
            answer = synthesizer.synthesize(bb)
            await _send('step', {'role': 'synthesizer', 'status': 'done',
                                 'action': '结果整合完成'})
            await _send('done', {
                'status': 'done' if completed == len(step_ids) else 'partial',
                'answer': answer, 'completed': completed, 'total': len(step_ids),
            })
        except Exception as exc:
            logger.warning('SSE collab failed: %s', exc)
            await _send('error', str(exc))
        return resp


# ═══════════════════════════════════════════
# POST 端点
# ═══════════════════════════════════════════

@routes.view('/api/auth')
class AuthHandler(web.View):
    async def post(self):
        try:
            data = await self.request.json()
        except Exception:
            return web.json_response({'error': '无效JSON'}, status=400, headers=_cors_headers())
        code = data.get('code', '')
        if session.is_unlocked():
            t = tokens.issue()
            return web.json_response({'ok': True, 'token': t, 'message': '已解锁'}, headers=_cors_headers())
        ok, msg = session.authenticate(code)
        if ok:
            wm.add_message('system', '会话解锁')
            t = tokens.issue()
            return web.json_response({'ok': True, 'token': t, 'message': msg}, headers=_cors_headers())
        return web.json_response({'ok': False, 'error': msg}, status=401, headers=_cors_headers())


@routes.view('/api/chat')
class ChatHandler(web.View):
    async def post(self):
        if not _authed(self.request):
            return web.json_response({'reply': '会话已锁定，请先认证。', 'status': 'locked'},
                                     status=401, headers=_cors_headers())
        try:
            data = await self.request.json()
        except Exception:
            return web.json_response({'error': '无效JSON'}, status=400, headers=_cors_headers())
        message = data.get('message', '')
        agent_id = data.get('agent_id')
        try:
            if agent_id:
                reply = registry.run(agent_id, message, capabilities=['chat'])
                agent = agent_id
            else:
                reply, agent = handle_message(message)
            try:
                memory_manager.save_conversation_summary(
                    topic=message[:30], summary=reply[:200],
                    emotion=wm.owner_mood, messages_count=1)
            except Exception:
                pass
            return web.json_response({'reply': reply, 'status': 'ok', 'agent': agent},
                                     headers=_cors_headers())
        except Exception as exc:
            return web.json_response({'reply': f'处理失败: {exc}', 'status': 'error', 'agent': 'zero'},
                                     status=500, headers=_cors_headers())


@routes.view('/api/agents/{agent_id}/run')
class AgentRunHandler(web.View):
    async def post(self):
        if not _authed(self.request):
            return web.json_response({'error': '需要认证'}, status=401, headers=_cors_headers())
        agent_id = self.request.match_info['agent_id']
        try:
            data = await self.request.json()
        except Exception:
            return web.json_response({'error': '无效JSON'}, status=400, headers=_cors_headers())
        message = data.get('message', '')
        try:
            reply = registry.run(agent_id, message, capabilities=data.get('capabilities'))
            return web.json_response({'reply': reply, 'status': 'ok', 'agent': agent_id},
                                     headers=_cors_headers())
        except Exception as exc:
            return web.json_response({'reply': f'⚠️ {exc}', 'status': 'error', 'agent': agent_id},
                                     status=500, headers=_cors_headers())


@routes.view('/api/collab')
class CollabHandler(web.View):
    async def post(self):
        if not _authed(self.request):
            return web.json_response({'error': '需要认证'}, status=401, headers=_cors_headers())
        try:
            data = await self.request.json()
        except Exception:
            return web.json_response({'error': '无效JSON'}, status=400, headers=_cors_headers())
        message = data.get('message', '')
        if not message:
            return web.json_response({'error': '缺少 message'}, status=400, headers=_cors_headers())
        try:
            from multi_agent import collaborate_v8
            tool_exec = None
            try:
                from action.tools import execute as _texec
                tool_exec = lambda out, _e=_texec: _e('shell', {'command': f'python -c "{out[:200]}"'}).ok
            except Exception:
                pass
            result = collaborate_v8(message, call_llm, tool_exec)
            bb = result.get('blackboard')
            steps_detail = []
            if bb:
                for sid in bb._step_order:
                    s = bb._steps.get(sid, {})
                    versions = s.get('versions', [])
                    steps_detail.append({
                        'id': sid, 'action': s.get('action', '')[:120],
                        'status': s.get('status', 'pending'),
                        'output': versions[-1]['output'][:500] if versions else '',
                        'version_count': len(versions),
                        'critiques': [c.get('data', {}).get('passed', True)
                                      for c in s.get('critiques', [])]
                        if isinstance(s.get('critiques'), list) else [],
                    })
            return web.json_response({
                'status': result.get('status', 'error'),
                'answer': result.get('answer', ''),
                'steps': steps_detail,
                'completed': result.get('completed', 0),
                'failed': result.get('failed', 0),
                'grounded': result.get('grounded', 0),
                'events': bb.events.stats() if bb else {'total': 0},
            }, headers=_cors_headers())
        except Exception as exc:
            logger.warning('collab failed: %s', exc)
            return web.json_response({'error': str(exc), 'status': 'failed'},
                                     status=500, headers=_cors_headers())


# ═══════════════════════════════════════════
# 静态文件 + 前端
# ═══════════════════════════════════════════

@routes.get('/')
@routes.get('/index.html')
async def index_handler(request: web.Request):
    return web.Response(body=WEBAPP_HTML.encode('utf-8'),
                        content_type='text/html', charset='utf-8',
                        headers={'Cache-Control': 'no-cache'})


@routes.get('/product')
async def product_handler(request: web.Request):
    path = os.path.join(ZERO_ROOT, 'interface', 'product.html')
    if os.path.isfile(path):
        return web.FileResponse(path)
    return web.Response(status=404)


@routes.get('/favicon.ico')
async def favicon_handler(request: web.Request):
    path = os.path.join(ZERO_ROOT, 'interface', 'hermes_web', 'favicon.ico')
    if os.path.isfile(path):
        return web.FileResponse(path)
    return web.Response(status=404)


# ═══════════════════════════════════════════
# OPTIONS (CORS preflight)
# ═══════════════════════════════════════════

# ═══════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════

@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == 'OPTIONS':
        return web.Response(status=200, headers=_cors_headers())
    resp = await handler(request)
    resp.headers.update(_cors_headers())
    return resp


# 速率限制
import time as _time
_rate_limits: dict[str, list[float]] = {}

@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    """简单令牌桶: 每 IP 每分钟 60 请求。"""
    ip = request.remote or '127.0.0.1'
    now = _time.time()
    window = 60  # 1 分钟
    max_req = 60

    if ip not in _rate_limits:
        _rate_limits[ip] = []
    # 清理过期记录
    _rate_limits[ip] = [t for t in _rate_limits[ip] if now - t < window]
    if len(_rate_limits[ip]) >= max_req:
        return web.json_response(
            {'error': '请求过于频繁，请稍后重试', 'retry_after': int(window - (now - _rate_limits[ip][0]))},
            status=429,
            headers={**_cors_headers(), 'Retry-After': str(int(window))},
        )
    _rate_limits[ip].append(now)
    return await handler(request)


def main():
    logger.info('零 v5 · 启动中... http://%s:%s', HTTP_HOST, HTTP_PORT)
    logger.info('Agent: %d 位已注册', len(registry.list_all()))

    app = web.Application(middlewares=[cors_middleware, rate_limit_middleware])
    app.add_routes(routes)

    # 静态资源
    static_path = os.path.join(ZERO_ROOT, 'interface', 'hermes_web')
    if os.path.isdir(static_path):
        app.router.add_static('/assets/', path=static_path, name='assets')

    web.run_app(app, host=HTTP_HOST, port=HTTP_PORT, print=None)


if __name__ == '__main__':
    main()
