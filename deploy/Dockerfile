FROM mwader/static-ffmpeg:latest AS ffmpeg
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY app/ /app/
EXPOSE 4999
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "4999"]
