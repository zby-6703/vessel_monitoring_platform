ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG ONNXRUNTIME_PACKAGE=onnxruntime==1.26.0

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /workspace/vessel_monitoring_platform

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt "${ONNXRUNTIME_PACKAGE}"

COPY backend ./backend
COPY runtime_support ./runtime_support

WORKDIR /workspace/vessel_monitoring_platform/backend
EXPOSE 8010
CMD ["python", "run.py"]

