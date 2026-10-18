# The chat app. FalkorDB and Ollama run as their own services -- see
# docker-compose.yml -- so this image holds only the Python side.
FROM python:3.12-slim

# Dependencies first, in their own layer, so editing the source does not
# reinstall them.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# `src` is not a package on the path by default; every entry point does
# sys.path.insert itself, but the Streamlit app is launched by Streamlit
# rather than by us, so set it here too.
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8501

# Headless, and listening on every interface: the default binds loopback
# inside the container, which the host cannot reach.
CMD ["python", "-m", "streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
