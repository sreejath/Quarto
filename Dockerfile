FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8765
ENV QUARTO_UI_HOST=0.0.0.0
ENV QUARTO_UI_PORT=8765
ENV QUARTO_UI_NO_BROWSER=1

CMD ["python", "-m", "quarto.ui"]
