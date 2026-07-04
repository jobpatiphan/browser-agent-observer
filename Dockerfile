FROM python:3.13-slim

WORKDIR /app

# mitmproxy pulls a fair bit; install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 8790 = dashboard UI/API, 8083 = traffic proxy
EXPOSE 8790 8083

CMD ["python", "server.py"]
