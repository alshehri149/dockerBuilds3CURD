FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV PORT 8080
CMD ["gunicorn", "main:app", "-b", "0.0.0.0:8080"]
