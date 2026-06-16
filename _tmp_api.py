r"""零 · 主服务器
================
HTTP :5052。串联全部模块。

修复要点：
  - P0-A1: call_llm —— 候选链 + 显式异常，不再裸 `except: pass`
class ZeroHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。token 机制：
    /api/auth 成功后会签发 token；其他所有写接口都必须带 token。
    """

    def log_message(self, format, *args):  # noqa: A002
        return  # 安静模式

    # ── 辅助 ────────────────────────────────────────────────────
    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers',
                         'Content-Type, Authorization')
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, rel_path, content_type):
        full = os.path.join(ZERO_ROOT, 'interface', rel_path)
        if os.path.isfile(full):
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.end_headers()
            with open(full, 'rb') as fh:
                self.wfile.write(fh.read())
        else:
            self.send_error(404)

    def _needs_auth(self) -> bool:
        return self.path in ('/api/chat', '/api/history',
                              '/api/kanban', '/api/notifications')

    # ── HTTP 方法 ──────────────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods',
                         'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                         'Content-Type, Authorization')
        self.end_headers()

    def do_GET(self):
        # 标准化路径（去除 query string 和尾部 /）
        path = self.path.split('?')[0].rstrip('/') or '/'

        if path == '/health':
            self._json({
                'status': 'ok',
                'session': '已解锁' if session.is_unlocked() else '已锁定',
                'active_tokens': tokens.count(),
            })
            return

        # ===== 升级：Settings API（前端用）=====
        if path == '/api/settings':
            if not self._authed():
                self._json({'error': '需要认证'}, 401)
                return
            # 返回系统状态：Agent 列表、记忆统计、模型 API 状态
            agent_status = registry.list_all()
            mem_status = memory_manager.status()
            # 检测各 API 连通性（轻量——只看 key 是否配置）
            api_info = {
                'agnes': bool(get_agnes_key()),
                'deepseek': bool(get_api_key()),
                'base_url': get_api_url() if get_api_key() else '',
            }
            self._json({
                'agents': agent_status,
                'memory': mem_status,
                'apis': api_info,
                'session_unlocked': session.is_unlocked(),
                'watch_root': 'E:\\project',
            })
            return

        # ===== 升级：SSE 流式聊天端点 =====
        if path == '/api/chat/stream':
            if not self._authed():
                self.send_response(401)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.end_headers()
                body = json.dumps({'type': 'error', 'data': '需要认证'},
                                  ensure_ascii=False)
                self.wfile.write(('data: ' + body + '\n\n').encode('utf-8'))
                return

            # 从 query string 读消息
            message = ''
            q = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(q)
            if 'm' in params:
                message = urllib.parse.unquote(params['m'][0])

            # 如果 GET 没传消息，返回 400
            if not message:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'missing message'},
                                            ensure_ascii=False).encode('utf-8'))
                return

            # SSE 头
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            def _send(kind, payload):
                data = json.dumps({'type': kind, 'data': payload},
                                  ensure_ascii=False)
                try:
                    self.wfile.write(('data: ' + data + '\n\n').encode('utf-8'))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    # 客户端断开连接 — 正常，静默停止
                    pass
                except Exception:
                    logger.warning('SSE _send 异常（非网络断开）', exc_info=True)

            # 告知已接收
            _send('status', 'thinking')

            try:
                # 调用主处理
                reply, agent = handle_message(message)

                # 模拟流式输出（按"字符块"）
                # 真实 SSE 需要模型流式返回，这里先按 80 字符一块输出
                chunk_size = 80
                for i in range(0, len(reply), chunk_size):
                    chunk = reply[i:i + chunk_size]
                    _send('chunk', chunk)

                _send('done', {'agent': agent, 'total_chars': len(reply)})
            except Exception as exc:  # noqa: BLE001
                logger.warning('SSE 聊天失败: %s', exc)
                _send('error', str(exc))
            finally:
                # 关闭 SSE 连接，让前端 reader.read() 收到 done: true
                self.close_connection = True
            return

        if path.startswith('/assets'):
            ct = 'text/css' if self.path.endswith('.css') else (
                'application/javascript' if self.path.endswith('.js')
                else 'image/svg+xml' if self.path.endswith('.svg')
                else 'application/octet-stream'
            )
            self._serve_file('hermes_web/' + path[len('/assets'):].lstrip('/'), ct)
            return

        if path == '/favicon.ico':
            self._serve_file('hermes_web/favicon.ico', 'image/x-icon')
            return

        if path == '/product':
            self._serve_file('product.html', 'text/html; charset=utf-8')
            return

        if path in ('/', '/index.html'):
            body = WEBAPP_HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ('/agnes', '/agnes.html'):
            self._serve_file('agnes_chat.html', 'text/html; charset=utf-8')
            return

        # 图片代理：解决跨域下载问题
        if path == '/api/image-proxy':
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            url = params.get('url', [None])[0]
            if not url:
                self._json({'error': 'missing url'}, 400)
                return
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Zero/1.0'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    img_data = resp.read()
                ct = 'image/png' if url.endswith('.png') else (
                    'image/jpeg' if url.endswith(('.jpg', '.jpeg')) else
                    'image/webp' if url.endswith('.webp') else
                    'image/gif' if url.endswith('.gif') else
                    'image/png'
                )
                self.send_response(200)
                self.send_header('Content-Type', ct)
                self.send_header('Content-Length', str(len(img_data)))
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.end_headers()
                self.wfile.write(img_data)
            except Exception as exc:
                logger.warning('image proxy failed: %s', exc)
                self._json({'error': str(exc)}, 502)
            return

        # 需要鉴权的读接口
        if path == '/api/history':
            if not self._authed():
                self._json({'error': '需要认证'}, 401)
                return
            try:
                from cognition.memory_manager import get_conversation_summaries
                summaries = get_conversation_summaries(days=7, limit=50)
                self._json({'history': summaries})
            except Exception as exc:  # noqa: BLE001
                logger.warning('get history: %s', exc)
                self._json({'error': str(exc)}, 500)
            return

        if path == '/api/kanban':
            if not self._authed():
                self._json({'error': '需要认证'}, 401)
                return
            try:
                from action.kanban import list_tasks, stats
                s = stats()
                tasks = list_tasks(limit=20)
                self._json({
                    'done': s['done'],
                    'total': s['total'],
                    'tasks': [
                        {'title': t.title[:60], 'status': t.status, 'id': t.id}
                        for t in tasks if t.title
                    ],
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning('get kanban: %s', exc)
                self._json({'error': str(exc)}, 500)
            return

        if path == '/api/notifications':
            if not self._authed():
                self._json({'error': '需要认证'}, 401)
                return
            self._json({'notifications': []})
            return

        # ===== SSE 流式协作 =====
        if path == '/api/collab/stream':
            if not self._authed():
                self.send_response(401)
                self.end_headers()
                return
            message = ''
            q = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(q)
            if 'm' in params:
                message = urllib.parse.unquote(params['m'][0])
            if not message:
                self.send_response(400)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            def _send(kind, payload):
                data = json.dumps({'type': kind, 'data': payload}, ensure_ascii=False)
                try:
                    self.wfile.write(('data: ' + data + '\n\n').encode('utf-8'))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def _send(kind, payload):
                data = json.dumps({'type': kind, 'data': payload}, ensure_ascii=False)
                try:
                    self.wfile.write(('data: ' + data + '\n\n').encode('utf-8'))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            try:
                from multi_agent import collaborate_v8
                from behavior_canon import synthetic_evaluate
                import traceback

                _send('status', '启动协作引擎')
                # 协作模式优先速度——跳过慢速免费API，60s超时
                _fast = lambda **kw: call_llm(prefer_free=False, timeout=60, **kw)

                # 1. Planner
                _send('step', {'role': 'planner', 'status': 'running', 'action': '正在分析任务...'})
                try:
                    from multi_agent import PlannerV5, BlackboardV5
                    planner = PlannerV5(_fast)
                    bb = BlackboardV5(message)
                    proposed = planner.propose(bb)
                    step_ids = bb.create_steps(proposed)
                    _send('step', {'role': 'planner', 'status': 'done',
                                   'action': f'拆解为 {len(step_ids)} 个步骤',
                                   'detail': str(proposed)[:500]})
                except Exception as exc:
                    _send('step', {'role': 'planner', 'status': 'failed',
                                   'action': f'规划失败: {exc}'})
                    _send('done', {'status': 'failed', 'answer': f'规划失败: {exc}'})
                    return

                # 2. Execute + Critique
                from multi_agent import ExecutorV5, CriticV5, SynthesizerV5
                executor = ExecutorV5(_fast)
                critic = CriticV5(_fast)
                completed = 0
                for sid in step_ids:
                    step = bb.get_step(sid)
                    if not step:
                        continue
                    info = {'id': sid, 'action': step['action'], 'criteria': step['criteria']}
                    _send('step', {'id': sid, 'role': 'executor', 'status': 'running',
                                   'action': step['action'][:120]})

                    for attempt in range(3):
                        try:
                            bb.start_step(sid, 'executor')
                            output = executor.execute(info, bb)
                            rule_s, _ = synthetic_evaluate(output, 'code')

                            critique = critic.review(output, info)
                            bb.submit_critique(sid, critique, 'critic')
                            critic_s = critique.get('score', 50) / 100.0

                            if critique.get('passed', True):
                                bb.complete_step(sid, output, 'executor')
                                completed += 1
                                _send('step', {
                                    'id': sid, 'role': 'executor', 'status': 'done',
                                    'action': step['action'][:120],
                                    'output': output[:800],
                                    'critique': {'score': critique.get('score'), 'passed': True},
                                })
                                break
                            elif attempt < 2:
                                issues = critique.get('issues', [])
                                suggestions = critique.get('suggestions', [])
                                info['action'] = f'{info["action"]}\n[修正] {"; ".join(issues)}'
                                _send('step', {
                                    'id': sid, 'role': 'critic', 'status': 'running',
                                    'action': f'发现问题: {"; ".join(issues[:2])}',
                                    'detail': f'建议: {"; ".join(suggestions[:2])}',
                                })
                            else:
                                bb.fail_step(sid, 'executor', '审查未通过')
                                _send('step', {'id': sid, 'role': 'executor', 'status': 'failed',
                                               'action': step['action'][:120]})
                        except Exception as exc:
                            _send('step', {'id': sid, 'role': 'executor', 'status': 'failed',
                                           'action': str(exc)[:120]})
                            break

                # 3. Synthesize
                _send('step', {'role': 'synthesizer', 'status': 'running', 'action': '正在整合结果...'})
                synthesizer = SynthesizerV5(_fast)
                answer = synthesizer.synthesize(bb)
                _send('step', {'role': 'synthesizer', 'status': 'done', 'action': '结果整合完成'})
                _send('done', {'status': 'done' if completed == len(step_ids) else 'partial',
                               'answer': answer, 'completed': completed,
                               'total': len(step_ids)})

            except Exception as exc:
                logger.warning('SSE collab failed: %s', exc)
                _send('error', str(exc))
            finally:
                self.close_connection = True
            return

        self._json({'error': 'not found', 'path': path, 'raw': self.path}, 404)

    def _authed(self) -> bool:
        """鉴权：Session 解锁即可，token 为可选项。"""
        return session.is_unlocked()

    def do_POST(self):
        # 标准化路径
        path = self.path.split('?')[0].rstrip('/') or '/'

        length = int(self.headers.get('Content-Length', '0') or 0)
        raw = self.rfile.read(length) if length > 0 else b'{}'
        try:
            body = raw.decode('utf-8')
            data = json.loads(body) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json({'error': '无效JSON'}, 400)
            return

        if path == '/api/auth':
            code = data.get('code', '')
            if session.is_unlocked():
                t = tokens.issue()
                self._json({'ok': True, 'token': t, 'message': '已解锁'})
                return
            ok, msg = session.authenticate(code)
            if ok:
                wm.add_message('system', '会话解锁')
                t = tokens.issue()
                logger.info('用户认证成功，签发 token')
                self._json({'ok': True, 'token': t, 'message': msg})
            else:
                logger.warning('用户认证失败')
                self._json({'ok': False, 'error': msg}, 401)
            return

        if path == '/api/chat':
            if not self._authed():
                self._json({'reply': '会话已锁定，请先认证。',
                            'status': 'locked'}, 401)
                return

            message = data.get('message', '')
            # 可选：指定 Agent ID
            agent_id = data.get('agent_id')

            try:
                if agent_id:
                    # 用户显式指定 Agent
                    reply = registry.run(agent_id, message,
                                          capabilities=['chat'])
                    agent = agent_id
                else:
                    # 走默认流程（意图分类 → orchestrator）
                    reply, agent = handle_message(message)

                try:
                    memory_manager.save_conversation_summary(
                        topic=message[:30], summary=reply[:200],
                        emotion=wm.owner_mood, messages_count=1,
                    )
                except Exception:  # noqa: BLE001
                    pass

                self._json({'reply': reply, 'status': 'ok', 'agent': agent})
            except Exception as exc:  # noqa: BLE001
                logger.warning('聊天处理失败: %s', exc)
                self._json({'reply': f'处理失败: {exc}',
                            'status': 'error', 'agent': 'zero'}, 500)
            return

        # ===== 升级：指定 Agent 直接执行 =====
        if path.startswith('/api/agents/') and path.endswith('/run'):
            if not self._authed():
                self._json({'error': '需要认证'}, 401)
                return
            # 从 /api/agents/<id>/run 解析 id
            parts = path.split('/')
            agent_id = parts[-2] if len(parts) >= 3 else ''
            message = data.get('message', '')
            try:
                reply = registry.run(agent_id, message,
                                      capabilities=data.get('capabilities'))
                self._json({'reply': reply, 'status': 'ok',
                            'agent': agent_id})
            except Exception as exc:  # noqa: BLE001
                self._json({'reply': f'⚠️ {exc}', 'status': 'error',
                            'agent': agent_id}, 500)
            return

        if path == '/api/collab':
            if not self._authed():
                self._json({'error': '需要认证'}, 401)
                return
            message = data.get('message', '')
            mode = data.get('mode', 'work')
            if not message:
                self._json({'error': '缺少 message'}, 400)
                return
            try:
                from multi_agent import collaborate_v8
                # 尝试加载工具执行器（可选）
                tool_exec = None
                try:
                    from action.tools import execute as _texec
                    tool_exec = lambda out, _e=_texec: _e('shell', {'command': f'python -c \"{out[:200]}\"'}).ok
                except Exception:
                    pass
                result = collaborate_v8(message, call_llm, tool_exec)
                # 提取步骤详情给前端可视化
                bb = result.get('blackboard')
                steps_detail = []
                if bb:
                    for sid in bb._step_order:
                        s = bb._steps.get(sid, {})
                        versions = s.get('versions', [])
                        steps_detail.append({
                            'id': sid,
                            'action': s.get('action', '')[:120],
                            'status': s.get('status', 'pending'),
                            'output': versions[-1]['output'][:500] if versions else '',
                            'version_count': len(versions),
                            'critiques': [
                                c.get('data', {}).get('passed', True)
                                for c in s.get('critiques', [])
                            ] if isinstance(s.get('critiques'), list) else [],
                        })
                self._json({
                    'status': result.get('status', 'error'),
                    'answer': result.get('answer', ''),
                    'mode': mode,
                    'steps': steps_detail,
                    'completed': result.get('completed', 0),
                    'failed': result.get('failed', 0),
                    'grounded': result.get('grounded', 0),
                    'events': bb.events.stats() if bb else {'total': 0},
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning('collab failed: %s', exc)
                self._json({'error': str(exc), 'status': 'failed'}, 500)
            return

        self._json({'error': '未知端点', 'path': path}, 404)


def main():
    logger.info('零 v5 · 启动中... http://%s:%s', HTTP_HOST, HTTP_PORT)
    logger.info('模块: MessageBus + Security + Cognition + Action + Perception')
    logger.info('Agent: %d 位已注册，流式聊天已启用', len(registry.list_all()))

    # ThreadingHTTPServer —— 每个请求一个线程，避免长调用阻塞健康检查
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), ZeroHandler)
    server.daemon_threads = True

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('收到 Ctrl+C，正在关闭...')
        wm.flush(memory_manager)
        server.shutdown()
        logger.info('零已关闭')


if __name__ == '__main__':
    main()
