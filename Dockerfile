FROM python:3.12-slim

WORKDIR /app

# deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pipeline ./pipeline
COPY webapp ./webapp

# all persistent state (sqlite DB, scratch work dirs, logs) lives under /data,
# which is mounted as a volume so books + generated art survive restarts.
ENV STORY_APP_DB=/data/storyteller.db \
    STORY_COST_DB=/data/costs.db \
    STORY_OUT=/data/scratch \
    STORY_WORK=/data/work \
    STORY_LOGS=/data/logs \
    STORY_PREFETCH=4 \
    STORY_WARM_PAGES=2 \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "webapp.server:app", "--host", "0.0.0.0", "--port", "8000"]
