FROM python:3.12-slim

WORKDIR /app

# 公开部署只托管已经生成的 site/ 快照，不复制凭据、采集代码或本地输出。
COPY src/web.py /app/web.py
COPY site /app/site

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "python /app/web.py --port \"${PORT}\""]
