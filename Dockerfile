FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /srv/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV OUTAGE_DB_PATH=/data/outage.db \
    MERGE_GAP_MINUTES=5

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
