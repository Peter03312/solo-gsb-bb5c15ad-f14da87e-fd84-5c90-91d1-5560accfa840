FROM python:3.12-slim

WORKDIR /app

# 依赖（分层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY app ./app

# 数据目录（SQLite），首次启动自动建表初始化
ENV FLOOD_DB_PATH=/data/flood.db
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
