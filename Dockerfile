FROM python:3.12-slim
WORKDIR /app
ARG APP_BUILD=lokal
ENV APP_BUILD=${APP_BUILD}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py LICENSE VERSION ./
COPY templates/ templates/
COPY assets/ assets/
EXPOSE 8080
CMD ["python", "/app/app.py"]
