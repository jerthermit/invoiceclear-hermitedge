FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INVOICECLEAR_HOST=0.0.0.0 \
    INVOICECLEAR_DB_PATH=/data/invoiceclear.sqlite3
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY invoiceclear ./invoiceclear
COPY fixtures ./fixtures
COPY run.py start.py ./
RUN groupadd --gid 10001 invoiceclear \
    && useradd --create-home --uid 10001 --gid 10001 invoiceclear \
    && mkdir -p /data \
    && chown invoiceclear:invoiceclear /data
USER invoiceclear
EXPOSE 8100
CMD ["python", "start.py"]
