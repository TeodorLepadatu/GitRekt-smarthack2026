FROM python:3.13-slim

WORKDIR /app

# lightgbm (the ML models, used by real days: python -m backend --date ...) needs OpenMP
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "-m", "backend", "--host", "0.0.0.0", "--port", "8000"]
