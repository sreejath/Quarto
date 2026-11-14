FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
ENV JIRA2ADO_UI_PORT=8080
ENV JIRA2ADO_UI_NO_BROWSER=1

CMD ["python", "-m", "jira2ado.ui"]
