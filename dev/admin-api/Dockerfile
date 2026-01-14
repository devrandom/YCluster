FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for scapy and netifaces
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY config/ansible/admin/files/ycluster/requirements.txt /app/requirements.txt
# Pin protobuf for etcd3 compatibility
RUN pip install --no-cache-dir 'protobuf<4' && pip install --no-cache-dir -r requirements.txt

# Create directory for bootstrap files (mounted at runtime)
RUN mkdir -p /opt/bootstrap-files

ENV PYTHONPATH=/app/ycluster

EXPOSE 12723

CMD ["python3", "app.py"]
