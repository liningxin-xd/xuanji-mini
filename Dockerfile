FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY host_integration ./host_integration
COPY host_service ./host_service
COPY runtime ./runtime
COPY contracts ./contracts
COPY references ./references

RUN useradd --create-home --shell /usr/sbin/nologin xuanji \
    && mkdir -p /var/lib/xuanji/runs /var/lib/xuanji/tasks /var/lib/xuanji/results \
    && chown -R xuanji:xuanji /app /var/lib/xuanji

USER xuanji

EXPOSE 8091

CMD ["python", "-m", "host_service.server"]
