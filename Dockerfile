FROM python:3.12-slim

WORKDIR /app

# 公开部署只托管已经生成的 site/ 快照，不复制凭据、采集代码或本地输出。
# 保持 src/web.py 的目录层级，使它能从源码位置推导出 /app/site。
COPY src/web.py /app/src/web.py
COPY site /app/site

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "python /app/src/web.py --port \"${PORT}\""]
