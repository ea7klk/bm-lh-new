FROM python:3.12-slim

WORKDIR /app
COPY bminfo/requirements.txt ./requirements.txt
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY bminfo/ ./

RUN chmod +x entrypoint.sh
CMD ["./entrypoint.sh"]
