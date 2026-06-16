FROM python:3.11-slim

WORKDIR /app

# ffmpeg powers local slideshow video creation (app/video_gen.py)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv

COPY pyproject.toml .
COPY uv.lock* ./
RUN uv sync --no-dev || uv sync

RUN uv run playwright install chromium
RUN uv run playwright install-deps chromium

COPY . .
RUN mkdir -p data/sessions data/media

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
