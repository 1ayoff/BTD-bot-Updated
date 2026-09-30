FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
RUN useradd --create-home btd && mkdir /data && chown btd:btd /data
ENV BTD_DATA_DIR=/data
USER btd
CMD ["python", "bot.py"]
