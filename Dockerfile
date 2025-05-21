# Use the official lightweight Python image.
# https://hub.docker.com/_/python
FROM python:3.9-slim-buster

# Allow statements and log messages to immediately appear in the Cloud Run logs
ENV PYTHONUNBUFFERED True

# Copy local code to the container image
WORKDIR /app
COPY . .

# Install production dependencies.
RUN pip install -r requirements.txt

# Service listens on port 8080 by default.
# This port is exposed automatically by Cloud Run.
ENV PORT 8080

CMD ["python", "main.py"]
