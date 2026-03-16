FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY epg_recorder.py .

CMD ["python", "-u", "epg_recorder.py"]
