FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
EXPOSE 8787
ENTRYPOINT ["spendcap", "-c", "/config/spendcap.toml"]
CMD ["serve", "--host", "0.0.0.0"]
