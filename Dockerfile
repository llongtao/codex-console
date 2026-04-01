# 使用官方 Python 基础镜像 (使用 slim 版本减小体积)
FROM python:3.11-slim

ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright

# 设置工作目录
WORKDIR /app

# 设置环境变量
# WebUI 默认配置
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    WEBUI_HOST=0.0.0.0 \
    WEBUI_PORT=1455 \
    DISPLAY=:99 \
    ENABLE_VNC=1 \
    VNC_PORT=5900 \
    NOVNC_PORT=6080 \
    LOG_LEVEL=info \
    DEBUG=0

# 安装系统依赖
# (curl_cffi 等库可能需要编译工具)
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|http://deb.debian.org/debian|https://${APT_MIRROR}/debian|g; s|http://security.debian.org/debian-security|https://${APT_MIRROR}/debian-security|g" /etc/apt/sources.list.d/debian.sources; \
      elif [ -f /etc/apt/sources.list ]; then \
        sed -i "s|http://deb.debian.org/debian|https://${APT_MIRROR}/debian|g; s|http://security.debian.org/debian-security|https://${APT_MIRROR}/debian-security|g" /etc/apt/sources.list; \
      fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
        xvfb \
        fluxbox \
        x11vnc \
        websockify \
        novnc \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && (PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST}" python -m playwright install --with-deps chromium \
        || env -u PLAYWRIGHT_DOWNLOAD_HOST python -m playwright install --with-deps chromium)

# 复制项目代码
COPY . .
COPY scripts/docker/start-webui.sh /app/scripts/docker/start-webui.sh
RUN chmod +x /app/scripts/docker/start-webui.sh

# 暴露端口
EXPOSE 1455
EXPOSE 6080
EXPOSE 5900

# 启动 WebUI
CMD ["/app/scripts/docker/start-webui.sh"]
