FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Volumes for persistent data
VOLUME ["/app/published.json", "/app/bot.log"]

CMD ["python", "-m", "telegram_news_bot.main"]
