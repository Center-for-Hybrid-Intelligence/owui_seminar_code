FROM python:3.11-slim

LABEL maintainer="Maceo"
LABEL description="OAuth revocation script disabled Azure AD accounts cleanup"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY revoke.py .

CMD ["python", "-u", "revoke.py"]
