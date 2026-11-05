FROM python:3.12-slim
RUN useradd --system --uid 10001 bot
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
RUN mkdir -p /app/data && chown bot /app/data
USER bot
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
