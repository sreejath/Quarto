FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8765
ENV JIRA2ADO_UI_HOST=0.0.0.0
ENV JIRA2ADO_UI_PORT=8765
ENV JIRA2ADO_UI_NO_BROWSER=1

CMD ["python", "-m", "jira2ado.ui"]
