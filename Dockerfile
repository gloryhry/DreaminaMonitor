FROM python:3.12-slim

WORKDIR /app

# 安装 uv 包管理器
RUN pip install uv

# 复制依赖文件
COPY pyproject.toml uv.lock ./

# 安装项目依赖
RUN uv sync --frozen

# 复制应用代码
COPY . .

# 暴露端口
EXPOSE 5100

# 启动应用
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5100"]
