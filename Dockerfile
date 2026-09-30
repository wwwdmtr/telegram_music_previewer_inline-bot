FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies in their own layer so code edits do not re-run pip.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY music_bot ./music_bot

# Don't run as root. No chown: the app only ever reads /app, so leaving it owned
# by root keeps the code read-only for the process.
RUN useradd --create-home --uid 10001 bot
USER bot

CMD ["python", "-m", "music_bot"]
