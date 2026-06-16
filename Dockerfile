# Zero · Docker 部署
FROM python:3.11-slim

LABEL org.opencontainers.image.title="Zero"
LABEL org.opencontainers.image.description="AI OS Kernel — 多Agent协作平台"
LABEL org.opencontainers.image.source="https://github.com/liutingqiu/zero"

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY . .

# 创建数据目录
RUN mkdir -p data/sandbox data/reports

# 暴露端口
EXPOSE 5052

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5052/health')" || exit 1

# 启动
CMD ["python", "zero_server.py"]
