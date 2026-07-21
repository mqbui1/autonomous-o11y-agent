FROM mcr.microsoft.com/playwright/python:v1.49.0-noble
WORKDIR /app
# Base image bundles browser binaries only, not the pip package
RUN pip install --no-cache-dir playwright==1.49.0
COPY sim.py .
CMD ["python3", "-u", "sim.py"]
