FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY windsor_bridge ./windsor_bridge
COPY run_bridge.py .
ENV BIND_HOST=0.0.0.0 BIND_PORT=8000
EXPOSE 8000
CMD ["python", "run_bridge.py"]
