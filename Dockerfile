FROM python:3.9-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
COPY main.py .
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir data
ENTRYPOINT ["python", "main.py"]
