FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home app
COPY help_me_tidy ./help_me_tidy
USER app
ENTRYPOINT ["python", "-m", "help_me_tidy"]
CMD ["check"]
