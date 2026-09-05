FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV SPARROW_BIND=0.0.0.0

WORKDIR /app

COPY requirements-hub.txt .

RUN pip install --no-cache-dir -r requirements-hub.txt

COPY . .

RUN mkdir -p /app/data

EXPOSE 8150

CMD ["python", "hub.py"]
